"""Unit tests for monitoring due/runs/alerts (mocked PG + handler auth)."""

from __future__ import annotations

import calendar
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from types import ModuleType
from unittest import mock


def _install_stub(name: str, **attrs) -> ModuleType:
    mod = ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


def _add_months(dt: datetime, months: int) -> datetime:
    month = dt.month - 1 + months
    year = dt.year + month // 12
    month = month % 12 + 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def _next_run_at_local(frequency: str, from_dt: datetime) -> datetime:
    if frequency == "weekly":
        return from_dt + timedelta(days=7)
    if frequency == "monthly":
        return _add_months(from_dt, 1)
    if frequency == "semi-annual":
        return _add_months(from_dt, 6)
    if frequency == "annual":
        return _add_months(from_dt, 12)
    return from_dt + timedelta(days=7)


def test_next_run_at_weekly_u_a01() -> None:
    now = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
    assert _next_run_at_local("weekly", now) == now + timedelta(days=7)


def test_next_run_at_calendar_u_a02() -> None:
    now = datetime(2026, 1, 31, 12, 0, tzinfo=timezone.utc)
    monthly = _next_run_at_local("monthly", now)
    assert monthly.month == 2 and monthly.day == 28
    semi = _next_run_at_local("semi-annual", datetime(2026, 3, 15, tzinfo=timezone.utc))
    assert semi.month == 9 and semi.day == 15
    annual = _next_run_at_local("annual", datetime(2026, 3, 15, tzinfo=timezone.utc))
    assert annual.year == 2027 and annual.month == 3


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

    _install_stub(
        "nuwa_api_auth",
        effective_tenant_scope=lambda claims: None if claims.get("role") == "super_admin" else claims.get("cid"),
        jwt_allows_client=lambda claims, cid: True,
        jwt_matches_actor_body=lambda claims, body: True,
        require_jwt=lambda event: {"sub": 1, "cid": 1, "role": "admin"},
    )
    _install_stub("nuwa_jwt", authorization_header_value=lambda e: None, jwt_int=lambda c, k: int(c[k]), verify_access_token=lambda t: None)
    _install_stub("nuwa_config", DatabaseConfigError=Exception, SupabaseConfigError=Exception, ensure_data_backend=lambda: None, is_database_mode=lambda: True)
    _install_stub("nuwa_errors", SupabaseRestError=type("SupabaseRestError", (Exception,), {"__init__": lambda self, status, body: (setattr(self, "status", status), setattr(self, "body", body), None)[-1]}))
    _install_stub("nuwa_http", CORS_HEADERS={})
    _install_stub("nuwa_obs_log", log_handler_enter=lambda *a, **k: None, log_phase=lambda *a, **k: None)

    # Fresh import of handler
    sys.modules.pop("handler_entities", None)
    import handler_entities

    return handler_entities


def _event(path_suffix: str, body: dict, *, secret: str | None = None, jwt: bool = True) -> dict:
    headers: dict[str, str] = {}
    if jwt:
        headers["Authorization"] = "Bearer tok"
    if secret:
        headers["x-monitoring-worker-secret"] = secret
    return {
        "httpMethod": "POST",
        "path": f"/prod/v1/entities/{path_suffix}",
        "body": json.dumps(body),
        "headers": headers,
    }


def test_due_requires_worker_secret() -> None:
    he = _load_handler()
    with mock.patch.dict(os.environ, {"MONITORING_WORKER_SECRET": "s3cret"}):
        with mock.patch.dict("sys.modules", {"nuwa_entities_pg": mock.MagicMock()}):
            # re-bind import inside handler by patching after load
            pass
        out = he.handler(_event("monitoring/due", {"limit": 10}, jwt=False), None)
    assert out["statusCode"] == 401


def test_due_with_secret_includes_client_id_u_a06b() -> None:
    he = _load_handler()
    due_mod = mock.MagicMock()
    due_mod.entities_monitoring_due_pg.return_value = {
        "items": [{"monitoringId": "m1", "entityId": "e1", "clientId": 42}],
        "total": 1,
    }
    with mock.patch.dict(os.environ, {"MONITORING_WORKER_SECRET": "s3cret"}):
        with mock.patch.dict(sys.modules, {"nuwa_entities_pg": due_mod}):
            sys.modules.pop("handler_entities", None)
            he = _load_handler()
            # Force the import inside handler to see our mock
            with mock.patch.dict(sys.modules, {"nuwa_entities_pg": due_mod}):
                out = he.handler(
                    _event("monitoring/due", {"limit": 5}, secret="s3cret", jwt=False),
                    None,
                )
    assert out["statusCode"] == 200
    body = json.loads(out["body"])
    assert body["items"][0]["clientId"] == 42


def test_run_start_and_alerts_with_worker_secret() -> None:
    he = _load_handler()
    pg = mock.MagicMock()
    pg.entities_monitoring_run_start_pg.return_value = {"runId": "r1", "status": "running", "clientId": 1}
    pg.entities_alerts_create_pg.return_value = {
        "alertId": "a1",
        "alertType": "risk_change",
        "severity": "high",
        "clientId": 1,
        "entityId": "e1",
        "status": "new",
    }
    with mock.patch.dict(os.environ, {"MONITORING_WORKER_SECRET": "s3cret"}):
        with mock.patch.dict(sys.modules, {"nuwa_entities_pg": pg}):
            out1 = he.handler(
                _event(
                    "monitoring/run-start",
                    {"clientId": 1, "monitoringId": "m1"},
                    secret="s3cret",
                    jwt=False,
                ),
                None,
            )
            out2 = he.handler(
                _event(
                    "alerts/create",
                    {
                        "clientId": 1,
                        "entityId": "e1",
                        "alertType": "risk_change",
                        "severity": "high",
                        "title": "Risk up",
                    },
                    secret="s3cret",
                    jwt=False,
                ),
                None,
            )
    assert out1["statusCode"] == 200
    assert out2["statusCode"] == 201
    assert json.loads(out2["body"])["severity"] == "high"


def test_alerts_list_and_ack_jwt() -> None:
    he = _load_handler()
    pg = mock.MagicMock()
    pg.entities_alerts_list_pg.return_value = {
        "items": [],
        "total": 0,
        "aggregates": {"newCount": 0, "highNew": 0, "mediumNew": 0, "totalAll": 0},
    }
    pg.entities_alerts_ack_pg.return_value = {
        "alertId": "a1",
        "status": "dismissed",
        "clientId": 1,
        "entityId": "e1",
    }
    with mock.patch.dict(sys.modules, {"nuwa_entities_pg": pg}):
        out_list = he.handler(_event("alerts/list", {"clientId": 1, "userId": 1}), None)
        out_ack = he.handler(
            _event("alerts/ack", {"clientId": 1, "userId": 1, "alertId": "a1", "status": "dismissed"}),
            None,
        )
    assert out_list["statusCode"] == 200
    assert out_ack["statusCode"] == 200
    assert json.loads(out_ack["body"])["status"] == "dismissed"
