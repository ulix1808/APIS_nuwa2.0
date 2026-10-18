"""
Database side of background source imports (public.source_imports, risk_entity_chunks, versions).

apply_records() writes one import in a single transaction, so screenings never see a half-loaded
source:
- sync: the file is the whole list. New keys are inserted, changed ones updated (previous text to
  risk_entity_chunk_versions as 'updated'), keys not in the file removed (to versions as 'removed').
- incremental: the file brings new or changed records; nothing is removed.
- replace: delete everything and insert the file (the old behaviour, no history).
Rows loaded before record keys existed get them the first time (several rows with one key: the newest
stays, the others go to versions as 'superseded').
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any, Callable

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json

from nuwa_config import get_database_config
from source_import_core import Record, key_and_hash_of_stored

IMPORT_COLUMNS = (
    "id, source_id, client_id, user_id, status, mode, s3_key, file_name, file_size, content_type, config, "
    "progress, result, error, created_at, started_at, finished_at"
)


def connect(*, autocommit: bool = False) -> psycopg.Connection:
    cfg = get_database_config()
    return psycopg.connect(
        f"host={cfg['host']} port={cfg['port']} dbname={cfg['dbname']} user={cfg['user']} "
        f"password={cfg['password']} sslmode={cfg['sslmode']}",
        row_factory=dict_row,
        autocommit=autocommit,
    )


def _jsonable(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out: dict[str, Any] = {}
    for k, v in row.items():
        if isinstance(v, (dt.datetime, dt.date)):
            v = v.isoformat()
        elif k == "id" or k.endswith("_id") and not isinstance(v, int):
            v = str(v) if v is not None else None
        out[k] = v
    return out


def to_api(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """snake_case row → camelCase API object."""
    r = _jsonable(row)
    if r is None:
        return None
    return {
        "importId": r["id"],
        "sourceId": r["source_id"],
        "clientId": r["client_id"],
        "userId": r["user_id"],
        "status": r["status"],
        "mode": r["mode"],
        "fileName": r["file_name"],
        "fileSize": r["file_size"],
        "config": r["config"],
        "progress": r["progress"],
        "result": r["result"],
        "error": r["error"],
        "createdAt": r["created_at"],
        "startedAt": r["started_at"],
        "finishedAt": r["finished_at"],
    }


def fetch_source(conn: psycopg.Connection, source_id: int) -> dict[str, Any] | None:
    return conn.execute(
        "select id, client_id, name, risk_level, visibility, metadata from public.sources where id=%s", (source_id,)
    ).fetchone()


def create_import(conn: psycopg.Connection, **f: Any) -> dict[str, Any]:
    return conn.execute(
        f"""insert into public.source_imports
            (id, source_id, client_id, user_id, mode, s3_key, file_name, file_size, content_type, config)
            values (%(id)s, %(source_id)s, %(client_id)s, %(user_id)s, %(mode)s, %(s3_key)s, %(file_name)s,
                    %(file_size)s, %(content_type)s, %(config)s)
            returning {IMPORT_COLUMNS}""",
        {**f, "config": Json(f.get("config") or {})},
    ).fetchone()


def get_import(conn: psycopg.Connection, import_id: str) -> dict[str, Any] | None:
    return conn.execute(f"select {IMPORT_COLUMNS} from public.source_imports where id=%s", (import_id,)).fetchone()


def list_imports(conn: psycopg.Connection, *, client_id: int | None, source_id: int | None, limit: int) -> list[dict[str, Any]]:
    where, args = [], []
    if client_id is not None:
        where.append("client_id=%s")
        args.append(client_id)
    if source_id is not None:
        where.append("source_id=%s")
        args.append(source_id)
    sql = f"select {IMPORT_COLUMNS} from public.source_imports"
    if where:
        sql += " where " + " and ".join(where)
    sql += " order by created_at desc limit %s"
    return conn.execute(sql, (*args, limit)).fetchall()


def claim_import(conn: psycopg.Connection, import_id: str) -> dict[str, Any] | None:
    """queued → processing, atomically: a second (retried) invocation gets None and does nothing."""
    return conn.execute(
        f"""update public.source_imports set status='processing', started_at=now(), updated_at=now()
            where id=%s and status='queued' returning {IMPORT_COLUMNS}""",
        (import_id,),
    ).fetchone()


def set_status(conn: psycopg.Connection, import_id: str, status: str, **extra: Any) -> None:
    sets = ["status=%(status)s", "updated_at=now()"]
    params: dict[str, Any] = {"id": import_id, "status": status}
    for k, v in extra.items():
        sets.append(f"{k}=%({k})s")
        params[k] = Json(v) if isinstance(v, (dict, list)) else v
    if status == "processing":
        sets.append("started_at=coalesce(started_at, now())")
    if status in ("completed", "failed"):
        sets.append("finished_at=now()")
    conn.execute(f"update public.source_imports set {', '.join(sets)} where id=%(id)s", params)


def _copy(conn: psycopg.Connection, table_sql: str, columns: str, rows) -> None:
    with conn.cursor().copy(f"copy {table_sql} ({columns}) from stdin") as cp:
        for r in rows:
            cp.write_row(r)


def apply_records(
    conn: psycopg.Connection,
    *,
    source: dict[str, Any],
    import_id: str,
    mode: str,
    records: list[Record],
    key_field: str | None,
    risk_level: int,
    visibility: str,
    entity_type: str,
    metadata_updates: dict[str, str],
    progress: Callable[[str], None] = lambda _m: None,
) -> dict[str, int]:
    """Writes the import in one transaction. Returns counts (inserted, updated, unchanged, removed…)."""
    sid = int(source["id"])
    client_id = int(source["client_id"])
    counts = {"inserted": 0, "updated": 0, "unchanged": 0, "removed": 0, "superseded": 0, "keysAssigned": 0}
    meta = source.get("metadata") or {}
    prev_key_field = meta.get("recordKeyField") if isinstance(meta, dict) else None

    with conn.transaction():
        # One import at a time per source.
        conn.execute("select id from public.sources where id=%s for update", (sid,))
        conn.execute("create temp table _new (record_key text, row_hash text, chunk_text text, n text) on commit drop")

        if mode == "replace":
            progress("Reemplazando todos los registros")
            counts["removed"] = conn.execute("delete from public.risk_entity_chunks where source_id=%s", (sid,)).rowcount
            to_insert = records
        else:
            progress("Leyendo registros actuales de la fuente")
            existing = conn.execute(
                "select id, created_at, chunk_text, record_key, row_hash from public.risk_entity_chunks where source_id=%s",
                (sid,),
            ).fetchall()

            # 1) Keys for rows loaded before keys existed (or with another key field).
            recompute = prev_key_field != key_field
            fills = []
            for r in existing:
                if recompute or not r["record_key"] or not r["row_hash"]:
                    k, h = key_and_hash_of_stored(r["chunk_text"], key_field)
                    if (k, h) != (r["record_key"], r["row_hash"]):
                        fills.append((r["id"], k, h))
                    r["record_key"], r["row_hash"] = k, h
            if fills:
                progress(f"Asignando llave a {len(fills):,} registros existentes")
                conn.execute("create temp table _keys (id uuid primary key, k text, h text) on commit drop")
                _copy(conn, "_keys", "id, k, h", fills)
                conn.execute(
                    "update public.risk_entity_chunks c set record_key=_keys.k, row_hash=_keys.h from _keys where c.id=_keys.id"
                )
                counts["keysAssigned"] = len(fills)

            # Several current rows with one key: the newest stays, the others become history.
            current: dict[str, dict[str, Any]] = {}
            superseded = []
            for r in sorted(existing, key=lambda x: x["created_at"]):
                prev = current.get(r["record_key"])
                if prev is not None:
                    superseded.append(prev)
                current[r["record_key"]] = r

            incoming = {rec.key: rec for rec in records}
            to_insert = [rec for k, rec in incoming.items() if k not in current]
            to_update = [
                (current[k], rec) for k, rec in incoming.items() if k in current and current[k]["row_hash"] != rec.digest
            ]
            counts["unchanged"] = sum(1 for k, rec in incoming.items() if k in current and current[k]["row_hash"] == rec.digest)
            removed = [r for k, r in current.items() if k not in incoming] if mode == "sync" else []

            history = (
                [(r, "superseded") for r in superseded]
                + [(old, "updated") for old, _ in to_update]
                + [(r, "removed") for r in removed]
            )
            if history:
                progress(f"Guardando {len(history):,} versiones anteriores")
                _copy(
                    conn,
                    "public.risk_entity_chunk_versions",
                    "chunk_id, source_id, record_key, row_hash, chunk_text, change, valid_from, import_id",
                    (
                        (r["id"], sid, r["record_key"], r["row_hash"], r["chunk_text"], change, r["created_at"], import_id)
                        for r, change in history
                    ),
                )
            gone = [r["id"] for r in superseded] + [r["id"] for r in removed]
            if gone:
                conn.execute("create temp table _gone (id uuid primary key) on commit drop")
                _copy(conn, "_gone", "id", ((i,) for i in gone))
                conn.execute("delete from public.risk_entity_chunks c using _gone where c.id=_gone.id")
            counts["superseded"] = len(superseded)
            counts["removed"] = len(removed)

            if to_update:
                progress(f"Actualizando {len(to_update):,} registros que cambiaron")
                conn.execute("create temp table _upd (id uuid primary key, t text, n text, h text) on commit drop")
                _copy(conn, "_upd", "id, t, n, h", ((old["id"], rec.chunk_text, rec.normalized, rec.digest) for old, rec in to_update))
                conn.execute(
                    """update public.risk_entity_chunks c
                       set chunk_text=_upd.t, chunk_text_normalized=_upd.n, row_hash=_upd.h,
                           risk_level=%s, visibility=%s, last_import_id=%s
                       from _upd where c.id=_upd.id""",
                    (risk_level, visibility, import_id),
                )
            counts["updated"] = len(to_update)

        if to_insert:
            progress(f"Insertando {len(to_insert):,} registros nuevos")
            _copy(
                conn,
                "public.risk_entity_chunks",
                "client_id, risk_level, source_id, entity_type, chunk_text, chunk_text_normalized, visibility, "
                "record_key, row_hash, last_import_id",
                (
                    (client_id, risk_level, sid, entity_type, rec.chunk_text, rec.normalized or None, visibility, rec.key, rec.digest, import_id)
                    for rec in to_insert
                ),
            )
        counts["inserted"] = len(to_insert)

        total = conn.execute("select count(*) as n from public.risk_entity_chunks where source_id=%s", (sid,)).fetchone()["n"]
        updates = {**metadata_updates, "row_count": str(total), "recordKeyField": key_field or ""}
        conn.execute(
            "update public.sources set metadata = coalesce(metadata,'{}'::jsonb) || %s::jsonb, updated_at=now() where id=%s",
            (json.dumps(updates, ensure_ascii=False), sid),
        )
        counts["rowCount"] = int(total)
    return counts
