"""Saldo, historial y cobro de tokens del tenant (clients + token_ledger)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import psycopg.errors

from nuwa_errors import SupabaseRestError
from nuwa_pg_dispatch import _conn

TOKEN_ACTIONS = frozenset({"screening", "background_check", "monitoring", "rescreen", "other"})

_USAGE_COLUMN = {
    "screening": "screenings",
    "rescreen": "screenings",
    "background_check": "background_checks",
    "monitoring": "monitoring",
}


def resolve_token_client_id(actor: dict[str, Any], body: dict[str, Any]) -> int:
    """Own company, unless super_admin sends targetClientId."""
    raw = body.get("targetClientId")
    if raw is not None and str(raw).strip() != "":
        try:
            target = int(raw)
        except (TypeError, ValueError) as e:
            raise SupabaseRestError(400, "targetClientId debe ser entero.") from e
        if target <= 0:
            raise SupabaseRestError(400, "targetClientId debe ser entero.")
        if actor.get("role_slug") != "super_admin" and target != int(actor["client_id"]):
            raise SupabaseRestError(403, "Sin permiso para ese cliente.")
        return target
    return int(actor["client_id"])


def _balance_from_row(row: dict[str, Any] | None) -> dict[str, int]:
    limit = int((row or {}).get("token_limit") or 0)
    used = int((row or {}).get("tokens_used") or 0)
    return {"limit": limit, "used": used, "remaining": max(0, limit - used)}


def _read_balance(conn: Any, client_id: int) -> dict[str, int]:
    row = conn.execute(
        """
        SELECT COALESCE(token_limit, 0)::int AS token_limit,
               COALESCE(tokens_used, 0)::int AS tokens_used
        FROM clients WHERE id = %s
        """,
        (client_id,),
    ).fetchone()
    return _balance_from_row(dict(row) if row else None)


def tokens_balance(actor: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    client_id = resolve_token_client_id(actor, body)
    with _conn() as conn:
        bal = _read_balance(conn, client_id)
    return {"success": True, **bal}


def _millis(value: Any) -> int:
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return int(parsed.timestamp() * 1000)
        except ValueError:
            return 0
    return 0


def tokens_ledger(actor: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    client_id = resolve_token_client_id(actor, body)
    try:
        limit = int(body.get("limit") or 50)
    except (TypeError, ValueError):
        limit = 50
    limit = min(200, max(1, limit))
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT id, cost, action, label, ref_folio, created_at
            FROM token_ledger
            WHERE client_id = %s
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (client_id, limit),
        ).fetchall()
    items: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        action = str(row.get("action") or "other")
        if action not in TOKEN_ACTIONS:
            action = "other"
        item: dict[str, Any] = {
            "id": str(row.get("id")),
            "cost": int(row.get("cost") or 0),
            "action": action,
            "label": str(row.get("label") or action),
            "timestamp": _millis(row.get("created_at")),
        }
        if row.get("ref_folio"):
            item["refFolio"] = str(row["ref_folio"])
        items.append(item)
    return {"success": True, "items": items}


def _bump_usage(conn: Any, client_id: int, action: str, cost: int) -> None:
    column = _USAGE_COLUMN.get(action)
    if not column:
        return
    conn.execute(
        f"""
        INSERT INTO client_token_usage (client_id, {column}, updated_at)
        VALUES (%s, %s, NOW())
        ON CONFLICT (client_id) DO UPDATE
        SET {column} = client_token_usage.{column} + EXCLUDED.{column},
            updated_at = NOW()
        """,
        (client_id, cost),
    )


def tokens_consume(actor: dict[str, Any], body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Idempotent charge. Same client_id + refFolio does not debit twice."""
    client_id = resolve_token_client_id(actor, body)
    try:
        cost = int(body.get("cost"))
    except (TypeError, ValueError):
        return 400, {"success": False, "code": "invalid_cost"}
    if cost <= 0:
        return 400, {"success": False, "code": "invalid_cost"}
    action = str(body.get("action") or "").strip()
    if action not in TOKEN_ACTIONS:
        return 400, {"success": False, "code": "invalid_action"}
    label = str(body.get("label") or action).strip()[:500]
    raw_ref = body.get("refFolio")
    ref_folio = str(raw_ref).strip()[:200] if raw_ref is not None and str(raw_ref).strip() else None
    user_id = int(actor["id"])

    with _conn() as conn:
        if ref_folio:
            existing = conn.execute(
                "SELECT id FROM token_ledger WHERE client_id = %s AND ref_folio = %s LIMIT 1",
                (client_id, ref_folio),
            ).fetchone()
            if existing:
                bal = _read_balance(conn, client_id)
                return 200, {"success": True, "ok": True, "alreadyCharged": True, **bal}

        conn.execute("SAVEPOINT token_charge")
        try:
            updated = conn.execute(
                """
                UPDATE clients
                SET tokens_used = tokens_used + %s, updated_at = NOW()
                WHERE id = %s AND tokens_used + %s <= token_limit
                RETURNING COALESCE(token_limit, 0)::int AS token_limit,
                          COALESCE(tokens_used, 0)::int AS tokens_used
                """,
                (cost, client_id, cost),
            ).fetchone()
            if not updated:
                conn.execute("ROLLBACK TO SAVEPOINT token_charge")
                bal = _read_balance(conn, client_id)
                return 409, {"success": False, "code": "insufficient_tokens", **bal}
            conn.execute(
                """
                INSERT INTO token_ledger (client_id, user_id, cost, action, label, ref_folio)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (client_id, user_id, cost, action, label, ref_folio),
            )
            _bump_usage(conn, client_id, action, cost)
            conn.execute("RELEASE SAVEPOINT token_charge")
            conn.commit()
            return 200, {"success": True, "ok": True, "alreadyCharged": False, **_balance_from_row(dict(updated))}
        except psycopg.errors.UniqueViolation:
            conn.execute("ROLLBACK TO SAVEPOINT token_charge")
            bal = _read_balance(conn, client_id)
            return 200, {"success": True, "ok": True, "alreadyCharged": True, **bal}
