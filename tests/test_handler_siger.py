"""Unit tests — SIGER lookups handler (worker auth, date floor, claim / reuse / record) with a fake PG."""

from __future__ import annotations

import json
import os
import sys
from contextlib import contextmanager
from types import ModuleType
from typing import Any

import pytest

LAMBDAS = os.path.join(os.path.dirname(__file__), "..", "cdk", "lambdas")


@pytest.fixture(autouse=True)
def _restore_modules():
    saved = dict(sys.modules)
    yield
    for name in list(sys.modules):
        if name not in saved:
            del sys.modules[name]
    sys.modules.update(saved)


class FakeConn:
    """Answers each execute() with the next scripted result; records SQL and params."""

    def __init__(self, results: list[Any]):
        self.results = list(results)
        self.calls: list[tuple[str, tuple]] = []
        self.commits = 0

    def execute(self, sql: str, params: tuple = ()):
        self.calls.append((" ".join(sql.split()), params))
        result = self.results.pop(0) if self.results else None
        conn = self

        class _Cur:
            def fetchone(self):
                return result[0] if isinstance(result, list) and result else (result if isinstance(result, dict) else None)

            def fetchall(self):
                return result if isinstance(result, list) else []

        _ = conn
        return _Cur()

    def commit(self):
        self.commits += 1


def _stub(name: str, **attrs) -> ModuleType:
    mod = ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


def _load(conn: FakeConn, worker: bool = True):
    try:
        import psycopg.types.json  # noqa: F401
    except ImportError:
        _stub("psycopg")
        _stub("psycopg.types")
        _stub("psycopg.types.json", Json=lambda x: x)

    class _Err(Exception):
        pass

    _stub("nuwa_config", DatabaseConfigError=_Err, SupabaseConfigError=_Err, ensure_data_backend=lambda: None, is_database_mode=lambda: True)
    _stub("nuwa_http", CORS_HEADERS={})
    _stub("nuwa_monitoring_worker", monitoring_worker_secret_ok=lambda event: worker)
    _stub("nuwa_obs_log", log_handler_enter=lambda *a, **k: None, log_phase=lambda *a, **k: None)

    @contextmanager
    def _conn():
        yield conn

    _stub("nuwa_pg_dispatch", _conn=_conn)
    sys.path.insert(0, LAMBDAS)
    sys.modules.pop("handler_siger", None)
    import handler_siger

    return handler_siger


def _call(mod, path: str, body: dict) -> tuple[int, dict]:
    out = mod.handler({"httpMethod": "POST", "path": f"/prod/v1/siger/{path}", "body": json.dumps(body)}, None)
    return out["statusCode"], json.loads(out["body"])


def _lookup(**over) -> dict:
    row = {
        "id": 1, "client_id": 8, "entity_id": "e1", "report_folio": "F1", "rfc": "GCO010101AAA",
        "query_name": "Gráficas Corona", "status": "running", "attempts": 1, "fems": [], "partners": [],
        "cost": None, "error": None, "reused_from": None, "created_at": "t", "updated_at": "t",
    }
    row.update(over)
    return row


ENTITY = {"party_type": "organization", "status": "active", "name": "Gráficas Corona", "legal_name": None, "rfc": "gco010101aaa"}


def test_requires_worker_secret():
    mod = _load(FakeConn([]), worker=False)
    status, body = _call(mod, "pending", {"clientIds": [8]})
    assert status == 401


def test_pending_never_goes_before_start(monkeypatch):
    monkeypatch.setenv("SIGER_START_AT", "2026-11-18T06:00:00Z")
    conn = FakeConn([[]])
    mod = _load(conn)
    status, body = _call(mod, "pending", {"clientIds": [8, "8", 9], "since": "2026-01-01", "until": "2026-11-19T00:00:00-06:00"})
    assert status == 200
    assert body["since"].startswith("2026-11-18T06:00:00")
    params = conn.calls[0][1]
    assert params[3] == [8, 9]
    assert "e.party_type = 'organization'" in conn.calls[0][0]


def test_pending_later_since_wins_and_bad_date_is_400(monkeypatch):
    monkeypatch.setenv("SIGER_START_AT", "2026-11-18T06:00:00Z")
    conn = FakeConn([[]])
    mod = _load(conn)
    status, body = _call(mod, "pending", {"clientIds": [8], "since": "2026-11-20T15:30:00Z"})
    assert body["since"].startswith("2026-11-20T15:30:00")
    status, body = _call(mod, "pending", {"clientIds": [8], "since": "ayer"})
    assert status == 400


def test_pending_without_start_at_is_503(monkeypatch):
    monkeypatch.delenv("SIGER_START_AT", raising=False)
    mod = _load(FakeConn([]))
    status, _ = _call(mod, "pending", {"clientIds": [8]})
    assert status == 503


def test_claim_rejects_individuals():
    conn = FakeConn([{**ENTITY, "party_type": "individual"}])
    mod = _load(conn)
    status, body = _call(mod, "claim", {"clientId": 8, "entityId": "e1"})
    assert status == 400 and body["code"] == "NOT_ORGANIZATION"


def test_claim_reuses_another_clients_lookup_without_paying():
    source = _lookup(id=40, client_id=3, entity_id="other", status="ok", partners=[{"name": "ANA"}], fems=["123"], cost=736)
    copied = _lookup(id=41, status="ok", partners=[{"name": "ANA"}], fems=["123"], cost=0, reused_from=40)
    conn = FakeConn([ENTITY, None, source, copied])
    mod = _load(conn)
    status, body = _call(mod, "claim", {"clientId": 8, "entityId": "e1", "folio": "F1"})
    assert status == 200
    assert body["claimed"] is False and body["reused"] is True
    assert body["lookup"]["reusedFrom"] == 40 and body["lookup"]["partners"] == [{"name": "ANA"}]
    reuse_sql, reuse_params = conn.calls[2]
    assert "upper(rfc) = %s" in reuse_sql and "GCO010101AAA" in reuse_params
    assert "graficascorona" in reuse_params
    assert conn.commits == 1


def test_claim_takes_new_company():
    conn = FakeConn([ENTITY, None, None, _lookup()])
    mod = _load(conn)
    status, body = _call(mod, "claim", {"clientId": 8, "entityId": "e1"})
    assert body["claimed"] is True and body["lookup"]["status"] == "running"


def test_claim_never_repeats_an_answered_company():
    done = _lookup(status="ok")
    conn = FakeConn([ENTITY, done, None, done])
    mod = _load(conn)
    status, body = _call(mod, "claim", {"clientId": 8, "entityId": "e1"})
    assert body["claimed"] is False and body.get("reused") is None
    # no reuse lookup when this client already has an answer
    assert not any("reused_from IS NULL" in sql for sql, _ in conn.calls)


def test_record_needs_a_running_claim():
    mod = _load(FakeConn([None]))
    status, body = _call(mod, "record", {"clientId": 8, "entityId": "e1", "status": "ok", "partners": []})
    assert status == 409


def test_record_rejects_unknown_status():
    mod = _load(FakeConn([]))
    status, _ = _call(mod, "record", {"clientId": 8, "entityId": "e1", "status": "running"})
    assert status == 400


def test_name_key_ignores_legal_form_and_accents():
    mod = _load(FakeConn([]))
    assert mod.name_key("Gráficas Corona, S.A. de C.V.") == mod.name_key("GRAFICAS CORONA SA DE CV") == "graficascorona"
    assert mod.name_key("Gráficas Corona J.E. S.A. de C.V.") != "graficascorona"
