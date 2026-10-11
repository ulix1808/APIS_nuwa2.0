"""Entity relations (subject ↔ related entity per report) — PostgreSQL.

Rows are computed by the BFF and replaced per report folio; this module never derives
relations from report_json itself. Table: supabase/migrations/20261011000000_entity_relations.sql.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.types.json import Json

from nuwa_obs_log import log_phase
from nuwa_pg_dispatch import _conn

MAX_ROWS_PER_REPORT = 300
MAX_KEYS = 16
MAX_QUERY_KEYS = 64
MAX_QUERY_LIMIT = 2000

_COLUMNS = (
    "client_id",
    "report_folio",
    "status",
    "subject_entity_id",
    "subject_name",
    "subject_key",
    "subject_aliases",
    "subject_alias_keys",
    "subject_party_type",
    "related_entity_id",
    "related_name",
    "related_key",
    "related_aliases",
    "related_alias_keys",
    "related_party_type",
    "relation_type",
    "relationship",
    "risk_level",
    "confidence",
    "source_type",
    "source_id",
    "source_url",
    "source_title",
    "source_excerpt",
    "report_created_at",
)


def _text(value: Any, max_len: int) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s[:max_len] if s else None


def _str_list(value: Any, max_items: int = MAX_KEYS, max_len: int = 200) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        s = _text(item, max_len)
        if s and s not in out:
            out.append(s)
        if len(out) >= max_items:
            break
    return out


def _int_or_none(value: Any) -> int | None:
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def _iso(dt: Any) -> str | None:
    if dt is None:
        return None
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat().replace("+00:00", "Z")
    return str(dt)


def normalize_relation_rows(
    rows: Any,
    *,
    client_id: int,
    folio: str,
    subject_entity_id: str | None = None,
    report_created_at: str | None = None,
) -> list[dict[str, Any]]:
    """Validates BFF rows; drops rows without the required name/key fields."""
    if not isinstance(rows, list):
        return []
    out: list[dict[str, Any]] = []
    for raw in rows[:MAX_ROWS_PER_REPORT]:
        if not isinstance(raw, dict):
            continue
        subject_name = _text(raw.get("subjectName"), 300)
        subject_key = _text(raw.get("subjectKey"), 300)
        related_name = _text(raw.get("relatedName"), 300)
        related_key = _text(raw.get("relatedKey"), 300)
        if not (subject_name and subject_key and related_name and related_key):
            continue
        out.append(
            {
                "client_id": client_id,
                "report_folio": folio,
                "status": "active",
                "subject_entity_id": _text(raw.get("subjectEntityId"), 100) or subject_entity_id,
                "subject_name": subject_name,
                "subject_key": subject_key,
                "subject_aliases": Json(_str_list(raw.get("subjectAliases"))),
                "subject_alias_keys": _str_list(raw.get("subjectAliasKeys")),
                "subject_party_type": _text(raw.get("subjectPartyType"), 20),
                "related_entity_id": _text(raw.get("relatedEntityId"), 100),
                "related_name": related_name,
                "related_key": related_key,
                "related_aliases": Json(_str_list(raw.get("relatedAliases"))),
                "related_alias_keys": _str_list(raw.get("relatedAliasKeys")),
                "related_party_type": _text(raw.get("relatedPartyType"), 20),
                "relation_type": _text(raw.get("relationType"), 50),
                "relationship": _text(raw.get("relationship"), 300),
                "risk_level": _text(raw.get("riskLevel"), 20),
                "confidence": _int_or_none(raw.get("confidence")),
                "source_type": _text(raw.get("sourceType"), 20),
                "source_id": _text(raw.get("sourceId"), 120),
                "source_url": _text(raw.get("sourceUrl"), 1000),
                "source_title": _text(raw.get("sourceTitle"), 500),
                "source_excerpt": _text(raw.get("sourceExcerpt"), 1500),
                "report_created_at": _text(raw.get("reportCreatedAt"), 40) or report_created_at,
            }
        )
    return out


def replace_report_relations(
    client_id: int,
    folio: str,
    rows: Any,
    *,
    subject_entity_id: str | None = None,
    report_created_at: str | None = None,
) -> int:
    """Replaces the relations of one report in a single transaction. Returns rows written,
    or -1 when the table is missing (migration not applied yet)."""
    clean = normalize_relation_rows(
        rows,
        client_id=client_id,
        folio=folio,
        subject_entity_id=subject_entity_id,
        report_created_at=report_created_at,
    )
    placeholders = ", ".join(["%s"] * len(_COLUMNS))
    insert_sql = f"INSERT INTO entity_relations ({', '.join(_COLUMNS)}) VALUES ({placeholders})"
    try:
        with _conn() as conn:
            with conn.transaction():
                conn.execute(
                    "DELETE FROM entity_relations WHERE client_id = %s AND report_folio = %s",
                    (client_id, folio),
                )
                if clean:
                    with conn.cursor() as cur:
                        cur.executemany(insert_sql, [tuple(r[c] for c in _COLUMNS) for r in clean])
    except psycopg.errors.UndefinedTable:
        log_phase("relations_sync", "entity_relations table missing (apply migration)")
        return -1
    log_phase("relations_sync", f"client={client_id} folio={folio} rows={len(clean)}")
    return len(clean)


def mark_report_relations_deleted(client_id: int, folio: str) -> None:
    try:
        with _conn() as conn:
            conn.execute(
                "UPDATE entity_relations SET status = 'deleted' WHERE client_id = %s AND report_folio = %s",
                (client_id, folio),
            )
            conn.commit()
    except psycopg.errors.UndefinedTable:
        log_phase("relations_delete", "entity_relations table missing (apply migration)")


def _row_to_api(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "reportFolio": row.get("report_folio"),
        "subjectEntityId": row.get("subject_entity_id"),
        "subjectName": row.get("subject_name"),
        "subjectKey": row.get("subject_key"),
        "subjectAliases": row.get("subject_aliases") or [],
        "subjectPartyType": row.get("subject_party_type"),
        "relatedEntityId": row.get("related_entity_id"),
        "relatedName": row.get("related_name"),
        "relatedKey": row.get("related_key"),
        "relatedAliases": row.get("related_aliases") or [],
        "relatedPartyType": row.get("related_party_type"),
        "relationType": row.get("relation_type"),
        "relationship": row.get("relationship"),
        "riskLevel": row.get("risk_level"),
        "confidence": row.get("confidence"),
        "sourceType": row.get("source_type"),
        "sourceId": row.get("source_id"),
        "sourceUrl": row.get("source_url"),
        "sourceTitle": row.get("source_title"),
        "sourceExcerpt": row.get("source_excerpt"),
        "reportCreatedAt": _iso(row.get("report_created_at")),
    }


def query_relations(
    client_id: int,
    *,
    keys: Any = None,
    entity_ids: Any = None,
    limit: int = 500,
) -> list[dict[str, Any]] | None:
    """Active relations where the given names/aliases/entities appear on either side
    (outgoing: as subject; incoming: as related entity). None when the table is missing."""
    key_list = _str_list(keys, max_items=MAX_QUERY_KEYS, max_len=300)
    id_list = _str_list(entity_ids, max_items=MAX_QUERY_KEYS, max_len=100)
    if not key_list and not id_list:
        return []
    limit = max(1, min(int(limit or 500), MAX_QUERY_LIMIT))
    sql = """
SELECT * FROM entity_relations
WHERE client_id = %(client_id)s AND status = 'active'
  AND (
    subject_key = ANY(%(keys)s) OR related_key = ANY(%(keys)s)
    OR subject_alias_keys && %(keys)s OR related_alias_keys && %(keys)s
    OR subject_entity_id = ANY(%(ids)s) OR related_entity_id = ANY(%(ids)s)
  )
ORDER BY report_created_at DESC NULLS LAST, id DESC
LIMIT %(limit)s
"""
    try:
        with _conn() as conn:
            rows = conn.execute(
                sql, {"client_id": client_id, "keys": key_list, "ids": id_list, "limit": limit}
            ).fetchall()
    except psycopg.errors.UndefinedTable:
        log_phase("relations_query", "entity_relations table missing (apply migration)")
        return None
    return [_row_to_api(dict(r)) for r in rows]


def report_belongs_to_client(client_id: int, folio: str) -> bool:
    with _conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM reports WHERE client_id = %s AND folio = %s AND status = 'active' LIMIT 1",
            (client_id, folio),
        ).fetchone()
    return row is not None
