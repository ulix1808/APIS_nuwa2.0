"""Entidades PF/PM, match, monitoreo continuo."""

from __future__ import annotations

import base64
import json
from typing import Any

from nuwa_api_auth import effective_tenant_scope, jwt_allows_client, jwt_matches_actor_body, require_jwt
from nuwa_config import DatabaseConfigError, SupabaseConfigError, ensure_data_backend, is_database_mode
from nuwa_errors import SupabaseRestError
from nuwa_http import CORS_HEADERS
from nuwa_obs_log import log_handler_enter, log_phase


def _resp(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", **CORS_HEADERS},
        "body": json.dumps(body, ensure_ascii=False, default=str),
    }


def _body(event: dict[str, Any]) -> dict[str, Any]:
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    try:
        return json.loads(raw) if isinstance(raw, str) else {}
    except json.JSONDecodeError:
        return {}


def _auth(body: dict[str, Any], event: dict[str, Any]) -> dict[str, Any] | str:
    claims = require_jwt(event)
    if isinstance(claims, str):
        return claims
    if not jwt_matches_actor_body(claims, body):
        return "FORBIDDEN_ACTOR"
    try:
        cid = int(body["clientId"])
    except (KeyError, TypeError, ValueError):
        return "BAD_CLIENT"
    if not jwt_allows_client(claims, cid):
        return "FORBIDDEN_CLIENT"
    bound = effective_tenant_scope(claims)
    if bound is not None and cid != bound:
        return "FORBIDDEN_CLIENT"
    return claims


def _require_pg() -> None:
    if not is_database_mode():
        raise SupabaseRestError(
            503,
            "Entidades requiere NUWA_DATABASE_SECRET_ARN (PostgreSQL directo).",
        )


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    log_handler_enter("entities", event, context)
    method = (event.get("httpMethod") or "POST").upper()
    if method == "OPTIONS":
        return _resp(200, {"message": "ok"})

    try:
        ensure_data_backend()
        _require_pg()
    except (SupabaseConfigError, DatabaseConfigError) as e:
        return _resp(503, {"message": str(e), "code": "BACKEND_NOT_CONFIGURED"})
    except SupabaseRestError as e:
        return _resp(e.status if 400 <= e.status < 600 else 500, {"message": e.body, "code": "ENTITIES_PG_REQUIRED"})

    path = (event.get("path") or "").rstrip("/")
    log_phase("entities_route", f"{method} {path}")

    try:
        body = _body(event)
        auth = _auth(body, event)
        if auth == "FORBIDDEN_ACTOR":
            return _resp(403, {"code": "FORBIDDEN", "message": "clientId y userId deben coincidir con el JWT."})
        if auth == "FORBIDDEN_CLIENT":
            return _resp(403, {"code": "FORBIDDEN", "message": "clientId no autorizado para este token."})
        if auth == "BAD_CLIENT":
            return _resp(400, {"code": "BAD_REQUEST", "message": "clientId es requerido"})
        if isinstance(auth, str):
            return _resp(401, {"code": "UNAUTHORIZED", "message": auth})

        from nuwa_entities_pg import (
            entities_create_pg,
            entities_delete_pg,
            entities_get_pg,
            entities_list_pg,
            entities_match_pg,
            entities_monitoring_list_pg,
            entities_monitoring_upsert_pg,
            entities_stats_pg,
            entities_update_pg,
        )

        if path.endswith("/entities/match"):
            return _resp(200, entities_match_pg(body))
        if path.endswith("/entities/create"):
            return _resp(201, entities_create_pg(body))
        if path.endswith("/entities/list"):
            return _resp(200, entities_list_pg(body))
        if path.endswith("/entities/get"):
            return _resp(200, entities_get_pg(body))
        if path.endswith("/entities/update"):
            return _resp(200, entities_update_pg(body))
        if path.endswith("/entities/delete"):
            return _resp(200, entities_delete_pg(body))
        if path.endswith("/entities/stats"):
            return _resp(200, entities_stats_pg(body))
        if path.endswith("/entities/monitoring/upsert"):
            return _resp(200, entities_monitoring_upsert_pg(body))
        if path.endswith("/entities/monitoring/list"):
            return _resp(200, entities_monitoring_list_pg(body))

        return _resp(404, {"message": "Ruta no encontrada", "method": method, "path": path})
    except SupabaseRestError as e:
        code = "DUPLICATE_RFC" if e.body == "DUPLICATE_RFC" else "DUPLICATE_CURP" if e.body == "DUPLICATE_CURP" else "DATA_BACKEND_ERROR"
        return _resp(
            e.status if 400 <= e.status < 600 else 500,
            {"message": e.body, "code": code},
        )
    except json.JSONDecodeError:
        return _resp(400, {"message": "JSON inválido", "code": "BAD_REQUEST"})
    except Exception as e:
        return _resp(500, {"message": "Error interno", "error": str(e)})
