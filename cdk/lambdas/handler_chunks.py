"""Ingest de chunks (POST /v1/chunks/ingest) → public.risk_entity_chunks."""

from __future__ import annotations

import base64
import json
import uuid
from typing import Any

from nuwa_api_auth import jwt_allows_client, jwt_matches_actor_body, require_jwt
from nuwa_chunks import ingest_chunks
from nuwa_config import DatabaseConfigError, SupabaseConfigError, ensure_data_backend
from nuwa_errors import SupabaseRestError
from nuwa_http import json_response
from nuwa_obs_log import log_handler_enter, log_phase


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


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    log_handler_enter("chunks", event, context)
    method = (event.get("httpMethod") or "POST").upper()
    if method == "OPTIONS":
        return _response(200, {"message": "ok"})

    try:
        ensure_data_backend()
    except (SupabaseConfigError, DatabaseConfigError) as e:
        return _response(
            503,
            {"code": "BACKEND_NOT_CONFIGURED", "message": str(e)},
        )

    claims = require_jwt(event)
    if isinstance(claims, str):
        return _response(401, {"code": "UNAUTHORIZED", "message": claims})

    body = _body(event)
    path = (event.get("path") or "").rstrip("/")

    if not path.endswith("/chunks/ingest"):
        return _response(404, {"code": "NOT_FOUND", "message": "Ruta no encontrada", "path": path})

    try:
        cid = int(body["clientId"])
        int(body["userId"])
    except (KeyError, TypeError, ValueError):
        return _response(400, {"code": "BAD_REQUEST", "message": "clientId y userId requeridos (enteros)."})

    if not jwt_matches_actor_body(claims, body):
        return _response(
            403,
            {"code": "FORBIDDEN", "message": "clientId y userId deben coincidir con el JWT (sub / cid)."},
        )
    if not jwt_allows_client(claims, cid):
        return _response(403, {"code": "FORBIDDEN", "message": "clientId no permitido para este token."})

    try:
        source_id = int(body["sourceId"])
    except (KeyError, TypeError, ValueError):
        return _response(400, {"code": "BAD_REQUEST", "message": "sourceId requerido (entero)."})

    strategy = body.get("replaceStrategy")
    if strategy not in ("all", "append"):
        return _response(
            400,
            {"code": "BAD_REQUEST", "message": "replaceStrategy requerido: all o append."},
        )

    raw_chunks = body.get("chunks")
    if not isinstance(raw_chunks, list) or len(raw_chunks) < 1:
        return _response(400, {"code": "BAD_REQUEST", "message": "chunks debe ser un array con al menos un elemento."})

    parsed: list[tuple[int, str]] = []
    for i, ch in enumerate(raw_chunks):
        if not isinstance(ch, dict):
            return _response(400, {"code": "BAD_REQUEST", "message": f"chunks[{i}] debe ser objeto."})
        try:
            order = int(ch.get("order"))
        except (TypeError, ValueError):
            return _response(400, {"code": "BAD_REQUEST", "message": f"chunks[{i}].order debe ser entero."})
        text = ch.get("chunkText")
        if not isinstance(text, str) or not text.strip():
            return _response(
                400,
                {"code": "BAD_REQUEST", "message": f"chunks[{i}].chunkText debe ser string no vacío."},
            )
        parsed.append((order, text.strip()))

    parsed.sort(key=lambda x: x[0])
    chunk_texts = [t for _o, t in parsed]

    rl_raw = body.get("riskLevel")
    if rl_raw is not None:
        try:
            risk_level = int(rl_raw)
        except (TypeError, ValueError):
            return _response(400, {"code": "BAD_REQUEST", "message": "riskLevel debe ser 1, 2 o 3."})
        if risk_level not in (1, 2, 3):
            return _response(400, {"code": "BAD_REQUEST", "message": "riskLevel debe ser 1, 2 o 3."})
    else:
        risk_level = None

    vis_raw = body.get("visibility")
    if vis_raw is not None:
        if vis_raw not in ("public", "private"):
            return _response(400, {"code": "BAD_REQUEST", "message": "visibility debe ser public o private."})
        visibility = str(vis_raw)
    else:
        visibility = None

    et_raw = body.get("entityType")
    if et_raw is not None:
        if not isinstance(et_raw, str) or not et_raw.strip():
            return _response(400, {"code": "BAD_REQUEST", "message": "entityType debe ser string no vacío."})
        entity_type = et_raw.strip()[:200]
    else:
        entity_type = None

    is_super_admin = claims.get("role") == "super_admin"

    try:
        log_phase("chunks_ingest", f"sourceId={source_id} strategy={strategy} n={len(chunk_texts)}")
        out = ingest_chunks(
            source_id=source_id,
            viewer_client_id=cid,
            is_super_admin=is_super_admin,
            replace_strategy=strategy,
            chunk_texts=chunk_texts,
            risk_level=risk_level,
            visibility=visibility,
            entity_type=entity_type,
        )
        rid = body.get("requestId")
        if isinstance(rid, str) and rid.strip():
            try:
                uuid.UUID(rid.strip())
                out = {**out, "requestId": rid.strip()}
            except ValueError:
                pass
        return _response(200, out)
    except SupabaseRestError as e:
        code = "DATA_BACKEND_ERROR"
        if e.status == 404:
            code = "NOT_FOUND"
        elif e.status == 403:
            code = "FORBIDDEN"
        elif e.status == 400:
            code = "BAD_REQUEST"
        return _response(
            e.status if 400 <= e.status < 600 else 500,
            {"code": code, "message": str(e.body)[:2000]},
        )
    except Exception as e:
        return _response(500, {"code": "INTERNAL", "message": str(e)})
