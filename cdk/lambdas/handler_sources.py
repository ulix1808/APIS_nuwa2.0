"""Catálogo de fuentes: POST /v1/sources, /list, /get, /update, /delete."""

from __future__ import annotations

import base64
import json
from typing import Any

from nuwa_api_auth import jwt_allows_client, jwt_matches_actor_body, require_jwt
from nuwa_config import DatabaseConfigError, SupabaseConfigError, ensure_data_backend
from nuwa_errors import SupabaseRestError
from nuwa_http import json_response, no_content_response
from nuwa_obs_log import log_handler_enter, log_phase
from nuwa_sources import (
    create_source,
    delete_source,
    get_source,
    list_sources,
    resolve_create_visibility,
    update_source,
)


def _response(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return json_response(status, body)


def _body(event: dict[str, Any]) -> dict[str, Any]:
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    try:
        return json.loads(raw) if isinstance(raw, str) else {}
    except json.JSONDecodeError:
        return {}


def _bad(msg: str) -> dict[str, Any]:
    return _response(400, {"code": "BAD_REQUEST", "message": msg})


def _forbidden(msg: str) -> dict[str, Any]:
    return _response(403, {"code": "FORBIDDEN", "message": msg})


def _not_found(msg: str = "Recurso no encontrado.") -> dict[str, Any]:
    return _response(404, {"code": "NOT_FOUND", "message": msg})


def _require_actor(claims: dict[str, Any], body: dict[str, Any]) -> dict[str, Any] | None:
    if not jwt_matches_actor_body(claims, body):
        return _forbidden("clientId y userId deben coincidir con el JWT (sub / cid).")
    return None


def _parse_actor_ids(body: dict[str, Any]) -> tuple[int, int] | None:
    try:
        cid = int(body["clientId"])
        uid = int(body["userId"])
        return cid, uid
    except (KeyError, TypeError, ValueError):
        return None


def _validate_risk_level(v: Any) -> int | None:
    try:
        x = int(v)
        if x in (1, 2, 3):
            return x
    except (TypeError, ValueError):
        pass
    return None


def _validate_visibility(v: Any) -> str | None:
    if isinstance(v, str) and v in ("public", "private"):
        return v
    return None


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    log_handler_enter("sources", event, context)
    path = (event.get("path") or "").rstrip("/")
    method = (event.get("httpMethod") or "POST").upper()
    if method == "OPTIONS":
        return _response(200, {"message": "ok"})

    try:
        ensure_data_backend()
    except (SupabaseConfigError, DatabaseConfigError) as e:
        return _response(
            503,
            {"code": "BACKEND_NOT_CONFIGURED", "message": str(e), "path": path},
        )

    claims = require_jwt(event)
    if isinstance(claims, str):
        return _response(401, {"code": "UNAUTHORIZED", "message": claims})

    is_super_admin = claims.get("role") == "super_admin"
    body = _body(event)

    try:
        if path.endswith("/sources/delete"):
            err = _require_actor(claims, body)
            if err:
                return err
            ids = _parse_actor_ids(body)
            if not ids:
                return _bad("clientId y userId requeridos (enteros).")
            cid, _uid = ids
            if not jwt_allows_client(claims, cid):
                return _forbidden("clientId no permitido para este token.")
            try:
                sid = int(body["sourceId"])
            except (KeyError, TypeError, ValueError):
                return _bad("sourceId requerido (entero).")
            log_phase("sources_delete", f"id={sid}")
            st = delete_source(source_id=sid, viewer_client_id=cid, is_super_admin=is_super_admin)
            if st == "not_found":
                return _not_found("Fuente no encontrada.")
            if st == "forbidden":
                return _forbidden("Sin permiso para eliminar esta fuente.")
            return no_content_response()

        if path.endswith("/sources/update"):
            err = _require_actor(claims, body)
            if err:
                return err
            ids = _parse_actor_ids(body)
            if not ids:
                return _bad("clientId y userId requeridos (enteros).")
            cid, _uid = ids
            if not jwt_allows_client(claims, cid):
                return _forbidden("clientId no permitido para este token.")
            try:
                sid = int(body["sourceId"])
            except (KeyError, TypeError, ValueError):
                return _bad("sourceId requerido (entero).")
            name = body.get("name")
            if name is not None and (not isinstance(name, str) or not name.strip()):
                return _bad("name no puede estar vacío.")
            name = name.strip() if isinstance(name, str) else None
            rl = None
            if "riskLevel" in body:
                rl = _validate_risk_level(body.get("riskLevel"))
                if rl is None:
                    return _bad("riskLevel debe ser 1, 2 o 3.")
            vis = None
            if "visibility" in body:
                vis = _validate_visibility(body.get("visibility"))
                if vis is None:
                    return _bad("visibility debe ser public o private.")
            meta = body.get("metadata")
            if meta is not None and not isinstance(meta, dict):
                return _bad("metadata debe ser un objeto JSON.")
            log_phase("sources_update", f"id={sid}")
            out = update_source(
                source_id=sid,
                viewer_client_id=cid,
                is_super_admin=is_super_admin,
                name=name,
                risk_level=rl,
                visibility=vis,
                metadata=meta,
            )
            if out == "forbidden":
                return _forbidden("Sin permiso para actualizar esta fuente.")
            if out is None:
                return _not_found("Fuente no encontrada.")
            return _response(200, out)

        if path.endswith("/sources/get"):
            err = _require_actor(claims, body)
            if err:
                return err
            ids = _parse_actor_ids(body)
            if not ids:
                return _bad("clientId y userId requeridos (enteros).")
            cid, _uid = ids
            if not jwt_allows_client(claims, cid):
                return _forbidden("clientId no permitido para este token.")
            try:
                sid = int(body["sourceId"])
            except (KeyError, TypeError, ValueError):
                return _bad("sourceId requerido (entero).")
            log_phase("sources_get", f"id={sid}")
            row = get_source(
                source_id=sid,
                viewer_client_id=cid,
                is_super_admin=is_super_admin,
            )
            if not row:
                return _not_found("Fuente no encontrada o no visible para este tenant.")
            return _response(200, row)

        if path.endswith("/sources/list"):
            err = _require_actor(claims, body)
            if err:
                return err
            ids = _parse_actor_ids(body)
            if not ids:
                return _bad("clientId y userId requeridos (enteros).")
            cid, _uid = ids
            if not jwt_allows_client(claims, cid):
                return _forbidden("clientId no permitido para este token.")
            lim = int(body.get("limit") or 50)
            off = int(body.get("offset") or 0)
            log_phase("sources_list", f"clientId={cid}")
            items, total = list_sources(viewer_client_id=cid, limit=lim, offset=off)
            out: dict[str, Any] = {"clientId": cid, "items": items}
            if total is not None:
                out["total"] = total
            return _response(200, out)

        if path.endswith("/sources"):
            err = _require_actor(claims, body)
            if err:
                return err
            ids = _parse_actor_ids(body)
            if not ids:
                return _bad("clientId y userId requeridos (enteros).")
            cid, uid = ids
            if not jwt_allows_client(claims, cid):
                return _forbidden("clientId no permitido para este token.")
            name = body.get("name")
            if not isinstance(name, str) or not name.strip():
                return _bad("name requerido (string no vacío).")
            rl = _validate_risk_level(body.get("riskLevel"))
            if rl is None:
                return _bad("riskLevel requerido: 1, 2 o 3.")
            vis = _validate_visibility(body.get("visibility"))
            if vis is None:
                return _bad("visibility requerido: public o private.")
            meta = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
            vis = resolve_create_visibility(cid, uid, vis)
            log_phase("sources_create", f"name={name!r} clientId={cid}")
            row = create_source(
                name=name.strip(),
                risk_level=rl,
                visibility=vis,
                client_id=cid,
                created_by_user_id=uid,
                metadata=meta,
            )
            return json_response(201, row)

    except SupabaseRestError as e:
        return _response(
            e.status if 400 <= e.status < 600 else 500,
            {"code": "DATA_BACKEND_ERROR", "message": str(e.body)[:2000]},
        )
    except Exception as e:
        return _response(500, {"code": "INTERNAL", "message": str(e)})

    return _response(404, {"code": "NOT_FOUND", "message": "Ruta no encontrada", "path": path})
