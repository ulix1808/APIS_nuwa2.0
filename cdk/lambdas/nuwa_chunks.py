"""Ingest de chunks en public.risk_entity_chunks (PostgreSQL directo o PostgREST)."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from nuwa_config import is_database_mode
from nuwa_errors import SupabaseRestError
from nuwa_obs_log import log_phase
from nuwa_pg_dispatch import can_mutate_source_row, ingest_chunks_pg
from nuwa_sources import fetch_source_row_rest
from nuwa_supabase import rest_json


def ingest_chunks(
    *,
    source_id: int,
    viewer_client_id: int,
    is_super_admin: bool,
    replace_strategy: str,
    chunk_texts: list[str],
    risk_level: int | None,
    visibility: str | None,
    entity_type: str | None,
) -> dict[str, Any]:
    if is_database_mode():
        return ingest_chunks_pg(
            source_id,
            viewer_client_id=viewer_client_id,
            is_super_admin=is_super_admin,
            replace_strategy=replace_strategy,
            chunk_texts=chunk_texts,
            risk_level=risk_level,
            visibility=visibility,
            entity_type=entity_type,
        )
    return _ingest_chunks_rest(
        source_id,
        viewer_client_id=viewer_client_id,
        is_super_admin=is_super_admin,
        replace_strategy=replace_strategy,
        chunk_texts=chunk_texts,
        risk_level=risk_level,
        visibility=visibility,
        entity_type=entity_type,
    )


def _ingest_chunks_rest(
    source_id: int,
    *,
    viewer_client_id: int,
    is_super_admin: bool,
    replace_strategy: str,
    chunk_texts: list[str],
    risk_level: int | None,
    visibility: str | None,
    entity_type: str | None,
) -> dict[str, Any]:
    row0 = fetch_source_row_rest(source_id)
    if not row0:
        raise SupabaseRestError(404, "Fuente no encontrada.")
    if not can_mutate_source_row(row0, viewer_client_id, is_super_admin):
        raise SupabaseRestError(403, "Sin permiso para ingestar chunks en esta fuente.")
    if replace_strategy not in ("all", "append"):
        raise SupabaseRestError(400, "replaceStrategy debe ser all o append.")
    if not chunk_texts:
        raise SupabaseRestError(400, "chunks no puede estar vacío.")

    eff_rl = int(risk_level) if risk_level is not None else int(row0["risk_level"])
    eff_vis = visibility if visibility is not None else str(row0["visibility"])
    eff_et = (entity_type or "").strip() or "entity"
    if eff_rl not in (1, 2, 3):
        raise SupabaseRestError(400, "risk_level inválido.")
    if eff_vis not in ("public", "private"):
        raise SupabaseRestError(400, "visibility inválida.")

    client_id_src = int(row0["client_id"])
    deleted_chunks: int | None = None

    if replace_strategy == "all":
        q_count = urlencode(
            [
                ("select", "id"),
                ("source_id", f"eq.{source_id}"),
            ]
        )
        prev = rest_json("GET", "risk_entity_chunks", query=q_count)
        if isinstance(prev, list):
            deleted_chunks = len(prev)
        log_phase("chunks_ingest", f"DELETE risk_entity_chunks source_id={source_id}")
        rest_json("DELETE", "risk_entity_chunks", query=f"source_id=eq.{source_id}")

    batch = [
        {
            "client_id": client_id_src,
            "risk_level": eff_rl,
            "source_id": source_id,
            "entity_type": eff_et[:200],
            "chunk_text": txt,
            "visibility": eff_vis,
        }
        for txt in chunk_texts
    ]
    log_phase("chunks_ingest", f"POST {len(batch)} rows source_id={source_id}")
    rest_json("POST", "risk_entity_chunks", body=batch)

    out: dict[str, Any] = {
        "sourceId": source_id,
        "status": "completed",
        "insertedChunks": len(chunk_texts),
    }
    if replace_strategy == "all" and deleted_chunks is not None:
        out["deletedChunks"] = deleted_chunks
    return out
