"""Documentos internos del cliente (S3 + Postgres)."""

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
            "Documentos requiere NUWA_DATABASE_SECRET_ARN (PostgreSQL directo).",
        )


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    log_handler_enter("documents", event, context)
    method = (event.get("httpMethod") or "POST").upper()
    if method == "OPTIONS":
        return _resp(200, {"message": "ok"})

    try:
        ensure_data_backend()
        _require_pg()
    except (SupabaseConfigError, DatabaseConfigError) as e:
        return _resp(503, {"message": str(e), "code": "BACKEND_NOT_CONFIGURED"})
    except SupabaseRestError as e:
        return _resp(e.status if 400 <= e.status < 600 else 500, {"message": e.body, "code": "DOCUMENTS_PG_REQUIRED"})

    path = (event.get("path") or "").rstrip("/")
    log_phase("documents_route", f"{method} {path}")

    try:
        body = _body(event)
        auth = _auth(body, event)
        if auth == "FORBIDDEN_ACTOR":
            return _resp(403, {"code": "FORBIDDEN", "message": "clientId/userId no coinciden con JWT."})
        if auth in ("BAD_CLIENT", "FORBIDDEN_CLIENT"):
            return _resp(403, {"code": "FORBIDDEN", "message": "clientId no permitido."})
        if isinstance(auth, str):
            return _resp(401, {"code": "UNAUTHORIZED", "message": auth})

        from nuwa_documents_pg import (
            documents_delete_pg,
            documents_download_url_pg,
            documents_finalize_pg,
            documents_get_pg,
            documents_list_pg,
            documents_presign_pg,
            documents_update_pg,
            documents_upload_complete_pg,
            storage_init_pg,
        )

        if path.endswith("/clients/storage/init"):
            return _resp(200, storage_init_pg(body))
        if path.endswith("/documents/presign"):
            return _resp(200, documents_presign_pg(body))
        if path.endswith("/documents/upload-complete"):
            return _resp(200, documents_upload_complete_pg(body))
        if path.endswith("/documents/finalize"):
            return _resp(200, documents_finalize_pg(body))
        if path.endswith("/documents/list"):
            return _resp(200, documents_list_pg(body))
        if path.endswith("/documents/get"):
            return _resp(200, documents_get_pg(body))
        if path.endswith("/documents/update"):
            return _resp(200, documents_update_pg(body))
        if path.endswith("/documents/delete"):
            return _resp(200, documents_delete_pg(body))
        if path.endswith("/documents/download-url"):
            return _resp(200, documents_download_url_pg(body))

        return _resp(404, {"code": "NOT_FOUND", "message": path})
    except SupabaseRestError as e:
        code = "DATA_ERROR"
        if e.status == 404:
            code = "NOT_FOUND"
        elif e.status == 409:
            code = "CONFLICT"
        elif e.status == 422:
            code = "UNPROCESSABLE"
        return _resp(
            e.status if 400 <= e.status < 600 else 500,
            {"code": code, "message": e.body},
        )
    except RuntimeError as e:
        return _resp(503, {"code": "STORAGE_NOT_CONFIGURED", "message": str(e)})
    except Exception as e:
        return _resp(500, {"code": "INTERNAL", "message": str(e)})
