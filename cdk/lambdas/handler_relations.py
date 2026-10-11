"""
POST /v1/relations/query — active relations where names/aliases (keys) or entity ids appear
  as subject or as related entity. Body { clientId?, keys?: [], entityIds?: [], limit? }.
POST /v1/relations/sync — replace the relations of one report. Body
  { clientId?, folio, rows: [], subjectEntityId?, reportCreatedAt? } (BFF backfill / repair).

Report save/update also replace relations when the body carries `entityRelations`.
"""

from __future__ import annotations

import json
from typing import Any

from nuwa_api_auth import effective_tenant_scope, jwt_allows_client, require_jwt
from nuwa_config import DatabaseConfigError, SupabaseConfigError, ensure_data_backend, is_database_mode
from nuwa_http import CORS_HEADERS
from nuwa_obs_log import log_handler_enter, log_phase
from nuwa_relations_pg import query_relations, replace_report_relations, report_belongs_to_client


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


def _client_id(body: dict[str, Any], claims: dict[str, Any]) -> int | dict[str, Any]:
    client_id = _int(body.get("clientId")) or effective_tenant_scope(claims)
    if client_id is None:
        return _resp(400, {"code": "BAD_REQUEST", "message": "clientId requerido."})
    if not jwt_allows_client(claims, client_id):
        return _resp(403, {"code": "FORBIDDEN", "message": "clientId no permitido para este token."})
    return client_id


def handle_query(event: dict[str, Any], claims: dict[str, Any]) -> dict[str, Any]:
    body = _parse_json_body(event)
    client_id = _client_id(body, claims)
    if isinstance(client_id, dict):
        return client_id
    rows = query_relations(
        client_id,
        keys=body.get("keys"),
        entity_ids=body.get("entityIds"),
        limit=_int(body.get("limit")) or 500,
    )
    if rows is None:
        return _resp(200, {"success": True, "available": False, "relations": []})
    return _resp(200, {"success": True, "available": True, "relations": rows})


def handle_sync(event: dict[str, Any], claims: dict[str, Any]) -> dict[str, Any]:
    body = _parse_json_body(event)
    client_id = _client_id(body, claims)
    if isinstance(client_id, dict):
        return client_id
    folio = str(body.get("folio") or "").strip()
    if not folio:
        return _resp(400, {"code": "BAD_REQUEST", "message": "folio requerido."})
    if not isinstance(body.get("rows"), list):
        return _resp(400, {"code": "BAD_REQUEST", "message": "rows debe ser una lista."})
    if not report_belongs_to_client(client_id, folio):
        return _resp(404, {"code": "NOT_FOUND", "message": f"No hay reporte activo {folio} para este cliente."})
    written = replace_report_relations(
        client_id,
        folio,
        body["rows"],
        subject_entity_id=str(body.get("subjectEntityId") or "").strip() or None,
        report_created_at=str(body.get("reportCreatedAt") or "").strip() or None,
    )
    if written < 0:
        return _resp(503, {"code": "RELATIONS_TABLE_MISSING", "message": "Aplica la migración entity_relations."})
    return _resp(200, {"success": True, "folio": folio, "rows": written})


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    log_handler_enter("relations", event, context)
    method = (event.get("httpMethod") or "POST").upper()
    if method == "OPTIONS":
        return _resp(200, {"message": "ok"})

    try:
        ensure_data_backend()
    except (SupabaseConfigError, DatabaseConfigError) as e:
        return _resp(503, {"code": "BACKEND_NOT_CONFIGURED", "message": str(e)})
    if not is_database_mode():
        return _resp(501, {"code": "NOT_SUPPORTED", "message": "Relaciones requieren modo PostgreSQL."})

    claims = require_jwt(event)
    if isinstance(claims, str):
        return _resp(401, {"code": "UNAUTHORIZED", "message": claims})

    path = (event.get("path") or "").rstrip("/")
    log_phase("relations_route", f"{method} {path}")
    try:
        if method == "POST" and path.endswith("/relations/query"):
            return handle_query(event, claims)
        if method == "POST" and path.endswith("/relations/sync"):
            return handle_sync(event, claims)
        return _resp(404, {"code": "NOT_FOUND", "message": "Ruta no encontrada", "path": path})
    except Exception as e:
        return _resp(500, {"code": "INTERNAL", "message": "Error interno", "error": str(e)})
