"""Unit tests — audit create/list (mocked JWT + PG)."""

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


def _load_handler_audit():
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
        "nuwa_audit_pg",
        insert_audit_event=lambda payload: {"id": "AUD-1", "duplicate": False},
        list_audit_events=lambda client_id, limit=300: [],
    )
    _install_stub("nuwa_pg_dispatch", _conn=lambda: None)

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "cdk", "lambdas"))
    sys.modules.pop("handler_audit", None)
    import handler_audit

    return handler_audit


def _event(path: str, body: dict | None = None, method: str = "POST", auth: bool = True) -> dict:
    headers = {"Authorization": "Bearer tok"} if auth else {}
    return {
        "httpMethod": method,
        "path": path,
        "headers": headers,
        "body": json.dumps(body or {}),
    }


def test_create_requires_jwt() -> None:
    ha = _load_handler_audit()
    out = ha.handler(
        _event("/prod/v1/audit/create", {"type": "auth.login", "category": "auth"}, auth=False),
        None,
    )
    assert out["statusCode"] == 401


def test_create_scopes_to_jwt_client() -> None:
    ha = _load_handler_audit()
    with mock.patch.object(ha, "insert_audit_event", return_value={"id": "AUD-1", "duplicate": False}) as insert:
        out = ha.handler(
            _event(
                "/v1/audit/create",
                {
                    "clientId": 7,
                    "type": "auth.login",
                    "category": "auth",
                    "user": "ulises sadadah",
                    "target": "ulises.m.1800@gmail.com",
                },
            ),
            None,
        )
    assert out["statusCode"] == 200
    insert.assert_called_once()
    payload = insert.call_args[0][0]
    assert payload["clientId"] == 7


def test_create_rejects_other_client() -> None:
    ha = _load_handler_audit()
    out = ha.handler(
        _event("/v1/audit/create", {"clientId": 1, "type": "auth.login", "category": "auth"}),
        None,
    )
    assert out["statusCode"] == 403


def test_list_returns_tenant_events() -> None:
    ha = _load_handler_audit()
    with mock.patch.object(
        ha,
        "list_audit_events",
        return_value=[{"id": "AUD-1", "clientId": 7, "user": "ulises sadadah", "type": "auth.login"}],
    ) as listed:
        out = ha.handler(_event("/v1/audit/list", {"clientId": 7, "limit": 50}), None)
    assert out["statusCode"] == 200
    body = json.loads(out["body"])
    assert body["clientId"] == 7
    assert body["events"][0]["user"] == "ulises sadadah"
    listed.assert_called_once_with(7, 50)
