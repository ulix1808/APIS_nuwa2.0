-- Nuwa 2.0 — índice de búsqueda para risk entities (Supabase / PostgreSQL)
-- Requiere migración previa: 20260405110000_sources_catalog.sql (FK source_id → sources.id)
-- Ingest / chunking (CSV, TXT, PDF, Vercel): ver docs/INGEST_CHUNKING.md
--
-- risk_level: 1 = low, 2 = medium, 3 = high
-- source_id: BIGINT = id del catálogo (al borrar fuente → DELETE chunks WHERE source_id = …)
--
-- No hay columna aliases: el usuario solo envía el nombre a buscar en runtime; el índice guarda
-- el texto de cada chunk (p. ej. una fila SAT serializada). La búsqueda difusa va sobre chunk_text.
--
-- word_similarity(consulta, chunk_text): encaja nombres embebidos en texto largo / CSV aplanado en ingest.
-- fts sobre chunk_text: tokens correctos sin typo.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS public.risk_entity_chunks (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  client_id integer NOT NULL,

  risk_level smallint NOT NULL CHECK (risk_level BETWEEN 1 AND 3),

  source_id bigint NOT NULL REFERENCES public.sources (id) ON DELETE CASCADE,

  entity_type text NOT NULL,

  chunk_text text NOT NULL,

  visibility text NOT NULL DEFAULT 'private' CHECK (visibility IN ('public', 'private')),

  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE public.risk_entity_chunks IS 'Un chunk = trozo indexable de la fuente (p. ej. fila/registro); búsqueda solo sobre chunk_text';

CREATE OR REPLACE FUNCTION public.set_updated_at()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_risk_entity_chunks_updated_at ON public.risk_entity_chunks;
CREATE TRIGGER trg_risk_entity_chunks_updated_at
  BEFORE UPDATE ON public.risk_entity_chunks
  FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

ALTER TABLE public.risk_entity_chunks
  ADD COLUMN IF NOT EXISTS fts tsvector
  GENERATED ALWAYS AS (to_tsvector('simple', coalesce(chunk_text, ''))) STORED;

CREATE INDEX IF NOT EXISTS idx_risk_entity_chunks_fts
  ON public.risk_entity_chunks USING gin (fts);

CREATE INDEX IF NOT EXISTS idx_risk_entity_chunks_chunk_trgm
  ON public.risk_entity_chunks USING gin (chunk_text gin_trgm_ops);

CREATE INDEX IF NOT EXISTS idx_risk_entity_chunks_source_id
  ON public.risk_entity_chunks (source_id);

CREATE INDEX IF NOT EXISTS idx_risk_entity_chunks_client_id
  ON public.risk_entity_chunks (client_id);

CREATE INDEX IF NOT EXISTS idx_risk_entity_chunks_entity_type
  ON public.risk_entity_chunks (entity_type);

CREATE INDEX IF NOT EXISTS idx_risk_entity_chunks_risk_level
  ON public.risk_entity_chunks (risk_level);

CREATE OR REPLACE FUNCTION public.search_risk_entities(
  p_client_id integer,
  p_query text DEFAULT '',
  p_rfc text DEFAULT NULL,
  p_entity_types text[] DEFAULT NULL,
  p_risk_levels smallint[] DEFAULT NULL,
  p_limit integer DEFAULT 20,
  p_word_similarity_threshold real DEFAULT 0.38
)
RETURNS TABLE (
  id uuid,
  client_id integer,
  risk_level smallint,
  source_id bigint,
  entity_type text,
  chunk_text text,
  visibility text,
  score real,
  rank_ts real,
  snippet text
)
LANGUAGE plpgsql
STABLE
AS $$
DECLARE
  q text;
  r text;
  q_ts tsquery;
  q_ts_rfc tsquery;
BEGIN
  q := trim(coalesce(p_query, ''));
  r := upper(regexp_replace(trim(coalesce(p_rfc, '')), '[\s-]+', '', 'g'));
  IF r = '' THEN
    r := NULL;
  END IF;

  IF q = '' AND r IS NULL THEN
    RETURN;
  END IF;

  PERFORM set_config('pg_trgm.word_similarity_threshold', p_word_similarity_threshold::text, true);

  IF q <> '' THEN
    q_ts := websearch_to_tsquery('simple', q);
  ELSE
    q_ts := NULL;
  END IF;

  IF r IS NOT NULL THEN
    q_ts_rfc := plainto_tsquery('simple', r);
  ELSE
    q_ts_rfc := NULL;
  END IF;

  RETURN QUERY
  SELECT
    c.id,
    c.client_id,
    c.risk_level,
    c.source_id,
    c.entity_type,
    c.chunk_text,
    c.visibility,
    greatest(
      CASE
        WHEN q <> '' THEN
          greatest(
            word_similarity(q, c.chunk_text)::real,
            CASE
              WHEN q_ts IS NOT NULL AND coalesce(numnode(q_ts), 0) > 0 AND c.fts @@ q_ts
                THEN COALESCE(ts_rank_cd(c.fts, q_ts), 0)::real
              ELSE 0::real
            END
          )
        ELSE 0::real
      END,
      CASE
        WHEN r IS NOT NULL THEN
          greatest(
            word_similarity(r, c.chunk_text)::real,
            CASE
              WHEN strpos(
                regexp_replace(upper(c.chunk_text), '[\s-]+', '', 'g'),
                r
              ) > 0 THEN 0.95::real
              ELSE 0::real
            END,
            CASE
              WHEN q_ts_rfc IS NOT NULL AND coalesce(numnode(q_ts_rfc), 0) > 0 AND c.fts @@ q_ts_rfc
                THEN COALESCE(ts_rank_cd(c.fts, q_ts_rfc), 0)::real
              ELSE 0::real
            END
          )
        ELSE 0::real
      END
    ) AS score,
    (
      CASE
        WHEN q_ts IS NOT NULL AND coalesce(numnode(q_ts), 0) > 0 AND c.fts @@ q_ts
          THEN COALESCE(ts_rank_cd(c.fts, q_ts), 0)::real
        ELSE 0::real
      END
      +
      CASE
        WHEN q_ts_rfc IS NOT NULL AND coalesce(numnode(q_ts_rfc), 0) > 0 AND c.fts @@ q_ts_rfc
          THEN COALESCE(ts_rank_cd(c.fts, q_ts_rfc), 0)::real
        ELSE 0::real
      END
    )::real AS rank_ts,
    CASE
      WHEN q_ts IS NOT NULL AND coalesce(numnode(q_ts), 0) > 0 AND c.fts @@ q_ts THEN
        ts_headline(
          'simple',
          c.chunk_text,
          q_ts,
          'StartSel=<mark>, StopSel=</mark>, MaxFragments=1, MaxWords=48, MinWords=10, ShortWord=2'
        )
      WHEN q_ts_rfc IS NOT NULL AND coalesce(numnode(q_ts_rfc), 0) > 0 AND c.fts @@ q_ts_rfc THEN
        ts_headline(
          'simple',
          c.chunk_text,
          q_ts_rfc,
          'StartSel=<mark>, StopSel=</mark>, MaxFragments=1, MaxWords=48, MinWords=10, ShortWord=2'
        )
      WHEN length(c.chunk_text) <= 520 THEN
        c.chunk_text
      ELSE
        left(c.chunk_text, 520) || '…'
    END AS snippet
  FROM public.risk_entity_chunks c
  WHERE
    (c.visibility = 'public' OR c.client_id = p_client_id)
    AND (p_entity_types IS NULL OR c.entity_type = ANY (p_entity_types))
    AND (p_risk_levels IS NULL OR c.risk_level = ANY (p_risk_levels))
    AND (
      (
        q <> ''
        AND (
          (q_ts IS NOT NULL AND coalesce(numnode(q_ts), 0) > 0 AND c.fts @@ q_ts)
          OR word_similarity(q, c.chunk_text) >= p_word_similarity_threshold
        )
      )
      OR (
        r IS NOT NULL
        AND (
          word_similarity(r, c.chunk_text) >= p_word_similarity_threshold
          OR strpos(regexp_replace(upper(c.chunk_text), '[\s-]+', '', 'g'), r) > 0
          OR (
            q_ts_rfc IS NOT NULL
            AND coalesce(numnode(q_ts_rfc), 0) > 0
            AND c.fts @@ q_ts_rfc
          )
        )
      )
    )
  ORDER BY score DESC, c.updated_at DESC
  LIMIT LEAST(GREATEST(p_limit, 1), 100);
END;
$$;

COMMENT ON FUNCTION public.search_risk_entities IS
  'Nombre y/o RFC sobre chunk_text (word_similarity + fts). Alcance: visibility=public OR client_id=p_client_id; sin sourceIds desde el cliente.';

ALTER TABLE public.risk_entity_chunks ENABLE ROW LEVEL SECURITY;
