"""
Integration tests for source_import_db.apply_records against a real PostgreSQL.

Skipped unless NUWA_TEST_PG is set to a libpq conninfo of a throwaway database, e.g.:
  docker run -d --rm --name nuwa-import-test -e POSTGRES_PASSWORD=test -p 55432:5432 postgres:16-alpine
  NUWA_TEST_PG="host=localhost port=55432 user=postgres password=test dbname=postgres" pytest tests/test_source_import_db.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "cdk" / "lambdas"))

psycopg = pytest.importorskip("psycopg")
from psycopg.rows import dict_row  # noqa: E402

import source_import_core as core  # noqa: E402
import source_import_db as db  # noqa: E402

CONNINFO = os.environ.get("NUWA_TEST_PG")
pytestmark = pytest.mark.skipif(not CONNINFO, reason="NUWA_TEST_PG not set")

BASE_SCHEMA = """
drop schema public cascade; create schema public;
create table public.sources (id bigserial primary key, name text, risk_level smallint, visibility text,
  client_id integer, metadata jsonb, created_at timestamptz default now(), updated_at timestamptz default now());
create table public.risk_entity_chunks (id uuid primary key default gen_random_uuid(), client_id integer not null,
  risk_level smallint not null, source_id bigint not null references public.sources(id) on delete cascade,
  entity_type text not null, chunk_text text not null, visibility text not null default 'private',
  created_at timestamptz not null default now(), updated_at timestamptz not null default now(),
  chunk_text_normalized text);
"""


@pytest.fixture()
def conn():
    c = psycopg.connect(CONNINFO, row_factory=dict_row, autocommit=True)
    c.execute(BASE_SCHEMA)
    c.execute((ROOT / "supabase" / "migrations" / "20261018000000_source_imports.sql").read_text())
    yield c
    c.close()


def _source(conn) -> dict:
    return conn.execute(
        "insert into public.sources (name, risk_level, visibility, client_id, metadata) values ('SAT', 3, 'public', 1, '{}') returning *"
    ).fetchone()


def _import(conn, sid: int, mode: str) -> str:
    return str(
        conn.execute(
            "insert into public.source_imports (source_id, client_id, user_id, mode, s3_key, file_name) "
            "values (%s, 1, 1, %s, 'k', 'f.csv') returning id",
            (sid, mode),
        ).fetchone()["id"]
    )


def _load_old_style(conn, sid: int, objs: list[dict]) -> None:
    """Rows as the old browser upload stored them: no record_key / row_hash."""
    from chunk_normalize import prepare_chunk_text_for_storage

    for o in objs:
        t, n = prepare_chunk_text_for_storage(json.dumps(o, ensure_ascii=False))
        conn.execute(
            "insert into public.risk_entity_chunks (client_id, risk_level, source_id, entity_type, chunk_text, chunk_text_normalized, visibility) "
            "values (1, 3, %s, 'company', %s, %s, 'public')",
            (sid, t, n),
        )


def _apply(conn, source, mode, rows, key_field="id"):
    headers = list(rows[0].keys())
    mapping = {h: ("id" if h == "RFC" else "nombre" if h == "Nombre" else f"custom:{h}") for h in headers}
    built = core.build_records(headers, rows, mapping, key_field)
    # Same connection setup as the worker: a read first, then apply_records on an autocommit connection.
    work = psycopg.connect(CONNINFO, row_factory=dict_row, autocommit=True)
    try:
        src = db.fetch_source(work, int(source["id"]))
        return db.apply_records(
            work, source=src, import_id=_import(conn, int(source["id"]), mode), mode=mode, records=built.records,
            key_field=key_field, risk_level=3, visibility="public", entity_type="company", metadata_updates={},
        )
    finally:
        work.close()


def _current(conn, sid):
    return {
        r["record_key"]: json.loads(r["chunk_text"])
        for r in conn.execute("select record_key, chunk_text from public.risk_entity_chunks where source_id=%s", (sid,))
    }


def test_sync_against_an_old_doubled_source(conn):
    s = _source(conn)
    sid = int(s["id"])
    # July and October copies of the same RFC (old uploads), plus one RFC only in July.
    _load_old_style(conn, sid, [
        {"id": "A1", "nombre": "ACME", "Monto": "773", "Fecha": "1/1/16"},
        {"id": "B2", "nombre": "BETA", "Monto": "10", "Fecha": "01/02/2016"},
        {"id": "C3", "nombre": "RETIRADO", "Monto": "1", "Fecha": "01/02/2016"},
    ])
    conn.execute("update public.risk_entity_chunks set created_at = now() - interval '90 days'")
    _load_old_style(conn, sid, [{"id": "A1", "nombre": "ACME", "Monto": "$773.00", "Fecha": "01/02/2016"}])

    counts = _apply(conn, s, "sync", [
        {"RFC": "A1", "Nombre": "ACME", "Monto": "$773.00", "Fecha": "01/02/2016"},   # same as October
        {"RFC": "B2", "Nombre": "BETA", "Monto": "$12.00", "Fecha": "01/02/2016"},    # amount changed
        {"RFC": "D4", "Nombre": "NUEVA", "Monto": "5", "Fecha": "03/03/2026"},        # new
    ])
    assert counts["superseded"] == 1   # July copy of A1 → history
    assert counts["unchanged"] == 1    # A1 (October copy)
    assert counts["updated"] == 1      # B2
    assert counts["inserted"] == 1     # D4
    assert counts["removed"] == 1      # C3 not in the file
    assert counts["rowCount"] == 3
    cur = _current(conn, sid)
    assert set(cur) == {"A1", "B2", "D4"} and cur["B2"]["Monto"] == "$12.00"
    hist = conn.execute(
        "select record_key, change, chunk_text from public.risk_entity_chunk_versions where source_id=%s order by record_key, change",
        (sid,),
    ).fetchall()
    assert [(h["record_key"], h["change"]) for h in hist] == [("A1", "superseded"), ("B2", "updated"), ("C3", "removed")]
    assert json.loads(hist[1]["chunk_text"])["Monto"] == "10"
    meta = conn.execute("select metadata from public.sources where id=%s", (sid,)).fetchone()["metadata"]
    assert meta["row_count"] == "3" and meta["recordKeyField"] == "id"


def test_reupload_same_file_changes_nothing(conn):
    s = _source(conn)
    rows = [{"RFC": "A1", "Nombre": "ACME", "Monto": "1"}, {"RFC": "B2", "Nombre": "BETA", "Monto": "2"}]
    _apply(conn, s, "sync", rows)
    again = _apply(conn, s, "sync", rows)
    assert (again["inserted"], again["updated"], again["removed"], again["unchanged"]) == (0, 0, 0, 2)


def test_incremental_never_removes(conn):
    s = _source(conn)
    _apply(conn, s, "sync", [{"RFC": "A1", "Nombre": "ACME", "Monto": "1"}, {"RFC": "B2", "Nombre": "BETA", "Monto": "2"}])
    c = _apply(conn, s, "incremental", [{"RFC": "C3", "Nombre": "GAMA", "Monto": "3"}, {"RFC": "A1", "Nombre": "ACME", "Monto": "9"}])
    assert (c["inserted"], c["updated"], c["removed"]) == (1, 1, 0)
    assert set(_current(conn, int(s["id"]))) == {"A1", "B2", "C3"}


def test_replace_deletes_everything_first(conn):
    s = _source(conn)
    _apply(conn, s, "sync", [{"RFC": "A1", "Nombre": "ACME", "Monto": "1"}])
    c = _apply(conn, s, "replace", [{"RFC": "Z9", "Nombre": "ZETA", "Monto": "1"}])
    assert (c["removed"], c["inserted"]) == (1, 1)
    assert set(_current(conn, int(s["id"]))) == {"Z9"}


def test_refuses_a_connection_with_an_open_transaction(conn):
    s = _source(conn)
    work = psycopg.connect(CONNINFO, row_factory=dict_row)  # autocommit off, like the first worker version
    try:
        with pytest.raises(RuntimeError):
            db.apply_records(work, source=s, import_id=_import(conn, int(s["id"]), "sync"), mode="sync", records=[],
                             key_field="id", risk_level=3, visibility="public", entity_type="company", metadata_updates={})
    finally:
        work.close()


def test_source_without_key_dedupes_exact_rows(conn):
    s = _source(conn)
    rows = [{"Nombre": "ACME", "Expediente": "1/2025"}, {"Nombre": "ACME", "Expediente": "3/2025"}]
    _apply(conn, s, "sync", rows, key_field=None)
    again = _apply(conn, s, "sync", rows + [rows[0]], key_field=None)
    assert (again["inserted"], again["unchanged"], again["removed"]) == (0, 2, 0)
