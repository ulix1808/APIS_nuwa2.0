"""Postgres helpers for CJF / SISE legal source (not catalog chunks)."""

from __future__ import annotations

from typing import Any

from chunk_normalize import normalize_chunk_search_text
from nuwa_obs_log import log_phase
from nuwa_pg_dispatch import _conn


def search_cjf_mentions_pg(
    *,
    query: str,
    limit: int = 25,
    tipo_filter: str | None = None,
) -> list[dict[str, Any]]:
    q = normalize_chunk_search_text(query)
    if len(q) < 2:
        return []
    lim = max(1, min(int(limit), 100))
    log_phase("search_cjf_mentions_pg", f"q_len={len(q)} limit={lim} tipo={tipo_filter}")
    sql = """
    SELECT * FROM public.search_cjf_mentions(%s, %s, %s)
    """
    with _conn() as conn:
        rows = conn.execute(sql, [q, lim, tipo_filter]).fetchall()
    return [dict(r) for r in rows]


def upsert_cjf_documents_pg(documents: list[dict[str, Any]]) -> int:
    if not documents:
        return 0
    sql = """
    INSERT INTO public.cjf_documents (
      documento_id, url_sise, tema, sintesis, numero_expediente, materia,
      fecha_sentencia, tipo_asunto, tipo_organo, circuito, especialidad_organo,
      asunto_neun_id, numero_orden, sintesis_orden, datos_generales,
      documento_disponible, procesado_en, updated_at
    ) VALUES (
      %(documento_id)s, %(url_sise)s, %(tema)s, %(sintesis)s, %(numero_expediente)s,
      %(materia)s, %(fecha_sentencia)s, %(tipo_asunto)s, %(tipo_organo)s, %(circuito)s,
      %(especialidad_organo)s, %(asunto_neun_id)s, %(numero_orden)s, %(sintesis_orden)s,
      %(datos_generales)s, %(documento_disponible)s, %(procesado_en)s, NOW()
    )
    ON CONFLICT (documento_id) DO UPDATE SET
      url_sise = EXCLUDED.url_sise,
      tema = EXCLUDED.tema,
      sintesis = EXCLUDED.sintesis,
      numero_expediente = EXCLUDED.numero_expediente,
      materia = EXCLUDED.materia,
      fecha_sentencia = EXCLUDED.fecha_sentencia,
      tipo_asunto = EXCLUDED.tipo_asunto,
      tipo_organo = EXCLUDED.tipo_organo,
      circuito = EXCLUDED.circuito,
      especialidad_organo = EXCLUDED.especialidad_organo,
      asunto_neun_id = EXCLUDED.asunto_neun_id,
      numero_orden = EXCLUDED.numero_orden,
      sintesis_orden = EXCLUDED.sintesis_orden,
      datos_generales = EXCLUDED.datos_generales,
      documento_disponible = EXCLUDED.documento_disponible,
      procesado_en = EXCLUDED.procesado_en,
      updated_at = NOW()
    """
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.executemany(sql, documents)
        conn.commit()
    return len(documents)


def replace_cjf_mentions_pg(documento_ids: list[str], mentions: list[dict[str, Any]]) -> int:
    if not documento_ids and not mentions:
        return 0
    with _conn() as conn:
        if documento_ids:
            conn.execute(
                "DELETE FROM public.cjf_mentions WHERE documento_id = ANY(%s)",
                [documento_ids],
            )
        if mentions:
            sql = """
            INSERT INTO public.cjf_mentions (
              documento_id, nombre, nombre_norm, tipo, rol, extraccion_fuente
            ) VALUES (
              %(documento_id)s, %(nombre)s, %(nombre_norm)s, %(tipo)s, %(rol)s, %(extraccion_fuente)s
            )
            """
            with conn.cursor() as cur:
                cur.executemany(sql, mentions)
        conn.commit()
    return len(mentions)


def cjf_stats_pg() -> dict[str, int]:
    with _conn() as conn:
        docs = conn.execute("SELECT COUNT(*)::int AS c FROM public.cjf_documents").fetchone()["c"]
        ments = conn.execute("SELECT COUNT(*)::int AS c FROM public.cjf_mentions").fetchone()["c"]
    return {"documentCount": int(docs), "mentionCount": int(ments)}
