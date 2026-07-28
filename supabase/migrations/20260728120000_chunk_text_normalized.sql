-- Texto normalizado para búsqueda en catálogo (ñ→n, sin acentos). Ver chunk_normalize.py / lib/chunk-search-normalize.ts

ALTER TABLE public.risk_entity_chunks
  ADD COLUMN IF NOT EXISTS chunk_text_normalized text;

COMMENT ON COLUMN public.risk_entity_chunks.chunk_text_normalized IS
  'Texto plano normalizado para word_similarity/FTS; también va en chunk JSON como chunk_text_normalized.';

CREATE INDEX IF NOT EXISTS idx_risk_entity_chunks_chunk_norm_trgm
  ON public.risk_entity_chunks USING gin (chunk_text_normalized gin_trgm_ops);

ALTER TABLE public.risk_entity_chunks
  ADD COLUMN IF NOT EXISTS fts_normalized tsvector
  GENERATED ALWAYS AS (to_tsvector('simple', coalesce(chunk_text_normalized, ''))) STORED;

CREATE INDEX IF NOT EXISTS idx_risk_entity_chunks_fts_normalized
  ON public.risk_entity_chunks USING gin (fts_normalized);

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
            word_similarity(
              q,
              coalesce(nullif(trim(c.chunk_text_normalized), ''), c.chunk_text)
            )::real,
            CASE
              WHEN q_ts IS NOT NULL AND coalesce(numnode(q_ts), 0) > 0
                AND (
                  (c.chunk_text_normalized IS NOT NULL AND trim(c.chunk_text_normalized) <> '' AND c.fts_normalized @@ q_ts)
                  OR c.fts @@ q_ts
                )
                THEN COALESCE(
                  CASE
                    WHEN c.chunk_text_normalized IS NOT NULL AND trim(c.chunk_text_normalized) <> ''
                      THEN ts_rank_cd(c.fts_normalized, q_ts)
                    ELSE ts_rank_cd(c.fts, q_ts)
                  END,
                  0
                )::real
              ELSE 0::real
            END
          )
        ELSE 0::real
      END,
      CASE
        WHEN r IS NOT NULL THEN
          greatest(
            word_similarity(
              r,
              coalesce(nullif(trim(c.chunk_text_normalized), ''), c.chunk_text)
            )::real,
            CASE
              WHEN strpos(
                regexp_replace(
                  upper(coalesce(nullif(trim(c.chunk_text_normalized), ''), c.chunk_text)),
                  '[\s-]+',
                  '',
                  'g'
                ),
                r
              ) > 0 THEN 0.95::real
              ELSE 0::real
            END,
            CASE
              WHEN q_ts_rfc IS NOT NULL AND coalesce(numnode(q_ts_rfc), 0) > 0
                AND (
                  (c.chunk_text_normalized IS NOT NULL AND trim(c.chunk_text_normalized) <> '' AND c.fts_normalized @@ q_ts_rfc)
                  OR c.fts @@ q_ts_rfc
                )
                THEN COALESCE(
                  CASE
                    WHEN c.chunk_text_normalized IS NOT NULL AND trim(c.chunk_text_normalized) <> ''
                      THEN ts_rank_cd(c.fts_normalized, q_ts_rfc)
                    ELSE ts_rank_cd(c.fts, q_ts_rfc)
                  END,
                  0
                )::real
              ELSE 0::real
            END
          )
        ELSE 0::real
      END
    ) AS score,
    (
      CASE
        WHEN q_ts IS NOT NULL AND coalesce(numnode(q_ts), 0) > 0
          AND c.chunk_text_normalized IS NOT NULL AND trim(c.chunk_text_normalized) <> ''
          AND c.fts_normalized @@ q_ts
          THEN COALESCE(ts_rank_cd(c.fts_normalized, q_ts), 0)::real
        WHEN q_ts IS NOT NULL AND coalesce(numnode(q_ts), 0) > 0 AND c.fts @@ q_ts
          THEN COALESCE(ts_rank_cd(c.fts, q_ts), 0)::real
        ELSE 0::real
      END
      +
      CASE
        WHEN q_ts_rfc IS NOT NULL AND coalesce(numnode(q_ts_rfc), 0) > 0
          AND c.chunk_text_normalized IS NOT NULL AND trim(c.chunk_text_normalized) <> ''
          AND c.fts_normalized @@ q_ts_rfc
          THEN COALESCE(ts_rank_cd(c.fts_normalized, q_ts_rfc), 0)::real
        WHEN q_ts_rfc IS NOT NULL AND coalesce(numnode(q_ts_rfc), 0) > 0 AND c.fts @@ q_ts_rfc
          THEN COALESCE(ts_rank_cd(c.fts, q_ts_rfc), 0)::real
        ELSE 0::real
      END
    )::real AS rank_ts,
    CASE
      WHEN q_ts IS NOT NULL AND coalesce(numnode(q_ts), 0) > 0
        AND c.chunk_text_normalized IS NOT NULL AND trim(c.chunk_text_normalized) <> ''
        AND c.fts_normalized @@ q_ts THEN
        ts_headline(
          'simple',
          c.chunk_text,
          q_ts,
          'StartSel=<mark>, StopSel=</mark>, MaxFragments=1, MaxWords=48, MinWords=10, ShortWord=2'
        )
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
          (
            c.chunk_text_normalized IS NOT NULL AND trim(c.chunk_text_normalized) <> ''
            AND (
              (q_ts IS NOT NULL AND coalesce(numnode(q_ts), 0) > 0 AND c.fts_normalized @@ q_ts)
              OR word_similarity(q, c.chunk_text_normalized) >= p_word_similarity_threshold
            )
          )
          OR (
            (c.chunk_text_normalized IS NULL OR trim(c.chunk_text_normalized) = '')
            AND (
              (q_ts IS NOT NULL AND coalesce(numnode(q_ts), 0) > 0 AND c.fts @@ q_ts)
              OR word_similarity(q, c.chunk_text) >= p_word_similarity_threshold
            )
          )
        )
      )
      OR (
        r IS NOT NULL
        AND (
          word_similarity(r, coalesce(nullif(trim(c.chunk_text_normalized), ''), c.chunk_text)) >= p_word_similarity_threshold
          OR strpos(
            regexp_replace(
              upper(coalesce(nullif(trim(c.chunk_text_normalized), ''), c.chunk_text)),
              '[\s-]+',
              '',
              'g'
            ),
            r
          ) > 0
          OR (
            q_ts_rfc IS NOT NULL AND coalesce(numnode(q_ts_rfc), 0) > 0
            AND (
              (c.chunk_text_normalized IS NOT NULL AND trim(c.chunk_text_normalized) <> '' AND c.fts_normalized @@ q_ts_rfc)
              OR c.fts @@ q_ts_rfc
            )
          )
        )
      )
    )
  ORDER BY score DESC, c.updated_at DESC
  LIMIT LEAST(GREATEST(p_limit, 1), 100);
END;
$$;

COMMENT ON FUNCTION public.search_risk_entities IS
  'Búsqueda sobre chunk_text_normalized (si existe) o chunk_text legacy. p_query debe venir normalizado (ñ→n, sin acentos).';
