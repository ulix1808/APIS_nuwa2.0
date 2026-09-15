"""
POST /v1/escalations/list — list tenant escalations.
POST /v1/escalations/save — upsert escalation.
POST /v1/escalations/resolve — resolve (de-escalate) by id.
"""

from __future__ import annotations

import json
from typing import Any

from nuwa_api_auth import effective_tenant_scope, jwt_allows_client, require_jwt
from nuwa_config import DatabaseConfigError, SupabaseConfigError, ensure_data_backend
from nuwa_errors import SupabaseRestError
from nuwa_escalations_pg import get_escalation, list_escalations, resolve_escalation, save_escalation
from nuwa_http import CORS_HEADERS
from nuwa_obs_log import log_handler_enter, log_phase


def _resp(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", **CORS_HEADERS},
        "body": json.dumps(body, ensure_ascii=False, default=str),
    }


def _parse_json_body(event: dict[str, Any]) -> dict[str, Any]:
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        import base64

        raw = base64.b64decode(raw).decode("utf-8")
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw) if isinstance(raw, str) else {}
    except json.JSONDecodeError:
        return {}


def _int(value: Any) -> int | None:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def handle_list(event: dict[str, Any], claims: dict[str, Any]) -> dict[str, Any]:
    body = _parse_json_body(event)
    qs = event.get("queryStringParameters") or {}
    bound = effective_tenant_scope(claims)
    client_id = _int(body.get("clientId") or qs.get("clientId")) or bound
    if client_id is None:
        return _resp(400, {"code": "BAD_REQUEST", "message": "clientId requerido."})
    if not jwt_allows_client(claims, client_id):
        return _resp(403, {"code": "FORBIDDEN", "message": "clientId no permitido para este token."})
    limit = _int(body.get("limit") or qs.get("limit")) or 500
    rows = list_escalations(client_id, limit)
    return _resp(200, {"success": True, "clientId": client_id, "escalations": rows})


def handle_save(event: dict[str, Any], claims: dict[str, Any]) -> dict[str, Any]:
    body = _parse_json_body(event)
    bound = effective_tenant_scope(claims)
    client_id = _int(body.get("clientId")) or bound
    if client_id is None:
        return _resp(400, {"code": "BAD_REQUEST", "message": "clientId requerido."})
    if not jwt_allows_client(claims, client_id):
        return _resp(403, {"code": "FORBIDDEN", "message": "clientId no permitido para este token."})

    escalation = body.get("escalation")
    if not isinstance(escalation, dict):
        escalation = body
    esc_id = str(escalation.get("id") or "").strip()
    prior = get_escalation(client_id, esc_id) if esc_id else None
    saved = save_escalation(client_id, escalation)
    return _resp(200, {"success": True, "escalation": saved, "isNew": prior is None})


def handle_resolve(event: dict[str, Any], claims: dict[str, Any]) -> dict[str, Any]:
    body = _parse_json_body(event)
    bound = effective_tenant_scope(claims)
    client_id = _int(body.get("clientId")) or bound
    if client_id is None:
        return _resp(400, {"code": "BAD_REQUEST", "message": "clientId requerido."})
    if not jwt_allows_client(claims, client_id):
        return _resp(403, {"code": "FORBIDDEN", "message": "clientId no permitido para este token."})

    esc_id = str(body.get("id") or "").strip()
    if not esc_id:
        return _resp(400, {"code": "BAD_REQUEST", "message": "id requerido."})
    resolution = body.get("resolution") if isinstance(body.get("resolution"), dict) else {}
    updated = resolve_escalation(client_id, esc_id, resolution)
    if not updated:
        return _resp(404, {"code": "NOT_FOUND", "message": "Escalation not found"})
    return _resp(200, {"success": True, "escalation": updated})


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    log_handler_enter("escalations", event, context)
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

    path = (event.get("path") or "").rstrip("/")
    log_phase("escalations_route", f"{method} {path}")

    try:
        if method in ("GET", "POST") and path.endswith("/escalations/list"):
            return handle_list(event, claims)
        if method == "POST" and path.endswith("/escalations/save"):
            return handle_save(event, claims)
        if method == "POST" and path.endswith("/escalations/resolve"):
            return handle_resolve(event, claims)
        return _resp(404, {"code": "NOT_FOUND", "message": "Ruta no encontrada", "path": path})
    except SupabaseRestError as e:
        return _resp(
            e.status if 400 <= e.status < 600 else 500,
            {"code": "DATA_BACKEND_ERROR", "message": e.body},
        )
    except Exception as e:
        return _resp(500, {"code": "INTERNAL", "message": "Error interno", "error": str(e)})
