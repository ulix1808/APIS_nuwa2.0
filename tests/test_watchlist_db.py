"""
Integration tests for the early-warning list sync (watchlist_db + handler_watchlist_sync) on a real PostgreSQL.

Skipped unless NUWA_TEST_PG is set to a libpq conninfo of a throwaway database, e.g.:
  docker run -d --rm --name nuwa-watchlist-test -e POSTGRES_PASSWORD=test -p 55433:5432 postgres:16-alpine
  NUWA_TEST_PG="host=localhost port=55433 user=postgres password=test dbname=postgres" pytest tests/test_watchlist_db.py
"""

from __future__ import annotations

import copy
import hashlib
import os
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "cdk" / "lambdas"))

psycopg = pytest.importorskip("psycopg")
from psycopg.rows import dict_row  # noqa: E402

import handler_watchlist_sync as sync  # noqa: E402
import watchlist_db as db  # noqa: E402
import watchlist_ofac as ofac  # noqa: E402

CONNINFO = os.environ.get("NUWA_TEST_PG")
pytestmark = pytest.mark.skipif(not CONNINFO, reason="NUWA_TEST_PG not set")
FIXTURES = ROOT / "tests" / "fixtures" / "ofac"
MIGRATION = ROOT / "supabase" / "migrations" / "20261020000000_watchlist_lists.sql"


@pytest.fixture()
def conn():
    c = psycopg.connect(CONNINFO, row_factory=dict_row, autocommit=True)
    c.execute("drop schema public cascade; create schema public;")
    c.execute(MIGRATION.read_text())
    c.execute(MIGRATION.read_text())  # idempotent
    yield c
    c.close()


def _records():
    _, sdn = ofac.parse_advanced_xml(str(FIXTURES / "SDN_SAMPLE.XML"))
    _, cons = ofac.parse_advanced_xml(str(FIXTURES / "CONS_SAMPLE.XML"))
    return ofac.merge_records(sdn, cons)


def _sync(conn, records):
    run_id = db.start_run(conn, "sync", "ofac", "manual")
    counts = db.apply_sync(conn, "ofac", records, run_id)
    db.finish_run(conn, run_id, "ok", counts=counts)
    return run_id, counts


def _changes(conn, run_id):
    return sorted(
        (r["ext_id"], r["change"])
        for r in conn.execute(
            "select e.ext_id, c.change from watchlist_entry_changes c join watchlist_entries e on e.id = c.entry_id where c.run_id = %s",
            (run_id,),
        ).fetchall()
    )


def test_first_sync_adds_everything_with_ids(conn):
    run_id, counts = _sync(conn, _records())
    assert counts == {"total": 10, "added": 10, "changed": 0, "removed": 0, "restored": 0, "unchanged": 0}
    green = conn.execute("select * from watchlist_entries where ext_id = '58020'").fetchone()
    assert green["name_norm"] == "green agropacific s p r de r l de c v"
    assert str(green["designated_on"]) == "2026-07-23"
    assert green["programs"] == ["ILLICIT-DRUGS-EO14059", "SDGT"]
    assert green["details"]["linked"][0]["extId"] == "58017"
    ids = conn.execute(
        "select e.ext_id, i.id_type, i.id_value from watchlist_entry_ids i join watchlist_entries e on e.id = i.entry_id order by 1, 2"
    ).fetchall()
    assert {"ext_id": "58021", "id_type": "curp", "id_value": "CITL801109MMNSPL07"} in ids
    assert {"ext_id": "7156", "id_type": "rfc", "id_value": "DIB771110HQ1"} in ids
    assert len(_changes(conn, run_id)) == 10


def test_second_sync_without_changes_only_touches_last_seen(conn):
    _sync(conn, _records())
    before = conn.execute("select max(last_seen_at) t, max(changed_at) c from watchlist_entries").fetchone()
    run_id, counts = _sync(conn, _records())
    assert counts["unchanged"] == 10 and counts["added"] == counts["changed"] == counts["removed"] == 0
    after = conn.execute("select max(last_seen_at) t, max(changed_at) c from watchlist_entries").fetchone()
    assert after["t"] > before["t"] and after["c"] == before["c"]
    assert _changes(conn, run_id) == []


def test_new_changed_removed_and_restored_records(conn):
    records = _records()
    _sync(conn, records)

    nxt = [copy.deepcopy(r) for r in records if r["extId"] != "4238"]  # the vessel leaves the list
    cisneros = next(r for r in nxt if r["extId"] == "58021")
    cisneros["programs"].append("SDNTK")
    nxt[nxt.index(cisneros)] = ofac.finalize_record(cisneros)
    newcomer = copy.deepcopy(next(r for r in records if r["extId"] == "58022"))
    newcomer.update(extId="99999", name="Rancho San Miguel Los Tres Hermanos S.P.R. de R.L. de C.V.", partyType="organization")
    nxt.append(ofac.finalize_record(newcomer))

    run_id, counts = _sync(conn, nxt)
    assert (counts["added"], counts["changed"], counts["removed"], counts["restored"]) == (1, 1, 1, 0)
    assert _changes(conn, run_id) == [("4238", "removed"), ("58021", "changed"), ("99999", "added")]
    prev = conn.execute(
        "select c.previous from watchlist_entry_changes c join watchlist_entries e on e.id = c.entry_id "
        "where c.run_id = %s and e.ext_id = '58021'",
        (run_id,),
    ).fetchone()["previous"]
    assert prev["programs"] == ["ILLICIT-DRUGS-EO14059", "SDGT"]
    assert conn.execute("select removed_at from watchlist_entries where ext_id = '4238'").fetchone()["removed_at"]

    run_id, counts = _sync(conn, records + [ofac.finalize_record(newcomer)])
    assert counts["restored"] == 1 and counts["changed"] == 1
    assert ("4238", "restored") in _changes(conn, run_id)
    assert conn.execute("select removed_at from watchlist_entries where ext_id = '4238'").fetchone()["removed_at"] is None


def test_a_file_with_far_fewer_records_is_refused(conn):
    _sync(conn, _records())
    run_id = db.start_run(conn, "sync", "ofac", "manual")
    with pytest.raises(db.SyncRefused):
        db.apply_sync(conn, "ofac", _records()[:3], run_id)
    assert conn.execute("select count(*) n from watchlist_entries where removed_at is null").fetchone()["n"] == 10


def _fake_download(tmp_path):
    def download(name):
        src = FIXTURES / name.replace("_ADVANCED", "_SAMPLE")
        dst = tmp_path / name
        shutil.copy(src, dst)
        data = dst.read_bytes()
        return {"path": str(dst), "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data), "lastModified": "x"}

    return download


def test_handler_applies_then_skips_an_unchanged_publication(conn, monkeypatch, tmp_path):
    monkeypatch.setattr(sync, "_download", _fake_download(tmp_path))
    monkeypatch.setattr(sync.db, "connect", lambda **_: psycopg.connect(CONNINFO, row_factory=dict_row, autocommit=True))
    notified: list[str] = []
    monkeypatch.setattr(sync, "_notify_failure", notified.append)

    first = sync.handler({}, None)
    assert first["status"] == "ok" and first["counts"]["added"] == 10 and first["publishedOn"] == "2026-10-19"  # newest of SDN (15 Oct) and Consolidated (19 Oct)
    second = sync.handler({}, None)
    assert second["status"] == "unchanged"
    forced = sync.handler({"force": True, "trigger": "manual"}, None)
    assert forced["status"] == "ok" and forced["counts"]["unchanged"] == 10
    runs = db.list_runs(conn, kind="sync")
    assert [r["status"] for r in runs] == ["ok", "unchanged", "ok"]
    assert runs[0]["trigger"] == "manual" and runs[2]["files"]["SDN_ADVANCED.XML"]["parties"] == 9
    assert notified == []


def test_handler_records_and_reports_a_failure(conn, monkeypatch, tmp_path):
    def broken(name):
        raise RuntimeError("HTTP Error 503")

    monkeypatch.setattr(sync, "_download", broken)
    monkeypatch.setattr(sync.db, "connect", lambda **_: psycopg.connect(CONNINFO, row_factory=dict_row, autocommit=True))
    notified: list[str] = []
    monkeypatch.setattr(sync, "_notify_failure", notified.append)
    out = sync.handler({}, None)
    assert out["status"] == "failed" and "503" in out["error"]
    assert db.list_runs(conn)[0]["status"] == "failed"
    assert notified and "503" in notified[0]


# --- API (handler_watchlist_api) -------------------------------------------------------------------

import json  # noqa: E402
from types import ModuleType  # noqa: E402

try:
    import nuwa_api_auth  # noqa: F401,E402
except ImportError:  # the Lambda bundle carries Linux-only cryptography wheels; the tests stub the JWT
    _auth = ModuleType("nuwa_api_auth")
    _auth.require_jwt = lambda event: "stub"
    sys.modules["nuwa_api_auth"] = _auth

import handler_watchlist_api as api  # noqa: E402


def _call(monkeypatch, path, body, role="analyst"):
    monkeypatch.setattr(api, "ensure_data_backend", lambda: None)
    monkeypatch.setattr(api, "require_jwt", lambda event: {"sub": "1", "cid": "8", "role": role})
    monkeypatch.setattr(api.db, "connect", lambda **_: psycopg.connect(CONNINFO, row_factory=dict_row, autocommit=True))
    out = api.handler({"httpMethod": "POST", "path": f"/prod/v1/watchlist/{path}", "body": json.dumps(body)}, None)
    return out["statusCode"], json.loads(out["body"])


def test_api_search_get_changes_and_runs(conn, monkeypatch):
    run_id, _ = _sync(conn, _records())
    api._INDEX.update(key=None, index=None)

    status, out = _call(monkeypatch, "search", {"name": "Green Agropacific", "partyType": "organization"})
    assert status == 200 and out["total"] == 1
    hit = out["matches"][0]
    assert (hit["extId"], hit["matchedBy"], hit["severity"], hit["designatedOn"]) == ("58020", "name", "high", "2026-07-23")

    status, out = _call(monkeypatch, "search", {"name": "Liliana Cisneros Tapia", "partyType": "individual"})
    assert status == 200 and out["total"] == 0
    status, out = _call(
        monkeypatch, "search", {"name": "Liliana Cisneros Tapia", "partyType": "individual", "curp": "CITL801109MMNSPL07"}
    )
    assert out["matches"][0]["matchedBy"] == "curp"

    status, out = _call(monkeypatch, "entries/get", {"extId": "58021"})
    assert status == 200 and out["entry"]["details"]["ids"][0]["value"] == "CITL801109MMNSPL07"
    assert out["history"][0]["change"] == "added"

    assert _call(monkeypatch, "changes", {})[0] == 403
    assert _call(monkeypatch, "runs", {})[0] == 403
    status, out = _call(monkeypatch, "changes", {}, role="super_admin")
    assert status == 200 and out["runId"] == run_id and len(out["changes"]) == 10
    status, out = _call(monkeypatch, "changes", {"change": "removed"}, role="super_admin")
    assert out["changes"] == []
    status, out = _call(monkeypatch, "runs", {"kind": "sync"}, role="super_admin")
    assert status == 200 and out["runs"][0]["runId"] == run_id

    assert _call(monkeypatch, "search", {"partyType": "organization"})[0] == 400
