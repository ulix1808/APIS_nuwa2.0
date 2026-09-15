"""Unit tests — escalations list/save/resolve (mocked JWT + PG)."""

from __future__ import annotations

import json
import os
import sys
from types import ModuleType
from unittest import mock


def _install_stub(name: str, **attrs) -> ModuleType:
    mod = ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


def _load_handler():
    stubs = [
        "jwt",
        "cryptography",
        "cryptography.fernet",
        "cryptography.hazmat",
        "cryptography.hazmat.primitives",
        "cryptography.hazmat.primitives.asymmetric",
        "cryptography.hazmat.primitives.asymmetric.ec",
        "cryptography.hazmat.primitives.hashes",
        "cryptography.hazmat.primitives.serialization",
        "cryptography.exceptions",
        "psycopg",
        "psycopg.errors",
        "psycopg.types",
        "psycopg.types.json",
    ]
    for name in stubs:
        if name not in sys.modules:
            _install_stub(name)
    sys.modules["psycopg.types.json"] = _install_stub("psycopg.types.json", Json=lambda x: x)

    def _require_jwt(event):
        headers = {str(k).lower(): str(v) for k, v in (event.get("headers") or {}).items()}
        if headers.get("authorization"):
            return {"sub": "6", "cid": 7, "role": "admin"}
        return "Se requiere Authorization"

    def _allows(claims, client_id):
        if claims.get("role") == "super_admin":
            return True
        return int(claims.get("cid")) == int(client_id)

    _install_stub(
        "nuwa_api_auth",
        effective_tenant_scope=lambda claims: None if claims.get("role") == "super_admin" else int(claims["cid"]),
        jwt_allows_client=_allows,
        require_jwt=_require_jwt,
    )
    _install_stub(
        "nuwa_jwt",
        authorization_header_value=lambda e: None,
        jwt_int=lambda c, k: int(c[k]),
        verify_access_token=lambda t: None,
    )
    _install_stub(
        "nuwa_config",
        DatabaseConfigError=Exception,
        SupabaseConfigError=Exception,
        ensure_data_backend=lambda: None,
        is_database_mode=lambda: True,
    )
    _install_stub(
        "nuwa_errors",
        SupabaseRestError=type(
            "SupabaseRestError",
            (Exception,),
            {
                "__init__": lambda self, status, body: (
                    setattr(self, "status", status),
                    setattr(self, "body", body),
                    None,
                )[-1]
            },
        ),
    )
    _install_stub("nuwa_http", CORS_HEADERS={})
    _install_stub("nuwa_obs_log", log_handler_enter=lambda *a, **k: None, log_phase=lambda *a, **k: None)
    _install_stub(
        "nuwa_escalations_pg",
        list_escalations=lambda client_id, limit=500: [],
        get_escalation=lambda client_id, escalation_id: None,
        save_escalation=lambda client_id, payload: payload,
        resolve_escalation=lambda client_id, escalation_id, resolution: None,
    )
    _install_stub("nuwa_pg_dispatch", _conn=lambda: None)

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "cdk", "lambdas"))
    sys.modules.pop("handler_escalations", None)
    import handler_escalations

    return handler_escalations


def _event(path: str, body: dict | None = None, auth: bool = True) -> dict:
    headers = {"Authorization": "Bearer tok"} if auth else {}
    return {
        "httpMethod": "POST",
        "path": path,
        "headers": headers,
        "body": json.dumps(body or {}),
    }


def test_list_requires_jwt() -> None:
    he = _load_handler()
    out = he.handler(_event("/v1/escalations/list", {"clientId": 7}, auth=False), None)
    assert out["statusCode"] == 401


def test_list_rejects_other_client() -> None:
    he = _load_handler()
    out = he.handler(_event("/v1/escalations/list", {"clientId": 1}), None)
    assert out["statusCode"] == 403


def test_save_scopes_to_jwt_client() -> None:
    he = _load_handler()
    with mock.patch.object(he, "get_escalation", return_value=None):
        with mock.patch.object(
            he,
            "save_escalation",
            return_value={"id": "esc-1", "entityName": "ACME", "status": "active"},
        ) as save:
            out = he.handler(
                _event(
                    "/v1/escalations/save",
                    {"clientId": 7, "id": "esc-1", "entityName": "ACME"},
                ),
                None,
            )
    assert out["statusCode"] == 200
    body = json.loads(out["body"])
    assert body["isNew"] is True
    save.assert_called_once()
    assert save.call_args[0][0] == 7


def test_resolve_not_found() -> None:
    he = _load_handler()
    with mock.patch.object(he, "resolve_escalation", return_value=None):
        out = he.handler(
            _event("/v1/escalations/resolve", {"clientId": 7, "id": "missing"}),
            None,
        )
    assert out["statusCode"] == 404
