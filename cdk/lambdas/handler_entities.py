"""Entidades PF/PM, match, monitoreo continuo."""

from __future__ import annotations

import base64
import hmac
import json
import os
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


def _headers_lower(event: dict[str, Any]) -> dict[str, str]:
    raw = event.get("headers") or {}
    return {str(k).lower(): str(v) for k, v in raw.items()}


def monitoring_worker_secret_ok(event: dict[str, Any]) -> bool:
    expected = (os.environ.get("MONITORING_WORKER_SECRET") or "").strip()
    if not expected:
        return False
    headers = _headers_lower(event)
    got = (headers.get("x-monitoring-worker-secret") or "").strip()
    if not got:
        auth = headers.get("authorization") or ""
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
            # Only treat as worker secret if it matches (not a JWT)
            if token and "." not in token:
                got = token
    if not got:
        return False
    return hmac.compare_digest(got, expected)


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


def _auth_user_or_worker(
    body: dict[str, Any],
    event: dict[str, Any],
    *,
    require_client_id: bool = True,
) -> dict[str, Any] | str:
    if monitoring_worker_secret_ok(event):
        if require_client_id:
            try:
                int(body["clientId"])
            except (KeyError, TypeError, ValueError):
                return "BAD_CLIENT"
        return {"role": "monitoring_worker", "worker": True}
    return _auth(body, event)


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

        worker_only_paths = (
            "/entities/monitoring/due",
        )
        worker_or_user_paths = (
            "/entities/monitoring/run-start",
            "/entities/monitoring/run-finish",
            "/entities/alerts/create",
        )

        if any(path.endswith(p) for p in worker_only_paths):
            if not monitoring_worker_secret_ok(event):
                return _resp(401, {"code": "UNAUTHORIZED", "message": "Se requiere x-monitoring-worker-secret"})
            auth: dict[str, Any] | str = {"role": "monitoring_worker", "worker": True}
        elif any(path.endswith(p) for p in worker_or_user_paths):
            auth = _auth_user_or_worker(body, event, require_client_id=True)
        else:
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
            entities_alerts_ack_pg,
            entities_alerts_create_pg,
            entities_alerts_list_pg,
            entities_create_pg,
            entities_delete_pg,
            entities_get_pg,
            entities_list_pg,
            entities_match_pg,
            entities_monitoring_due_pg,
            entities_monitoring_list_pg,
            entities_monitoring_run_finish_pg,
            entities_monitoring_run_start_pg,
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
        if path.endswith("/entities/monitoring/due"):
            return _resp(200, entities_monitoring_due_pg(body))
        if path.endswith("/entities/monitoring/run-start"):
            return _resp(200, entities_monitoring_run_start_pg(body))
        if path.endswith("/entities/monitoring/run-finish"):
            return _resp(200, entities_monitoring_run_finish_pg(body))
        if path.endswith("/entities/alerts/list"):
            return _resp(200, entities_alerts_list_pg(body))
        if path.endswith("/entities/alerts/ack"):
            return _resp(200, entities_alerts_ack_pg(body))
        if path.endswith("/entities/alerts/create"):
            return _resp(201, entities_alerts_create_pg(body))

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
