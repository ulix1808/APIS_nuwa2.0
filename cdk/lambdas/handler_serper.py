"""
POST /v1/search/serper — SerperGoogSearch (Google adverse via Serper + HTML enrich).

Devuelve hits listos para el pass Grok google_serper_seeded del BFF.
Lambda fuera de VPC (salida a Internet). Secreto: NUWA_SERPER_SECRET_ARN o SERPER_API_KEY.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from nuwa_api_auth import jwt_allows_client, require_jwt
from nuwa_http import json_response
from nuwa_obs_log import log_handler_enter, log_phase
from nuwa_serper_search import run_serper_goog_search


def _body(event: dict[str, Any]) -> dict[str, Any]:
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    try:
        return json.loads(raw) if isinstance(raw, str) else {}
    except json.JSONDecodeError:
        return {}


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    log_handler_enter("serper", event, context)
    body = _body(event)

    try:
        client_id = int(body["clientId"])
    except (KeyError, TypeError, ValueError):
        return json_response(400, {"code": "BAD_REQUEST", "message": "clientId requerido (entero)."})

    jwt_msg = require_jwt(event)
    if isinstance(jwt_msg, str):
        return json_response(401, {"code": "UNAUTHORIZED", "message": jwt_msg})
    if not jwt_allows_client(jwt_msg, client_id):
        return json_response(403, {"code": "FORBIDDEN", "message": "clientId no permitido para este token."})

    query = str(body.get("searchQuery") or body.get("query") or "").strip()
    if len(query) < 3:
        return json_response(400, {"code": "BAD_REQUEST", "message": "searchQuery/query requerido (min 3)."})

    extras = body.get("extraKeywords") or body.get("industryTerms") or []
    if extras is not None and not isinstance(extras, list):
        return json_response(400, {"code": "BAD_REQUEST", "message": "extraKeywords debe ser lista."})
    extra_keywords = [str(x).strip() for x in extras if str(x).strip()][:8]

    fetch_html = body.get("fetchHtml", True)
    if not isinstance(fetch_html, bool):
        fetch_html = str(fetch_html).lower() not in ("0", "false", "no")

    log_phase("serper", f"query={query[:80]!r} extras={len(extra_keywords)} fetchHtml={fetch_html}")
    result = run_serper_goog_search(query, extra_keywords=extra_keywords, fetch_html=fetch_html)

    if result.get("success") is False and result.get("code") == "SERPER_NOT_CONFIGURED":
        return json_response(503, result)

    return json_response(
        200,
        {
            "success": True,
            "hits": result.get("hits") or [],
            "allHits": result.get("allHits") or [],
            "meta": result.get("meta") or {},
        },
    )
