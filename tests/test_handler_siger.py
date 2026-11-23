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
    assert params[5] == [8, 9] and params[7] is False
    assert "e.party_type = 'organization'" in conn.calls[0][0]


def test_pending_for_every_client_that_operates_in_mexico(monkeypatch):
    # Prod (23 nov 2026): SIGER for every client with Mexico among its operating countries, no fixed list.
    monkeypatch.setenv("SIGER_START_AT", "2026-11-18T06:00:00Z")
    conn = FakeConn([[]])
    mod = _load(conn)
    status, _ = _call(mod, "pending", {"operatingCountry": "Mexico"})
    assert status == 200
    sql, params = conn.calls[0]
    assert params[5] is None and params[6] is None and params[7] is True
    assert "'mexico' = ANY(c.operating_countries)" in sql and "operating_countries IS NULL" in sql and "'suspended'" in sql
    # Neither a list nor the country: refused.
    status, _ = _call(_load(FakeConn([[]])), "pending", {})
    assert status == 400


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


def test_claim_reuses_by_rfc_without_paying():
    source = _lookup(id=40, client_id=3, entity_id="other", status="ok", partners=[{"name": "ANA"}], fems=["123"], cost=736, aliases=[])
    copied = _lookup(id=41, status="ok", partners=[{"name": "ANA"}], fems=["123"], cost=0, reused_from=40)
    conn = FakeConn([ENTITY, None, source, copied, None])
    mod = _load(conn)
    status, body = _call(mod, "claim", {"clientId": 8, "entityId": "e1", "folio": "F1"})
    assert status == 200
    assert body["claimed"] is False and body["reused"] is True
    assert body["lookup"]["reusedFrom"] == 40 and body["lookup"]["partners"] == [{"name": "ANA"}]
    sure_sql, sure_params = conn.calls[2]
    assert "upper(rfc) = %s" in sure_sql and "GCO010101AAA" in sure_params
    # a name without legal form is never a sure match
    assert sure_params[5] is None
    # the searched name is remembered as an alias of the source company
    assert "SET aliases" in conn.calls[4][0]
    assert conn.commits == 1


def test_claim_reuses_exact_legal_name_with_legal_form():
    entity = {**ENTITY, "rfc": None, "legal_name": "Gráficas Corona J.E., S.A. de C.V."}
    source = _lookup(id=40, client_id=3, entity_id="other", status="ok")
    conn = FakeConn([entity, None, source, _lookup(id=41, status="ok", reused_from=40), None])
    mod = _load(conn)
    status, body = _call(mod, "claim", {"clientId": 8, "entityId": "e1"})
    assert body["reused"] is True
    assert conn.calls[2][1][5] == "graficascoronajesadecv"


def test_claim_name_without_legal_form_returns_candidates_not_reuse():
    entity = {**ENTITY, "rfc": None}
    cand = _lookup(id=40, client_id=3, entity_id="other", status="ok", legal_name="Gráficas Corona J.E., S.A. de C.V.",
                   aliases=["Gráficas Corona"], partners=[{"name": "ANA"}], fems=["123"])
    conn = FakeConn([entity, None, None, [cand]])
    mod = _load(conn)
    status, body = _call(mod, "claim", {"clientId": 8, "entityId": "e1"})
    assert body["claimed"] is False and "reused" not in body
    assert body["candidates"][0]["id"] == 40 and body["candidates"][0]["partners"] == 1
    assert "graficascorona" in conn.calls[3][1]
    assert conn.commits == 0


def test_claim_with_reuse_from_id_copies_the_chosen_candidate():
    entity = {**ENTITY, "rfc": None}
    source = _lookup(id=40, client_id=3, entity_id="other", status="ok", aliases=[])
    conn = FakeConn([entity, None, source, _lookup(id=41, status="ok", reused_from=40), None])
    mod = _load(conn)
    status, body = _call(mod, "claim", {"clientId": 8, "entityId": "e1", "reuseFromId": 40})
    assert body["reused"] is True and conn.calls[2][1][0] == 40


def test_claim_force_pays_even_with_candidates():
    entity = {**ENTITY, "rfc": None}
    conn = FakeConn([entity, None, None, _lookup()])
    mod = _load(conn)
    status, body = _call(mod, "claim", {"clientId": 8, "entityId": "e1", "force": True})
    assert body["claimed"] is True
    assert not any("base_key = %s" in sql for sql, _ in conn.calls)


def test_claim_takes_new_company():
    conn = FakeConn([ENTITY, None, None, [], _lookup()])
    mod = _load(conn)
    status, body = _call(mod, "claim", {"clientId": 8, "entityId": "e1"})
    assert body["claimed"] is True and body["lookup"]["status"] == "running"


def test_claim_never_repeats_an_answered_company():
    done = _lookup(status="ok")
    conn = FakeConn([ENTITY, done])
    mod = _load(conn)
    status, body = _call(mod, "claim", {"clientId": 8, "entityId": "e1"})
    assert body["claimed"] is False and body.get("reused") is None
    assert len(conn.calls) == 2


def test_record_stores_chosen_company_and_aliases():
    conn = FakeConn([_lookup(status="ok")])
    mod = _load(conn)
    status, body = _call(mod, "record", {
        "clientId": 8, "entityId": "e1", "status": "ok", "legalName": "Gráficas Corona J.E., S.A. de C.V.",
        "rfc": "gcj0101", "searchName": "Gráficas Corona", "matchReason": "report_context",
        "candidates": [{"fem": "1"}, {"fem": "2"}], "partners": [{"name": "ANA"}],
    })
    assert status == 200
    params = conn.calls[0][1]
    assert params[1] == "GCJ0101" and params[3] == "graficascoronajesadecv" and params[4] == "graficascoronaje"
    assert getattr(params[6], "obj", params[6]) == ["Gráficas Corona"] and params[7] == "report_context"


def test_record_needs_a_running_claim():
    mod = _load(FakeConn([None]))
    status, body = _call(mod, "record", {"clientId": 8, "entityId": "e1", "status": "ok", "partners": []})
    assert status == 409


def test_record_rejects_unknown_status():
    mod = _load(FakeConn([]))
    status, _ = _call(mod, "record", {"clientId": 8, "entityId": "e1", "status": "running"})
    assert status == 400


def test_keys():
    mod = _load(FakeConn([]))
    assert mod.legal_key("Gráficas Corona J.E., S.A. de C.V.") == mod.legal_key("GRAFICAS CORONA JE SA DE CV")
    assert mod.base_key("Gráficas Corona, S.A. de C.V.") == mod.base_key("Gráficas Corona") == "graficascorona"
    assert mod.base_key("Gráficas Corona J.E. S.A. de C.V.") == "graficascoronaje"
    assert mod.has_legal_form("Gráficas Corona S.A. de C.V.") and not mod.has_legal_form("Gráficas Corona")


def test_pending_also_returns_known_companies_whose_new_report_lacks_partners(monkeypatch):
    monkeypatch.setenv("SIGER_START_AT", "2026-11-18T06:00:00Z")
    row = {"entity_id": "e1", "client_id": 1, "name": "Cadgrafics", "legal_name": None, "rfc": None, "folio": "F2",
           "report_created_at": "t", "created_by_user_id": 5, "lookup_status": "ok", "lookup_attempts": 1, "attach_only": True}
    conn = FakeConn([[row]])
    mod = _load(conn)
    status, body = _call(mod, "pending", {"clientIds": [1]})
    assert body["companies"][0]["attachOnly"] is True and body["companies"][0]["folio"] == "F2"
    assert "s.report_folio IS DISTINCT FROM x.folio" in conn.calls[0][0]


def test_attach_moves_the_lookup_to_the_new_report():
    conn = FakeConn([_lookup(status="ok", report_folio="F2")])
    mod = _load(conn)
    status, body = _call(mod, "attach", {"clientId": 8, "entityId": "e1", "folio": "F2"})
    assert status == 200 and body["lookup"]["reportFolio"] == "F2"
    assert conn.calls[0][1][0] == "F2"
    mod = _load(FakeConn([None]))
    status, _ = _call(mod, "attach", {"clientId": 8, "entityId": "e1", "folio": "F2"})
    assert status == 409


def test_resolve_turns_a_multiple_lookup_into_ok_without_paying():
    conn = FakeConn([_lookup(status="ok", legal_name="GRAFICAS CORONA J.E.", partners=[{"name": "A"}, {"name": "B"}])])
    mod = _load(conn)
    status, body = _call(mod, "resolve", {"clientId": 1, "entityId": "e2", "legalName": "GRAFICAS CORONA J.E.",
                                           "fems": ["289782"], "partners": [{"name": "A"}, {"name": "B"}], "matchReason": "report_context"})
    assert status == 200 and body["lookup"]["status"] == "ok"
    sql, params = conn.calls[0]
    assert "status = 'multiple'" in sql and params[1] == "graficascoronaje" and params[6] == "report_context"
    mod = _load(FakeConn([None]))
    assert _call(mod, "resolve", {"clientId": 1, "entityId": "e2", "legalName": "X", "partners": [{"name": "A"}]})[0] == 409
    assert _call(mod, "resolve", {"clientId": 1, "entityId": "e2", "legalName": "X", "partners": []})[0] == 400


REP = {"name": "ANA", "personKind": "individual", "rfc": "BBBB000000XX0", "curp": "BBBB000000MDFXXX00", "role": "administrador unico"}


def test_record_stores_the_legal_representatives_also_without_partners():
    conn = FakeConn([_lookup(status="ok", representatives=[REP])])
    mod = _load(conn)
    status, body = _call(mod, "record", {"clientId": 8, "entityId": "e1", "status": "ok", "partners": [], "representatives": [REP]})
    assert status == 200 and body["lookup"]["representatives"] == [REP]
    sql, params = conn.calls[0]
    assert "representatives = %s" in sql
    assert getattr(params[-3], "obj", params[-3]) == [REP]


def test_get_and_attach_return_the_representatives():
    mod = _load(FakeConn([_lookup(status="ok", representatives=[REP])]))
    assert _call(mod, "get", {"clientId": 8, "entityId": "e1"})[1]["lookup"]["representatives"] == [REP]
    mod = _load(FakeConn([_lookup(status="ok", report_folio="F2", representatives=[REP])]))
    assert _call(mod, "attach", {"clientId": 8, "entityId": "e1", "folio": "F2"})[1]["lookup"]["representatives"] == [REP]
    # Lookups saved before the column existed come back with an empty list.
    mod = _load(FakeConn([_lookup(status="ok")]))
    assert _call(mod, "get", {"clientId": 8, "entityId": "e1"})[1]["lookup"]["representatives"] == []


def test_claim_reuse_copies_the_representatives():
    source = _lookup(id=40, client_id=3, entity_id="other", status="ok", partners=[], representatives=[REP], aliases=[])
    copied = _lookup(id=41, status="ok", representatives=[REP], reused_from=40)
    conn = FakeConn([ENTITY, None, source, copied, None])
    mod = _load(conn)
    status, body = _call(mod, "claim", {"clientId": 8, "entityId": "e1"})
    assert body["reused"] is True and body["lookup"]["representatives"] == [REP]
    copy_sql, copy_params = conn.calls[3]
    assert "representatives = EXCLUDED.representatives" in copy_sql
    assert [REP] in [getattr(p, "obj", p) for p in copy_params]


def test_pending_attach_only_counts_representatives_too(monkeypatch):
    monkeypatch.setenv("SIGER_START_AT", "2026-11-18T06:00:00Z")
    conn = FakeConn([[]])
    mod = _load(conn)
    _call(mod, "pending", {"clientIds": [1]})
    assert "jsonb_array_length(s.representatives) > 0" in conn.calls[0][0]


def test_resolve_accepts_only_representatives():
    conn = FakeConn([_lookup(status="ok", representatives=[REP])])
    mod = _load(conn)
    status, body = _call(mod, "resolve", {"clientId": 1, "entityId": "e2", "legalName": "X", "partners": [], "representatives": [REP]})
    assert status == 200 and body["lookup"]["representatives"] == [REP]
    assert getattr(conn.calls[0][1][-3], "obj", conn.calls[0][1][-3]) == [REP]


def test_pending_returns_a_failed_lookup_with_attempts_left_even_before_since(monkeypatch):
    monkeypatch.setenv("SIGER_START_AT", "2026-11-18T06:00:00Z")
    row = {"entity_id": "e1", "client_id": 1, "name": "Gráficas Corona", "legal_name": None, "rfc": None, "folio": "F1",
           "report_created_at": "t", "created_by_user_id": 5, "lookup_status": "error", "lookup_attempts": 3, "attach_only": False}
    conn = FakeConn([[row]])
    mod = _load(conn)
    status, body = _call(mod, "pending", {"clientIds": [1], "since": "2026-11-23T02:00:00Z"})
    assert status == 200 and body["companies"][0]["attempts"] == 3 and body["companies"][0]["previousStatus"] == "error"
    sql, params = conn.calls[0]
    # Newer reports from the cursor, or an older one whose lookup failed with attempts left (never before the floor).
    assert "f.status = 'error' AND f.attempts < %s" in sql
    assert params[0].isoformat().startswith("2026-11-23T02:00:00") and params[1].isoformat().startswith("2026-11-18T06:00:00")
    assert params[2] == 20 and params[-2] == 20


def test_claim_retakes_an_error_until_the_twentieth_attempt():
    mod = _load(FakeConn([ENTITY, _lookup(status="error", attempts=3), None, _lookup(status="running", attempts=4)]))
    status, body = _call(mod, "claim", {"clientId": 8, "entityId": "e1", "force": True})
    assert status == 200 and body["claimed"] is True and body["lookup"]["attempts"] == 4
    mod = _load(FakeConn([ENTITY, _lookup(status="error", attempts=20)]))
    status, body = _call(mod, "claim", {"clientId": 8, "entityId": "e1"})
    assert status == 200 and body["claimed"] is False and body["lookup"]["attempts"] == 20
    assert mod.MAX_ATTEMPTS == 20
