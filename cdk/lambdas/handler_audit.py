"""
POST /v1/audit/create — persist a tenant audit event.
POST /v1/audit/list — list events for the JWT clientId.
"""

from __future__ import annotations

import json
from typing import Any

from nuwa_api_auth import effective_tenant_scope, jwt_allows_client, require_jwt
from nuwa_audit_pg import insert_audit_event, list_audit_events
from nuwa_config import DatabaseConfigError, SupabaseConfigError, ensure_data_backend
from nuwa_errors import SupabaseRestError
from nuwa_http import CORS_HEADERS
from nuwa_monitoring_worker import monitoring_worker_claims, monitoring_worker_secret_ok
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


def handle_create(event: dict[str, Any], claims: dict[str, Any]) -> dict[str, Any]:
    body = _parse_json_body(event)
    bound = effective_tenant_scope(claims)
    client_id = _int(body.get("clientId")) or bound
    if client_id is None:
        return _resp(400, {"code": "BAD_REQUEST", "message": "clientId requerido."})
    if not claims.get("worker") and not jwt_allows_client(claims, client_id):
        return _resp(403, {"code": "FORBIDDEN", "message": "clientId no permitido para este token."})
    event_type = str(body.get("type") or "").strip()
    category = str(body.get("category") or "").strip()
    if not event_type or not category:
        return _resp(400, {"code": "BAD_REQUEST", "message": "type y category requeridos."})

    user_id = _int(body.get("userId"))
    try:
        from nuwa_jwt import jwt_int

        user_id = user_id or jwt_int(claims, "sub")
    except (ValueError, TypeError):
        pass

    result = insert_audit_event(
        {
            "id": body.get("id"),
            "clientId": client_id,
            "userId": user_id,
            "userName": body.get("userName") or body.get("user"),
            "userRole": body.get("userRole") or claims.get("role"),
            "type": event_type,
            "category": category,
            "target": body.get("target"),
            "targetId": body.get("targetId"),
            "details": body.get("details"),
            "metadata": body.get("metadata"),
            "ipAddress": body.get("ipAddress") or body.get("ip"),
            "timestamp": body.get("timestamp"),
        }
    )
    return _resp(200, {"success": True, **result})


def handle_list(event: dict[str, Any], claims: dict[str, Any]) -> dict[str, Any]:
    body = _parse_json_body(event)
    qs = event.get("queryStringParameters") or {}
    bound = effective_tenant_scope(claims)
    client_id = _int(body.get("clientId") or qs.get("clientId")) or bound
    if client_id is None:
        return _resp(400, {"code": "BAD_REQUEST", "message": "clientId requerido."})
    if not jwt_allows_client(claims, client_id):
        return _resp(403, {"code": "FORBIDDEN", "message": "clientId no permitido para este token."})
    limit = _int(body.get("limit") or qs.get("limit")) or 300
    events = list_audit_events(client_id, limit)
    return _resp(200, {"success": True, "clientId": client_id, "events": events})


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    log_handler_enter("audit", event, context)
    method = (event.get("httpMethod") or "POST").upper()
    if method == "OPTIONS":
        return _resp(200, {"message": "ok"})

    try:
        ensure_data_backend()
    except (SupabaseConfigError, DatabaseConfigError) as e:
        return _resp(503, {"code": "BACKEND_NOT_CONFIGURED", "message": str(e)})

    claims = require_jwt(event)
    if isinstance(claims, str):
        if monitoring_worker_secret_ok(event):
            claims = monitoring_worker_claims()
        else:
            return _resp(401, {"code": "UNAUTHORIZED", "message": claims})

    path = (event.get("path") or "").rstrip("/")
    log_phase("audit_route", f"{method} {path}")

    try:
        if method == "POST" and path.endswith("/audit/create"):
            return handle_create(event, claims)
        if method in ("GET", "POST") and path.endswith("/audit/list"):
            if claims.get("worker"):
                return _resp(403, {"code": "FORBIDDEN", "message": "Worker no puede listar audit."})
            return handle_list(event, claims)
        return _resp(404, {"code": "NOT_FOUND", "message": "Ruta no encontrada", "path": path})
    except SupabaseRestError as e:
        return _resp(
            e.status if 400 <= e.status < 600 else 500,
            {"code": "DATA_BACKEND_ERROR", "message": e.body},
        )
    except Exception as e:
        return _resp(500, {"code": "INTERNAL", "message": "Error interno", "error": str(e)})
