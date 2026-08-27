"""Fuente Legal CJF/SISE — POST /v1/legal/cjf/search | /v1/legal/cjf/ingest | /v1/legal/cjf/stats."""

from __future__ import annotations

import base64
import json
from typing import Any

from chunk_normalize import normalize_chunk_search_text
from nuwa_api_auth import jwt_allows_client, require_jwt
from nuwa_config import DatabaseConfigError, SupabaseConfigError, ensure_data_backend, is_database_mode
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


def _path(event: dict[str, Any]) -> str:
    return str(event.get("path") or event.get("rawPath") or "")


def _map_hit(row: dict[str, Any], idx: int) -> dict[str, Any]:
    score_raw = row.get("score")
    match_score = 0
    if isinstance(score_raw, (int, float)):
        s = float(score_raw)
        match_score = int(round(s * 100)) if s <= 1 else min(100, int(round(s)))
    mention_id = row.get("mention_id")
    documento_id = str(row.get("documento_id") or "")
    return {
        "id": f"cjf-{mention_id if mention_id is not None else f'{documento_id}-{idx}'}",
        "documentoId": documento_id,
        "nombre": str(row.get("nombre") or ""),
        "tipo": row.get("tipo"),
        "rol": row.get("rol"),
        "extraccionFuente": row.get("extraccion_fuente"),
        "matchScore": match_score,
        "tema": row.get("tema"),
        "sintesis": row.get("sintesis"),
        "numeroExpediente": row.get("numero_expediente"),
        "materia": row.get("materia"),
        "fechaSentencia": row.get("fecha_sentencia"),
        "tipoAsunto": row.get("tipo_asunto"),
        "documentoUrlSise": row.get("url_sise"),
        "informational": True,
        "category": "legal",
        "matchedList": "CJF / SISE",
        "sourceKind": "cjf_legal",
    }


def _tipo_from_entity(entity_type: Any) -> str | None:
    if not isinstance(entity_type, str):
        return None
    t = entity_type.strip().lower()
    if t in ("individual", "persona", "pf"):
        return "persona"
    if t in ("organization", "empresa", "pm", "company"):
        return "empresa"
    return None


def _handle_search(body: dict[str, Any]) -> dict[str, Any]:
    from nuwa_legal_pg import search_cjf_mentions_pg

    q = normalize_chunk_search_text(str(body.get("query") or body.get("searchQuery") or "").strip())
    if len(q) < 2:
        return _response(400, {"code": "BAD_REQUEST", "message": "query requerido (min 2 chars)."})

    try:
        lim = int(body.get("limit", 25))
    except (TypeError, ValueError):
        lim = 25

    tipo = _tipo_from_entity(body.get("entityType") or body.get("tipo"))
    # Optional explicit override
    if isinstance(body.get("tipoFilter"), str) and body["tipoFilter"].strip():
        tipo = body["tipoFilter"].strip().lower()

    rows = search_cjf_mentions_pg(query=q, limit=lim, tipo_filter=tipo)
    hits = [_map_hit(r, i) for i, r in enumerate(rows)]
    return _response(
        200,
        {
            "success": True,
            "hits": hits,
            "status": "ok" if hits else "empty",
            "source": "postgres",
            "category": "legal",
            "matchedList": "CJF / SISE",
        },
    )


def _as_str(v: Any) -> str | None:
    if isinstance(v, str) and v.strip():
        return v.strip()
    if isinstance(v, (int, float)) and v == v:
        return str(v)
    return None


def _parse_compact(raw: Any, fallback_id: str | None = None) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
    if not isinstance(raw, dict):
        return None
    documento_id = _as_str(raw.get("documento_id")) or (fallback_id.strip() if fallback_id else None)
    if not documento_id:
        return None
    tabla = raw.get("tabla") if isinstance(raw.get("tabla"), dict) else {}

    def t(key: str) -> str | None:
        return _as_str(tabla.get(key))

    doc = {
        "documento_id": documento_id,
        "url_sise": _as_str(raw.get("documento_url_sise")),
        "tema": t("tema"),
        "sintesis": t("sintesis"),
        "numero_expediente": t("numero_expediente"),
        "materia": t("materia"),
        "fecha_sentencia": t("fecha_sentencia"),
        "tipo_asunto": t("tipo_asunto"),
        "tipo_organo": t("tipo_organo"),
        "circuito": t("circuito"),
        "especialidad_organo": t("especialidad_organo"),
        "asunto_neun_id": t("asunto_neun_id"),
        "numero_orden": t("numero_orden"),
        "sintesis_orden": t("sintesis_orden"),
        "datos_generales": t("datos_generales"),
        "documento_disponible": t("documento_disponible"),
        "procesado_en": _as_str(raw.get("procesado_en")),
    }

    mentions: list[dict[str, Any]] = []
    seen: set[str] = set()
    sujetos = raw.get("sujetos_empresas_mencionados")
    if isinstance(sujetos, list):
        for s in sujetos:
            if not isinstance(s, dict):
                continue
            nombre = _as_str(s.get("nombre"))
            if not nombre:
                continue
            nombre_norm = normalize_chunk_search_text(nombre)
            if len(nombre_norm) < 2:
                continue
            tipo = _as_str(s.get("tipo"))
            rol = _as_str(s.get("rol"))
            fuente = _as_str(s.get("fuente"))
            key = f"{nombre_norm}|{tipo or ''}|{rol or ''}"
            if key in seen:
                continue
            seen.add(key)
            mentions.append(
                {
                    "documento_id": documento_id,
                    "nombre": nombre,
                    "nombre_norm": nombre_norm,
                    "tipo": tipo,
                    "rol": rol,
                    "extraccion_fuente": fuente,
                }
            )
    return doc, mentions


def _handle_ingest(body: dict[str, Any]) -> dict[str, Any]:
    from nuwa_legal_pg import replace_cjf_mentions_pg, upsert_cjf_documents_pg

    items = body.get("documents") or body.get("items") or []
    if not isinstance(items, list) or not items:
        return _response(400, {"code": "BAD_REQUEST", "message": "documents[] requerido."})

    docs: list[dict[str, Any]] = []
    ments: list[dict[str, Any]] = []
    skipped = 0
    for item in items:
        parsed = _parse_compact(item)
        if not parsed:
            skipped += 1
            continue
        doc, mentions = parsed
        docs.append(doc)
        ments.extend(mentions)

    # Deduplicate docs by id (last wins)
    by_id = {d["documento_id"]: d for d in docs}
    docs = list(by_id.values())
    doc_ids = [d["documento_id"] for d in docs]

    upsert_cjf_documents_pg(docs)
    inserted = replace_cjf_mentions_pg(doc_ids, ments)
    return _response(
        200,
        {
            "success": True,
            "documentsUpserted": len(docs),
            "mentionsInserted": inserted,
            "skipped": skipped,
        },
    )


def _handle_stats() -> dict[str, Any]:
    from nuwa_legal_pg import cjf_stats_pg

    stats = cjf_stats_pg()
    return _response(200, {"success": True, **stats})


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    log_handler_enter("legal", event, context)
    try:
        ensure_data_backend()
    except (SupabaseConfigError, DatabaseConfigError) as e:
        return _response(503, {"code": "BACKEND_NOT_CONFIGURED", "message": str(e)})

    if not is_database_mode():
        return _response(
            503,
            {
                "code": "DATABASE_REQUIRED",
                "message": "Fuente Legal CJF requiere modo PostgreSQL (NUWA_DATABASE_*).",
            },
        )

    body = _body(event)
    path = _path(event)

    # Auth: search/stats need client JWT; ingest can use same JWT + clientId
    try:
        client_id = int(body.get("clientId")) if body.get("clientId") is not None else None
    except (TypeError, ValueError):
        return _response(400, {"code": "BAD_REQUEST", "message": "clientId inválido."})

    jwt_msg = require_jwt(event)
    if isinstance(jwt_msg, str):
        return _response(401, {"code": "UNAUTHORIZED", "message": jwt_msg})
    if client_id is not None and not jwt_allows_client(jwt_msg, client_id):
        return _response(403, {"code": "FORBIDDEN", "message": "clientId no permitido para este token."})

    try:
        if path.endswith("/legal/cjf/stats") or path.endswith("/legal/stats"):
            log_phase("legal", "stats")
            return _handle_stats()
        if path.endswith("/legal/cjf/ingest") or path.endswith("/legal/ingest"):
            log_phase("legal", "ingest")
            if client_id is None:
                return _response(400, {"code": "BAD_REQUEST", "message": "clientId requerido."})
            return _handle_ingest(body)
        # default search
        log_phase("legal", "search")
        if client_id is None:
            return _response(400, {"code": "BAD_REQUEST", "message": "clientId requerido."})
        return _handle_search(body)
    except SupabaseRestError as e:
        return _response(e.status, {"code": "UPSTREAM_ERROR", "message": e.body if hasattr(e, "body") else str(e)})
    except Exception as e:  # noqa: BLE001
        return _response(500, {"code": "INTERNAL", "message": str(e)})
