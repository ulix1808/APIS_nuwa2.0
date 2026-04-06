"""
Metadatos derivados del JSON del reporte y utilidades de API (paginación, validación).
Las respuestas de listado usan camelCase; en Postgres las columnas son snake_case.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from typing import Any


def now_iso_z() -> str:
    return (
        datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )


def extract_report_metadata(reporte: dict[str, Any]) -> dict[str, Any]:
    metadatos = reporte.get("metadatos") or {}
    resumen = reporte.get("resumen") or {}
    grok = reporte.get("grokAnalisis") or {}

    def _int(v: Any) -> int | None:
        if v is None:
            return None
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    return {
        "folio": reporte.get("folio"),
        "entidad": reporte.get("entidad"),
        "tipo_consulta": reporte.get("tipoConsulta"),
        "fecha": reporte.get("fecha"),
        "hora": reporte.get("hora"),
        "nivel_riesgo": reporte.get("nivelRiesgo"),
        "nivel_riesgo_numerico": _int(reporte.get("nivelRiesgoNumerico")),
        "total_listas_original": _int(metadatos.get("totalListasOriginal")),
        "total_listas_activas": _int(metadatos.get("totalListasActivas")),
        "total_descartadas": _int(metadatos.get("totalDescartadas")),
        "es_actualizacion": bool(metadatos.get("esActualizacion"))
        if metadatos.get("esActualizacion") is not None
        else None,
        "total_listas": _int(resumen.get("totalListas")),
        "total_menciones": _int(resumen.get("totalMenciones")),
        "grok_resumen": grok.get("resumen"),
        "grok_falsos_positivos": _int(grok.get("falsosPositivos")),
        "grok_confirmados": _int(grok.get("confirmados")),
    }


def metadata_to_db_row(meta: dict[str, Any]) -> dict[str, Any]:
    """Mapeo a columnas SQL (snake_case). fecha como date string YYYY-MM-DD."""
    row: dict[str, Any] = {}
    if meta.get("entidad") is not None:
        row["entidad"] = meta["entidad"]
    if meta.get("tipo_consulta") is not None:
        row["tipo_consulta"] = meta["tipo_consulta"]
    if meta.get("fecha"):
        row["fecha"] = meta["fecha"]
    if meta.get("hora") is not None:
        row["hora"] = meta["hora"]
    if meta.get("nivel_riesgo") is not None:
        row["nivel_riesgo"] = meta["nivel_riesgo"]
    if meta.get("nivel_riesgo_numerico") is not None:
        row["nivel_riesgo_numerico"] = meta["nivel_riesgo_numerico"]
    if meta.get("total_listas_original") is not None:
        row["total_listas_original"] = meta["total_listas_original"]
    if meta.get("total_listas_activas") is not None:
        row["total_listas_activas"] = meta["total_listas_activas"]
    if meta.get("total_descartadas") is not None:
        row["total_descartadas"] = meta["total_descartadas"]
    if meta.get("es_actualizacion") is not None:
        row["es_actualizacion"] = meta["es_actualizacion"]
    if meta.get("total_listas") is not None:
        row["total_listas"] = meta["total_listas"]
    if meta.get("total_menciones") is not None:
        row["total_menciones"] = meta["total_menciones"]
    if meta.get("grok_resumen") is not None:
        row["grok_resumen"] = meta["grok_resumen"]
    if meta.get("grok_falsos_positivos") is not None:
        row["grok_falsos_positivos"] = meta["grok_falsos_positivos"]
    if meta.get("grok_confirmados") is not None:
        row["grok_confirmados"] = meta["grok_confirmados"]
    return row


def db_row_to_api_summary(row: dict[str, Any]) -> dict[str, Any]:
    """Resumen para listados (camelCase). `status` es el ciclo de vida del registro (active/archived/deleted)."""
    return {
        "folio": row.get("folio"),
        "clientId": row.get("client_id"),
        "userId": row.get("created_by_user_id"),
        "entidad": row.get("entidad"),
        "tipoConsulta": row.get("tipo_consulta"),
        "fecha": str(row["fecha"]) if row.get("fecha") is not None else None,
        "hora": row.get("hora"),
        "nivelRiesgo": row.get("nivel_riesgo"),
        "nivelRiesgoNumerico": row.get("nivel_riesgo_numerico"),
        "totalListasOriginal": row.get("total_listas_original"),
        "totalListasActivas": row.get("total_listas_activas"),
        "totalDescartadas": row.get("total_descartadas"),
        "esActualizacion": row.get("es_actualizacion"),
        "totalListas": row.get("total_listas"),
        "totalMenciones": row.get("total_menciones"),
        "grokResumen": row.get("grok_resumen"),
        "grokFalsosPositivos": row.get("grok_falsos_positivos"),
        "grokConfirmados": row.get("grok_confirmados"),
        "createdAt": row.get("created_at"),
        "updatedAt": row.get("updated_at"),
        "status": row.get("status"),
    }


def encode_next_key(offset: int) -> str:
    raw = json.dumps({"offset": offset})
    return base64.b64encode(raw.encode("utf-8")).decode("utf-8")


def decode_next_key(token: str | None) -> int | None:
    if not token:
        return None
    try:
        raw = base64.b64decode(token.encode("utf-8")).decode("utf-8")
        data = json.loads(raw)
        o = data.get("offset")
        return int(o) if o is not None else None
    except Exception:
        return None


def report_payload_from_body(body: dict[str, Any]) -> dict[str, Any] | None:
    """Prioridad: `report`, luego `reporte` (alias legado)."""
    r = body.get("report")
    if isinstance(r, dict):
        return r
    r = body.get("reporte")
    if isinstance(r, dict):
        return r
    return None


def validate_report_for_save(client_id: Any, user_id: Any, payload: Any) -> str | None:
    if not client_id:
        return "clientId es requerido"
    if not user_id:
        return "userId es requerido"
    if not payload or not isinstance(payload, dict):
        return "report (o reporte) es requerido y debe ser un objeto JSON"
    if not payload.get("folio"):
        return "report.folio es requerido"
    return None


def validate_report_for_update(folio: Any, payload: Any) -> str | None:
    if not folio:
        return "folio es requerido"
    if not payload or not isinstance(payload, dict):
        return "report (o reporte) es requerido y debe ser un objeto JSON"
    rf = payload.get("folio")
    if not rf:
        return "report.folio es requerido"
    if str(folio) != str(rf):
        return "folio del body debe coincidir con report.folio"
    return None
