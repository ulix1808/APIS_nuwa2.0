"""
Early-warning lists API (own API Gateway, separate from screening). JWT as the rest of Nuwa.

POST /v1/watchlist/search        {name, partyType: individual|organization|unknown, rfc?, curp?, otherIds?: [{type, value}], limit?}
                                 → OFAC records that match with the early-warning rules (any signed-in user)
POST /v1/watchlist/entries/get   {extId, listCode?: 'ofac'} → one record with documents, relations and history
POST /v1/watchlist/changes       {runId? | since?, change?, limit?} → delta of the syncs (super admin)
POST /v1/watchlist/runs          {kind?: sync|match, limit?} → last runs (super admin)
POST /v1/watchlist/sync          {force?} → starts a sync now, asynchronous (super admin)
"""

from __future__ import annotations

import datetime as dt
import json
import os
from typing import Any

import watchlist_db as db
import watchlist_index as wi
from nuwa_api_auth import require_jwt
from nuwa_config import DatabaseConfigError, SupabaseConfigError, ensure_data_backend
from nuwa_http import CORS_HEADERS
from nuwa_obs_log import log_handler_enter, log_phase

_INDEX: dict[str, Any] = {"key": None, "index": None}


def _resp(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", **CORS_HEADERS},
        "body": json.dumps(body, ensure_ascii=False, default=str),
    }


def _body(event: dict[str, Any]) -> dict[str, Any]:
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        import base64

        raw = base64.b64decode(raw).decode("utf-8")
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _limit(value: Any, default: int, top: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(n, top))


def _iso(v: Any) -> Any:
    return v.isoformat() if isinstance(v, (dt.date, dt.datetime)) else v


def ofac_index(conn) -> wi.ListIndex:
    """Active OFAC records, cached while the list does not change (warm Lambda)."""
    key_row = conn.execute(
        "SELECT count(*) AS n, max(changed_at) AS at FROM public.watchlist_entries WHERE list_code = 'ofac'"
    ).fetchone()
    key = (key_row["n"], _iso(key_row["at"]))
    if _INDEX["key"] != key or _INDEX["index"] is None:
        rows = conn.execute(
            """
            SELECT ext_id, party_type, name, aliases, lists, programs, designated_on, birth_dates, nationalities,
                   countries, details, content_hash
              FROM public.watchlist_entries
             WHERE list_code = 'ofac' AND removed_at IS NULL AND party_type IN ('individual', 'organization')
            """
        ).fetchall()
        _INDEX["index"] = wi.ListIndex(wi.ofac_record_from_row(r) for r in rows)
        _INDEX["key"] = key
        log_phase("watchlist_api", f"ofac index {len(_INDEX['index'])} records")
    return _INDEX["index"]


def match_to_api(m: wi.Match) -> dict[str, Any]:
    return {
        "listCode": m.record.list_code,
        "extId": m.record.ref,
        "name": m.record.name,
        "partyType": m.record.party_type,
        "matchedBy": m.matched_by,
        "matchedName": m.matched_name,
        "nameLevel": m.name_level,
        "severity": m.severity,
        **m.record.info,
    }


def handle_search(conn, body: dict[str, Any]) -> dict[str, Any]:
    name = str(body.get("name") or "").strip()
    party_type = str(body.get("partyType") or "unknown").strip()
    if party_type not in ("individual", "organization", "unknown"):
        return _resp(400, {"code": "BAD_REQUEST", "message": "partyType: individual, organization o unknown."})
    rfc, curp = body.get("rfc"), body.get("curp")
    other = body.get("otherIds") if isinstance(body.get("otherIds"), list) else []
    if not name and not (rfc or curp or other):
        return _resp(400, {"code": "BAD_REQUEST", "message": "name, rfc, curp u otherIds requerido."})
    aliases = [str(a) for a in body.get("aliases") or [] if str(a).strip()][:8]
    subject = wi.Subject("search", name, party_type, wi.subject_ids(rfc, curp, other), aliases)
    matches = ofac_index(conn).match(subject)
    limit = _limit(body.get("limit"), 20, 100)
    return _resp(200, {"success": True, "total": len(matches), "matches": [match_to_api(m) for m in matches[:limit]]})


def handle_get(conn, body: dict[str, Any]) -> dict[str, Any]:
    ext_id = str(body.get("extId") or "").strip()
    list_code = str(body.get("listCode") or "ofac").strip()
    if not ext_id:
        return _resp(400, {"code": "BAD_REQUEST", "message": "extId requerido."})
    row = conn.execute(
        "SELECT * FROM public.watchlist_entries WHERE list_code = %s AND ext_id = %s", (list_code, ext_id)
    ).fetchone()
    if row is None:
        return _resp(404, {"code": "NOT_FOUND", "message": "Registro no encontrado."})
    history = conn.execute(
        """
        SELECT c.change, c.at, c.run_id FROM public.watchlist_entry_changes c
         WHERE c.entry_id = %s ORDER BY c.id DESC LIMIT 50
        """,
        (row["id"],),
    ).fetchall()
    entry = {k: _iso(v) for k, v in row.items() if k not in ("id", "name_norm")}
    return _resp(200, {"success": True, "entry": entry, "history": [{k: _iso(v) for k, v in h.items()} for h in history]})


def handle_changes(conn, body: dict[str, Any]) -> dict[str, Any]:
    run_id = body.get("runId")
    since = body.get("since")
    change = body.get("change")
    if change and change not in ("added", "changed", "removed", "restored"):
        return _resp(400, {"code": "BAD_REQUEST", "message": "change: added, changed, removed o restored."})
    if not run_id and not since:
        last = conn.execute(
            "SELECT id FROM public.watchlist_runs WHERE kind = 'sync' AND status = 'ok' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        run_id = last["id"] if last else None
    rows = conn.execute(
        """
        SELECT c.change, c.at, c.run_id, e.list_code, e.ext_id, e.name, e.party_type, e.lists, e.programs,
               e.designated_on, e.countries
          FROM public.watchlist_entry_changes c
          JOIN public.watchlist_entries e ON e.id = c.entry_id
         WHERE (%(run)s::bigint IS NULL OR c.run_id = %(run)s)
           AND (%(since)s::timestamptz IS NULL OR c.at >= %(since)s)
           AND (%(change)s::text IS NULL OR c.change = %(change)s)
         ORDER BY c.id DESC
         LIMIT %(limit)s
        """,
        {"run": run_id, "since": since, "change": change, "limit": _limit(body.get("limit"), 200, 2000)},
    ).fetchall()
    return _resp(
        200,
        {"success": True, "runId": run_id, "changes": [{k: _iso(v) for k, v in r.items()} for r in rows]},
    )


def handle_sync(body: dict[str, Any]) -> dict[str, Any]:
    fn = os.environ.get("WATCHLIST_SYNC_FUNCTION") or ""
    if not fn:
        return _resp(503, {"code": "NOT_CONFIGURED", "message": "WATCHLIST_SYNC_FUNCTION no configurado."})
    import boto3

    payload = {"force": bool(body.get("force")), "trigger": "manual"}
    boto3.client("lambda").invoke(FunctionName=fn, InvocationType="Event", Payload=json.dumps(payload).encode())
    return _resp(202, {"success": True, "message": "Sincronización iniciada."})


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    log_handler_enter("watchlist", event, context)
    method = (event.get("httpMethod") or "POST").upper()
    if method == "OPTIONS":
        return _resp(200, {"message": "ok"})
    try:
        ensure_data_backend()
    except (SupabaseConfigError, DatabaseConfigError) as e:
        return _resp(503, {"code": "BACKEND_NOT_CONFIGURED", "message": str(e)})

    claims = require_jwt(event)
    if isinstance(claims, str):
        return _resp(401, {"code": "UNAUTHORIZED", "message": claims})
    super_admin = claims.get("role") == "super_admin"
    path = (event.get("path") or "").rstrip("/")
    body = _body(event)
    log_phase("watchlist_route", f"{method} {path}")

    if method != "POST":
        return _resp(405, {"code": "METHOD_NOT_ALLOWED", "message": "Usa POST."})
    if not super_admin and not path.endswith("/watchlist/search") and not path.endswith("/watchlist/entries/get"):
        return _resp(403, {"code": "FORBIDDEN", "message": "Solo super admin."})
    try:
        if path.endswith("/watchlist/sync"):
            return handle_sync(body)
        conn = db.connect(autocommit=True)
        try:
            if path.endswith("/watchlist/search"):
                return handle_search(conn, body)
            if path.endswith("/watchlist/entries/get"):
                return handle_get(conn, body)
            if path.endswith("/watchlist/changes"):
                return handle_changes(conn, body)
            if path.endswith("/watchlist/runs"):
                kind = body.get("kind") if body.get("kind") in ("sync", "match") else None
                return _resp(200, {"success": True, "runs": db.list_runs(conn, kind=kind, limit=_limit(body.get("limit"), 30, 200))})
        finally:
            conn.close()
        return _resp(404, {"code": "NOT_FOUND", "message": "Ruta no encontrada", "path": path})
    except Exception as e:  # noqa: BLE001
        log_phase("watchlist_route", f"error {type(e).__name__}: {e}")
        return _resp(500, {"code": "INTERNAL", "message": "Error interno", "error": str(e)})
