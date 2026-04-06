"""Búsqueda (POST /v1/search) → RPC search_risk_entities (Postgres o PostgREST)."""

from __future__ import annotations

import base64
import json
from typing import Any

from nuwa_config import DatabaseConfigError, SupabaseConfigError, ensure_data_backend, is_database_mode
from nuwa_errors import SupabaseRestError
from nuwa_http import json_response
from nuwa_api_auth import jwt_allows_client, require_jwt
from nuwa_supabase import invoke_search_risk_entities
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


def _map_row(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "chunkId": str(r["id"]),
        "sourceId": int(r["source_id"]),
        "sourceName": None,
        "riskLevel": int(r["risk_level"]),
        "entityType": r["entity_type"],
        "score": float(r["score"]) if r.get("score") is not None else None,
        "rankTs": float(r["rank_ts"]) if r.get("rank_ts") is not None else None,
        "snippet": r.get("snippet"),
        "chunkText": r.get("chunk_text"),
        "visibility": r.get("visibility"),
    }


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    log_handler_enter("search", event, context)
    try:
        ensure_data_backend()
    except (SupabaseConfigError, DatabaseConfigError) as e:
        return _response(
            503,
            {"code": "BACKEND_NOT_CONFIGURED", "message": str(e)},
        )

    body = _body(event)
    try:
        client_id = int(body["clientId"])
    except (KeyError, TypeError, ValueError):
        return _response(400, {"code": "BAD_REQUEST", "message": "clientId requerido (entero)."})

    jwt_msg = require_jwt(event)
    if isinstance(jwt_msg, str):
        return _response(401, {"code": "UNAUTHORIZED", "message": jwt_msg})
    if not jwt_allows_client(jwt_msg, client_id):
        return _response(403, {"code": "FORBIDDEN", "message": "clientId no permitido para este token."})

    q = (body.get("query") or "").strip()
    rfc = body.get("rfc")
    if isinstance(rfc, str):
        rfc = rfc.strip() or None
    else:
        rfc = None
    if not q and not rfc:
        return _response(
            400,
            {"code": "BAD_REQUEST", "message": "Envía query y/o rfc."},
        )

    entity_types = body.get("entityTypes")
    if entity_types is not None and not isinstance(entity_types, list):
        return _response(400, {"code": "BAD_REQUEST", "message": "entityTypes debe ser lista."})
    risk_levels = body.get("riskLevels")
    if risk_levels is not None:
        if not isinstance(risk_levels, list):
            return _response(400, {"code": "BAD_REQUEST", "message": "riskLevels debe ser lista."})
        try:
            risk_levels = [int(x) for x in risk_levels]
        except (TypeError, ValueError):
            return _response(400, {"code": "BAD_REQUEST", "message": "riskLevels: enteros."})

    try:
        lim = int(body.get("limit", 20))
    except (TypeError, ValueError):
        lim = 20
    lim = max(1, min(lim, 100))

    try:
        wst = float(body.get("wordSimilarityThreshold", 0.38))
    except (TypeError, ValueError):
        wst = 0.38

    try:
        log_phase("search", "invoke_search_risk_entities")
        raw_rows = invoke_search_risk_entities(
            client_id=client_id,
            query=q,
            rfc=rfc,
            entity_types=entity_types,
            risk_levels=risk_levels,
            limit=lim,
            word_similarity_threshold=wst,
        )
    except SupabaseRestError as e:
        return _response(
            e.status if 400 <= e.status < 600 else 500,
            {"code": "SEARCH_ERROR", "message": e.body},
        )

    results = [_map_row(r) for r in raw_rows]
    out: dict[str, Any] = {
        "clientId": client_id,
        "requestId": body.get("requestId"),
        "results": results,
        "backend": "postgresql" if is_database_mode() else "supabase",
    }
    return _response(200, out)
