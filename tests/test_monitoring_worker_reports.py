"""U-A17–A19: reports get/save/update auth via MONITORING_WORKER_SECRET."""

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


def _load_handler_reports():
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
            return {"sub": 1, "cid": 1, "role": "admin"}
        return "JWT requerido"

    _install_stub(
        "nuwa_api_auth",
        effective_tenant_scope=lambda claims: None if claims.get("role") == "monitoring_worker" else claims.get("cid"),
        jwt_allows_client=lambda claims, cid: True,
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
    _install_stub("nuwa_rbac", can_read_report=lambda *a, **k: True)
    _install_stub("nuwa_supabase", fetch_user_with_role=lambda *a, **k: None, rest_json=lambda *a, **k: [])

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "cdk", "lambdas"))
    sys.modules.pop("handler_reports", None)
    import handler_reports

    return handler_reports


def _worker_event(path: str, body: dict | None = None, query: dict | None = None) -> dict:
    return {
        "httpMethod": "POST",
        "path": path,
        "headers": {"x-monitoring-worker-secret": "s3cret"},
        "queryStringParameters": query or {},
        "body": json.dumps(body or {}),
    }


@mock.patch.dict("os.environ", {"MONITORING_WORKER_SECRET": "s3cret"})
def test_u_a17_worker_get_requires_folio() -> None:
    hr = _load_handler_reports()
    with mock.patch.object(hr, "rest_json") as mock_rest:
        mock_rest.return_value = [
            {
                "id": 1,
                "folio": "RPT-1",
                "client_id": 1,
                "created_by_user_id": 2,
                "report_json": {"folio": "RPT-1"},
                "status": "active",
                "entidad": "Test",
                "tipo_consulta": "Persona moral",
                "fecha": "2026-01-01",
                "nivel_riesgo": "clear",
                "nivel_riesgo_numerico": 0,
            }
        ]
        event = _worker_event(
            "/prod/v1/reports/get",
            {"clientId": 1, "folio": "RPT-1", "includePayload": "true"},
        )
        out = hr.handler(event, None)
    assert out["statusCode"] == 200


@mock.patch.dict("os.environ", {"MONITORING_WORKER_SECRET": "s3cret"})
def test_u_a18_worker_get_without_secret_401() -> None:
    hr = _load_handler_reports()
    event = {
        "httpMethod": "POST",
        "path": "/v1/reports/get",
        "body": json.dumps({"clientId": 1, "folio": "X"}),
    }
    out = hr.handler(event, None)
    assert out["statusCode"] == 401


@mock.patch.dict("os.environ", {"MONITORING_WORKER_SECRET": "s3cret"})
def test_u_a19_worker_save_entity_mismatch_403() -> None:
    hr = _load_handler_reports()
    event = _worker_event(
        "/prod/v1/reports/save",
        {
            "clientId": 1,
            "userId": 2,
            "entityId": "ent-bad",
            "report": {"folio": "RPT-NEW", "entidad": "Test"},
        },
    )
    with mock.patch.object(hr, "_entity_belongs_to_client", return_value=False) as mock_ent:
        with mock.patch.object(hr, "rest_json", return_value=[{"id": 2}]):
            out = hr.handler(event, None)
    assert out["statusCode"] == 403
    mock_ent.assert_called_once_with(1, "ent-bad")
