-- CJF / SISE legal mentions (informational source; not risk_entity_chunks).
-- Applied via scripts/apply_migrations.sh

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS public.cjf_documents (
  documento_id TEXT PRIMARY KEY,
  url_sise TEXT,
  tema TEXT,
  sintesis TEXT,
  numero_expediente TEXT,
  materia TEXT,
  fecha_sentencia TEXT,
  tipo_asunto TEXT,
  tipo_organo TEXT,
  circuito TEXT,
  especialidad_organo TEXT,
  asunto_neun_id TEXT,
  numero_orden TEXT,
  sintesis_orden TEXT,
  datos_generales TEXT,
  documento_disponible TEXT,
  procesado_en TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS public.cjf_mentions (
  id BIGSERIAL PRIMARY KEY,
  documento_id TEXT NOT NULL REFERENCES public.cjf_documents(documento_id) ON DELETE CASCADE,
  nombre TEXT NOT NULL,
  nombre_norm TEXT NOT NULL,
  tipo TEXT,
  rol TEXT,
  extraccion_fuente TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_cjf_mentions_documento ON public.cjf_mentions(documento_id);
CREATE INDEX IF NOT EXISTS idx_cjf_mentions_tipo ON public.cjf_mentions(tipo);
CREATE INDEX IF NOT EXISTS idx_cjf_mentions_nombre_norm ON public.cjf_mentions(nombre_norm);
CREATE INDEX IF NOT EXISTS idx_cjf_mentions_nombre_trgm ON public.cjf_mentions USING gin (nombre_norm gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_cjf_documents_tema_trgm ON public.cjf_documents USING gin (tema gin_trgm_ops);

COMMENT ON TABLE public.cjf_documents IS 'CJF/SISE judicial documents — Legal source (not catalog chunks)';
COMMENT ON TABLE public.cjf_mentions IS 'PF/PM mentions extracted from CJF documents';

CREATE OR REPLACE FUNCTION public.search_cjf_mentions(
  q TEXT,
  lim INT DEFAULT 25,
  tipo_filter TEXT DEFAULT NULL
)
RETURNS TABLE (
  mention_id BIGINT,
  documento_id TEXT,
  nombre TEXT,
  nombre_norm TEXT,
  tipo TEXT,
  rol TEXT,
  extraccion_fuente TEXT,
  score REAL,
  url_sise TEXT,
  tema TEXT,
  sintesis TEXT,
  numero_expediente TEXT,
  materia TEXT,
  fecha_sentencia TEXT,
  tipo_asunto TEXT
)
LANGUAGE sql
STABLE
AS $$
  SELECT
    m.id AS mention_id,
    m.documento_id,
    m.nombre,
    m.nombre_norm,
    m.tipo,
    m.rol,
    m.extraccion_fuente,
    similarity(m.nombre_norm, lower(trim(q)))::real AS score,
    d.url_sise,
    d.tema,
    d.sintesis,
    d.numero_expediente,
    d.materia,
    d.fecha_sentencia,
    d.tipo_asunto
  FROM public.cjf_mentions m
  JOIN public.cjf_documents d ON d.documento_id = m.documento_id
  WHERE length(trim(q)) >= 2
    AND (
      m.nombre_norm % lower(trim(q))
      OR m.nombre_norm ILIKE '%' || lower(trim(q)) || '%'
    )
    AND (tipo_filter IS NULL OR m.tipo = tipo_filter)
  ORDER BY similarity(m.nombre_norm, lower(trim(q))) DESC, m.id ASC
  LIMIT GREATEST(1, LEAST(COALESCE(lim, 25), 100));
$$;
