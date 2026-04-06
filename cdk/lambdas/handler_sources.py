"""Catálogo de fuentes (POST /v1/sources/*). Implementar llamadas a Supabase REST o driver PG."""

from __future__ import annotations

import json
from typing import Any

from nuwa_api_auth import require_jwt
from nuwa_config import DatabaseConfigError, SupabaseConfigError, ensure_data_backend, is_database_mode
from nuwa_http import json_response
from nuwa_obs_log import log_handler_enter


def _response(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return json_response(status, body)


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    log_handler_enter("sources", event, context)
    path = (event.get("path") or "").rstrip("/") or "/"
    try:
        ensure_data_backend()
    except (SupabaseConfigError, DatabaseConfigError) as e:
        return _response(
            503,
            {
                "code": "BACKEND_NOT_CONFIGURED",
                "message": str(e),
                "path": path,
            },
        )

    msg = require_jwt(event)
    if isinstance(msg, str):
        return _response(401, {"code": "UNAUTHORIZED", "message": msg})

    return _response(
        200,
        {
            "message": "Nuwa sources handler — implementar tabla sources (PostgREST o RDS directo).",
            "path": path,
            "databaseMode": is_database_mode(),
        },
    )
