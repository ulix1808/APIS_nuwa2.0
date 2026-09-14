"""Tenant audit events — PostgreSQL (numeric client_id)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from psycopg.types.json import Json

from nuwa_pg_dispatch import _conn

_ENSURED = False

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS nuwa_audit_events (
  id TEXT PRIMARY KEY,
  client_id INTEGER NOT NULL,
  user_id INTEGER,
  user_name TEXT NOT NULL DEFAULT '',
  user_role TEXT NOT NULL DEFAULT '',
  event_type TEXT NOT NULL,
  category TEXT NOT NULL,
  target TEXT NOT NULL DEFAULT '',
  target_id TEXT,
  details TEXT NOT NULL DEFAULT '',
  metadata JSONB,
  ip_address TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""

_CREATE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_nuwa_audit_client_created
  ON nuwa_audit_events (client_id, created_at DESC)
"""


def _ensure_table() -> None:
    global _ENSURED
    if _ENSURED:
        return
    with _conn() as conn:
        conn.execute(_CREATE_TABLE)
        conn.execute(_CREATE_INDEX)
    _ENSURED = True


def _iso(dt: Any) -> str:
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return str(dt or "")


def row_to_event(row: dict[str, Any]) -> dict[str, Any]:
    meta = row.get("metadata")
    if not isinstance(meta, dict):
        meta = {}
    user = str(row.get("resolved_name") or row.get("user_name") or "").strip()
    return {
        "id": str(row.get("id") or ""),
        "clientId": int(row["client_id"]),
        "type": str(row.get("event_type") or ""),
        "category": str(row.get("category") or ""),
        "timestamp": _iso(row.get("created_at")),
        "user": user,
        "userRole": str(row.get("user_role") or ""),
        "target": str(row.get("target") or ""),
        "targetId": str(row["target_id"]) if row.get("target_id") is not None else None,
        "details": str(row.get("details") or ""),
        "metadata": meta,
        "ip": str(row["ip_address"]) if row.get("ip_address") is not None else None,
    }


def insert_audit_event(payload: dict[str, Any]) -> dict[str, Any]:
    _ensure_table()
    client_id = int(payload["clientId"])
    event_type = str(payload.get("type") or "").strip()
    user_name = str(payload.get("userName") or payload.get("user") or "").strip()
    target = str(payload.get("target") or "").strip()
    event_id = str(payload.get("id") or f"AUD-{uuid.uuid4().hex[:12].upper()}")
    created_at = payload.get("timestamp") or datetime.now(timezone.utc)

    with _conn() as conn:
        if event_type in ("auth.login", "auth.logout"):
            existing = conn.execute(
                """
                SELECT id FROM nuwa_audit_events
                WHERE client_id = %s AND event_type = %s
                  AND created_at > NOW() - INTERVAL '5 minutes'
                  AND (target = %s OR user_name = %s OR target = %s OR user_name = %s)
                LIMIT 1
                """,
                (client_id, event_type, target, user_name, user_name, target),
            ).fetchone()
        else:
            existing = conn.execute(
                """
                SELECT id FROM nuwa_audit_events
                WHERE client_id = %s AND event_type = %s AND target = %s AND user_name = %s
                  AND created_at > NOW() - INTERVAL '2 minutes'
                LIMIT 1
                """,
                (client_id, event_type, target, user_name),
            ).fetchone()
        if existing:
            return {"id": str(existing["id"]), "duplicate": True}

        conn.execute(
            """
            INSERT INTO nuwa_audit_events (
              id, client_id, user_id, user_name, user_role, event_type, category,
              target, target_id, details, metadata, ip_address, created_at
            ) VALUES (
              %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT (id) DO NOTHING
            """,
            (
                event_id,
                client_id,
                payload.get("userId"),
                user_name,
                str(payload.get("userRole") or ""),
                event_type,
                str(payload.get("category") or ""),
                target,
                payload.get("targetId"),
                str(payload.get("details") or ""),
                Json(payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}),
                payload.get("ipAddress") or payload.get("ip"),
                created_at,
            ),
        )

    return {"id": event_id, "duplicate": False}


def list_audit_events(client_id: int, limit: int = 300) -> list[dict[str, Any]]:
    _ensure_table()
    safe_limit = max(1, min(int(limit), 1000))
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT a.*,
                   COALESCE(NULLIF(TRIM(u.full_name), ''), NULLIF(TRIM(u.email), ''), a.user_name)
                     AS resolved_name
            FROM nuwa_audit_events a
            LEFT JOIN nuwa_users u
              ON u.id = a.user_id AND u.client_id = a.client_id
            WHERE a.client_id = %s
            ORDER BY a.created_at DESC
            LIMIT %s
            """,
            (client_id, safe_limit),
        ).fetchall() or []
    return [row_to_event(dict(row)) for row in rows]
