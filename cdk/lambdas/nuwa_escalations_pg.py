"""Tenant compliance escalations — PostgreSQL (numeric client_id)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from psycopg.types.json import Json

from nuwa_errors import SupabaseRestError
from nuwa_pg_dispatch import _conn

_ENSURED = False

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS nuwa_escalations (
  id            TEXT PRIMARY KEY,
  client_id     INTEGER NOT NULL,
  entity_name   TEXT NOT NULL,
  entity_id     TEXT,
  client_name   TEXT,
  context       TEXT NOT NULL DEFAULT 'screening',
  risk_level    TEXT NOT NULL DEFAULT 'high',
  actions       JSONB NOT NULL DEFAULT '[]'::jsonb,
  notes         TEXT NOT NULL DEFAULT '',
  priority      TEXT NOT NULL DEFAULT 'urgent',
  status        TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'resolved')),
  quick_resolve BOOLEAN NOT NULL DEFAULT false,
  report_id     TEXT,
  notify_email  TEXT,
  action_path   TEXT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  resolved_at   TIMESTAMPTZ,
  resolution    JSONB,
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""

_CREATE_INDEXES = [
    """
CREATE INDEX IF NOT EXISTS idx_nuwa_escalations_client_created
  ON nuwa_escalations (client_id, created_at DESC)
""",
    """
CREATE INDEX IF NOT EXISTS idx_nuwa_escalations_client_status
  ON nuwa_escalations (client_id, status)
""",
]


def _ensure_table() -> None:
    global _ENSURED
    if _ENSURED:
        return
    with _conn() as conn:
        conn.execute(_CREATE_TABLE)
        for sql in _CREATE_INDEXES:
            conn.execute(sql)
        conn.commit()
    _ENSURED = True


def _iso(dt: Any) -> str | None:
    if dt is None:
        return None
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    s = str(dt).strip()
    return s or None


def row_to_escalation(row: dict[str, Any]) -> dict[str, Any]:
    actions = row.get("actions")
    if not isinstance(actions, list):
        actions = []
    resolution = row.get("resolution")
    if resolution is not None and not isinstance(resolution, dict):
        resolution = None
    return {
        "id": str(row.get("id") or ""),
        "entityName": str(row.get("entity_name") or ""),
        "entityId": str(row["entity_id"]) if row.get("entity_id") is not None else None,
        "clientId": str(row.get("client_id") or ""),
        "clientName": str(row["client_name"]) if row.get("client_name") is not None else None,
        "context": str(row.get("context") or "screening"),
        "riskLevel": str(row.get("risk_level") or "high"),
        "actions": actions,
        "notes": str(row.get("notes") or ""),
        "priority": str(row.get("priority") or "urgent"),
        "status": str(row.get("status") or "active"),
        "quickResolve": bool(row.get("quick_resolve")),
        "reportId": str(row["report_id"]) if row.get("report_id") is not None else None,
        "notifyEmail": str(row["notify_email"]) if row.get("notify_email") is not None else None,
        "actionPath": str(row["action_path"]) if row.get("action_path") is not None else None,
        "createdAt": _iso(row.get("created_at")) or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "resolvedAt": _iso(row.get("resolved_at")),
        "resolution": resolution,
    }


def list_escalations(client_id: int, limit: int = 500) -> list[dict[str, Any]]:
    _ensure_table()
    lim = max(1, min(int(limit or 500), 500))
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM nuwa_escalations
            WHERE client_id = %s
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (int(client_id), lim),
        ).fetchall()
    return [row_to_escalation(dict(r)) for r in rows]


def get_escalation(client_id: int, escalation_id: str) -> dict[str, Any] | None:
    _ensure_table()
    with _conn() as conn:
        row = conn.execute(
            """
            SELECT * FROM nuwa_escalations
            WHERE client_id = %s AND id = %s
            LIMIT 1
            """,
            (int(client_id), str(escalation_id)),
        ).fetchone()
    return row_to_escalation(dict(row)) if row else None


def save_escalation(client_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    _ensure_table()
    esc_id = str(payload.get("id") or "").strip()
    entity_name = str(payload.get("entityName") or "").strip()
    if not esc_id or not entity_name:
        raise SupabaseRestError(400, "id y entityName requeridos.")

    actions = payload.get("actions") if isinstance(payload.get("actions"), list) else []
    resolution = payload.get("resolution") if isinstance(payload.get("resolution"), dict) else None
    status = str(payload.get("status") or "active").strip().lower()
    if status not in ("active", "resolved"):
        status = "active"

    row = {
        "id": esc_id,
        "clientId": str(client_id),
        "entityName": entity_name,
        "entityId": payload.get("entityId"),
        "clientName": payload.get("clientName"),
        "context": str(payload.get("context") or "screening"),
        "riskLevel": str(payload.get("riskLevel") or "high"),
        "actions": actions,
        "notes": str(payload.get("notes") or ""),
        "priority": str(payload.get("priority") or "urgent"),
        "status": status,
        "quickResolve": bool(payload.get("quickResolve")),
        "reportId": payload.get("reportId"),
        "notifyEmail": payload.get("notifyEmail"),
        "actionPath": payload.get("actionPath"),
        "createdAt": payload.get("createdAt")
        or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "resolvedAt": payload.get("resolvedAt"),
        "resolution": resolution,
    }

    with _conn() as conn:
        owned = conn.execute(
            "SELECT client_id FROM nuwa_escalations WHERE id = %s LIMIT 1",
            (esc_id,),
        ).fetchone()
        if owned and int(owned["client_id"]) != int(client_id):
            raise SupabaseRestError(403, "escalation_id pertenece a otro tenant.")
        conn.execute(
            """
            INSERT INTO nuwa_escalations (
              id, client_id, entity_name, entity_id, client_name, context, risk_level,
              actions, notes, priority, status, quick_resolve, report_id, notify_email,
              action_path, created_at, resolved_at, resolution, updated_at
            ) VALUES (
              %s,%s,%s,%s,%s,%s,%s,
              %s,%s,%s,%s,%s,%s,%s,
              %s,%s::timestamptz,%s::timestamptz,%s,NOW()
            )
            ON CONFLICT (id) DO UPDATE SET
              entity_name = EXCLUDED.entity_name,
              entity_id = EXCLUDED.entity_id,
              client_name = EXCLUDED.client_name,
              context = EXCLUDED.context,
              risk_level = EXCLUDED.risk_level,
              actions = EXCLUDED.actions,
              notes = EXCLUDED.notes,
              priority = EXCLUDED.priority,
              status = EXCLUDED.status,
              quick_resolve = EXCLUDED.quick_resolve,
              report_id = EXCLUDED.report_id,
              notify_email = EXCLUDED.notify_email,
              action_path = EXCLUDED.action_path,
              resolved_at = EXCLUDED.resolved_at,
              resolution = EXCLUDED.resolution,
              updated_at = NOW()
            WHERE nuwa_escalations.client_id = EXCLUDED.client_id
            """,
            (
                row["id"],
                int(client_id),
                row["entityName"],
                row["entityId"],
                row["clientName"],
                row["context"],
                row["riskLevel"],
                Json(row["actions"]),
                row["notes"],
                row["priority"],
                row["status"],
                row["quickResolve"],
                row["reportId"],
                row["notifyEmail"],
                row["actionPath"],
                row["createdAt"],
                row["resolvedAt"],
                Json(row["resolution"]) if row["resolution"] is not None else None,
            ),
        )
        conn.commit()
    return row


def resolve_escalation(
    client_id: int,
    escalation_id: str,
    resolution: dict[str, Any] | None,
) -> dict[str, Any] | None:
    _ensure_table()
    current = get_escalation(client_id, escalation_id)
    if not current:
        return None

    res = resolution if isinstance(resolution, dict) else {}
    resolved_at = str(res.get("resolvedAt") or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
    cancelled = set(res.get("cancelledActions") or [])
    actions = []
    for a in current.get("actions") or []:
        if not isinstance(a, dict):
            continue
        item = dict(a)
        if item.get("actionId") in cancelled:
            item["status"] = "cancelled"
        actions.append(item)

    next_row = {
        **current,
        "status": "resolved",
        "resolvedAt": resolved_at,
        "resolution": {
            "reason": str(res.get("reason") or ""),
            "reasonLabel": str(res.get("reasonLabel") or ""),
            "justification": str(res.get("justification") or ""),
            "cancelledActions": list(cancelled),
            "resolvedBy": str(res.get("resolvedBy") or ""),
            "resolvedAt": resolved_at,
        },
        "actions": actions,
    }

    with _conn() as conn:
        conn.execute(
            """
            UPDATE nuwa_escalations SET
              status = 'resolved',
              resolved_at = %s::timestamptz,
              resolution = %s,
              actions = %s,
              updated_at = NOW()
            WHERE client_id = %s AND id = %s
            """,
            (
                resolved_at,
                Json(next_row["resolution"]),
                Json(actions),
                int(client_id),
                str(escalation_id),
            ),
        )
        conn.commit()
    return next_row
