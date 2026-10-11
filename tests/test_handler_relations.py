"""Unit tests — entity relations query/sync handler and row normalization (mocked JWT + PG)."""

from __future__ import annotations

import json
import os
import sys
from types import ModuleType
from unittest import mock

import pytest


@pytest.fixture(autouse=True)
def _restore_modules():
    saved = dict(sys.modules)
    yield
    for name in list(sys.modules):
        if name not in saved:
            del sys.modules[name]
    sys.modules.update(saved)


def _install_stub(name: str, **attrs) -> ModuleType:
    mod = ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


class _UndefinedTable(Exception):
    pass


def _stub_common() -> None:
    try:
        import psycopg  # noqa: F401
        import psycopg.types.json  # noqa: F401
    except ImportError:
        _install_stub("psycopg", errors=_install_stub("psycopg.errors", UndefinedTable=_UndefinedTable))
        _install_stub("psycopg.types")
        _install_stub("psycopg.types.json", Json=lambda x: x)

    def _require_jwt(event):
        headers = {str(k).lower(): str(v) for k, v in (event.get("headers") or {}).items()}
        if headers.get("authorization"):
            return {"sub": "6", "cid": 7, "role": "admin"}
        return "Se requiere Authorization"

    def _allows(claims, client_id):
        return claims.get("role") == "super_admin" or int(claims.get("cid")) == int(client_id)

    _install_stub(
        "nuwa_api_auth",
        effective_tenant_scope=lambda claims: None if claims.get("role") == "super_admin" else int(claims["cid"]),
        jwt_allows_client=_allows,
        require_jwt=_require_jwt,
    )
    _install_stub(
        "nuwa_config",
        DatabaseConfigError=Exception,
        SupabaseConfigError=Exception,
        ensure_data_backend=lambda: None,
        is_database_mode=lambda: True,
    )
    _install_stub("nuwa_http", CORS_HEADERS={})
    _install_stub("nuwa_obs_log", log_handler_enter=lambda *a, **k: None, log_phase=lambda *a, **k: None)
    _install_stub("nuwa_pg_dispatch", _conn=lambda: None)
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "cdk", "lambdas"))


def _load_handler():
    _stub_common()
    _install_stub(
        "nuwa_relations_pg",
        query_relations=lambda client_id, **kw: [],
        replace_report_relations=lambda client_id, folio, rows, **kw: len(rows),
        report_belongs_to_client=lambda client_id, folio: True,
    )
    sys.modules.pop("handler_relations", None)
    import handler_relations

    return handler_relations


def _load_pg_module():
    _stub_common()
    sys.modules.pop("nuwa_relations_pg", None)
    import nuwa_relations_pg

    return nuwa_relations_pg


def _event(path: str, body: dict | None = None, auth: bool = True) -> dict:
    headers = {"Authorization": "Bearer tok"} if auth else {}
    return {"httpMethod": "POST", "path": path, "headers": headers, "body": json.dumps(body or {})}


ROW = {
    "subjectName": "Andres Manuel lopez beltran",
    "subjectKey": "andresmanuellopezbeltran",
    "subjectAliases": ["Andy"],
    "subjectAliasKeys": ["andylopezbeltran"],
    "relatedName": "Ikon Midstream",
    "relatedKey": "ikonmidstream",
    "relatedPartyType": "organization",
    "relationship": "Empresa investigada por contrabando",
    "confidence": 75.4,
    "sourceUrl": "https://www.razon.com.mx/nota",
}


def test_query_requires_jwt() -> None:
    hr = _load_handler()
    assert hr.handler(_event("/v1/relations/query", {"keys": ["x"]}, auth=False), None)["statusCode"] == 401


def test_query_rejects_other_client() -> None:
    hr = _load_handler()
    assert hr.handler(_event("/v1/relations/query", {"clientId": 1, "keys": ["x"]}), None)["statusCode"] == 403


def test_query_scopes_to_token_client_and_passes_keys() -> None:
    hr = _load_handler()
    with mock.patch.object(hr, "query_relations", return_value=[{"relatedName": "Ikon Midstream"}]) as q:
        out = hr.handler(_event("/v1/relations/query", {"keys": ["ikonmidstream"], "entityIds": ["e-1"]}), None)
    assert out["statusCode"] == 200
    body = json.loads(out["body"])
    assert body["available"] is True and body["relations"][0]["relatedName"] == "Ikon Midstream"
    assert q.call_args.args[0] == 7
    assert q.call_args.kwargs["keys"] == ["ikonmidstream"]


def test_query_reports_missing_table() -> None:
    hr = _load_handler()
    with mock.patch.object(hr, "query_relations", return_value=None):
        out = hr.handler(_event("/v1/relations/query", {"keys": ["x"]}), None)
    assert json.loads(out["body"]) == {"success": True, "available": False, "relations": []}


def test_sync_requires_owned_report() -> None:
    hr = _load_handler()
    with mock.patch.object(hr, "report_belongs_to_client", return_value=False):
        out = hr.handler(_event("/v1/relations/sync", {"folio": "SE-1", "rows": [ROW]}), None)
    assert out["statusCode"] == 404


def test_sync_replaces_rows() -> None:
    hr = _load_handler()
    with mock.patch.object(hr, "replace_report_relations", return_value=1) as rep:
        out = hr.handler(_event("/v1/relations/sync", {"folio": "SE-1", "rows": [ROW], "subjectEntityId": "e-1"}), None)
    assert out["statusCode"] == 200
    assert rep.call_args.args[:2] == (7, "SE-1")
    assert rep.call_args.kwargs["subject_entity_id"] == "e-1"


def test_sync_missing_table_is_503() -> None:
    hr = _load_handler()
    with mock.patch.object(hr, "replace_report_relations", return_value=-1):
        out = hr.handler(_event("/v1/relations/sync", {"folio": "SE-1", "rows": []}), None)
    assert out["statusCode"] == 503


def test_normalize_rows_drops_incomplete_and_fills_defaults() -> None:
    pg = _load_pg_module()
    rows = pg.normalize_relation_rows(
        [ROW, {"relatedName": "sin clave"}, "basura"],
        client_id=7,
        folio="SE-1",
        subject_entity_id="e-1",
        report_created_at="2026-10-06T17:43:34Z",
    )
    assert len(rows) == 1
    r = rows[0]
    assert r["client_id"] == 7 and r["report_folio"] == "SE-1" and r["status"] == "active"
    assert r["subject_entity_id"] == "e-1"
    assert r["confidence"] == 75
    assert r["subject_alias_keys"] == ["andylopezbeltran"]
    assert list(getattr(r["subject_aliases"], "obj", r["subject_aliases"])) == ["Andy"]
    assert r["report_created_at"] == "2026-10-06T17:43:34Z"
    assert set(pg._COLUMNS) == set(r.keys())
