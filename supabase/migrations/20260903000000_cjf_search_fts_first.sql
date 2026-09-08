-- Speed up CJF search after ~166k mentions.
-- Previous search_cjf_mentions evaluated LIKE/similarity/token checks on every row
-- (Seq Scan ~170–380ms). Same fix pattern as search_risk_entities FTS-first:
-- generate candidates via GIN FTS / trigram, then apply token filters on the subset.

ALTER TABLE public.cjf_mentions
  ADD COLUMN IF NOT EXISTS fts_nombre tsvector
  GENERATED ALWAYS AS (to_tsvector('simple', coalesce(nombre_norm, ''))) STORED;

CREATE INDEX IF NOT EXISTS idx_cjf_mentions_fts_nombre
  ON public.cjf_mentions USING gin (fts_nombre);

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
LANGUAGE plpgsql
STABLE
AS $$
DECLARE
  qnorm text;
  q_ts tsquery;
  q_has_fts boolean := false;
  lim_i int;
BEGIN
  qnorm := lower(trim(coalesce(q, '')));
  lim_i := GREATEST(1, LEAST(COALESCE(lim, 25), 100));
  IF length(qnorm) < 2 THEN
    RETURN;
  END IF;

  q_ts := plainto_tsquery('simple', qnorm);
  q_has_fts := q_ts IS NOT NULL AND coalesce(numnode(q_ts), 0) > 0;

  -- Prefer indexable candidate generation (FTS GIN / trigram %), then strict token filter.
  RETURN QUERY
  WITH
  q_tokens AS (
    SELECT t.token
    FROM unnest(string_to_array(qnorm, ' ')) AS t(token)
    WHERE length(t.token) >= 3
      AND t.token NOT IN (
        'del', 'los', 'las', 'una', 'uno', 'por', 'con', 'para',
        'que', 'sus', 'the', 'and', 'sociedad', 'anonima', 'limitada',
        'companias', 'compania'
      )
  ),
  token_count AS (
    SELECT COUNT(*)::int AS n FROM q_tokens
  ),
  candidates AS (
    SELECT
      m.id AS mention_id,
      m.documento_id,
      m.nombre,
      m.nombre_norm,
      m.tipo,
      m.rol,
      m.extraccion_fuente,
      similarity(m.nombre_norm, qnorm)::real AS score,
      d.url_sise,
      d.tema,
      d.sintesis,
      d.numero_expediente,
      d.materia,
      d.fecha_sentencia,
      d.tipo_asunto
    FROM public.cjf_mentions m
    JOIN public.cjf_documents d ON d.documento_id = m.documento_id
    CROSS JOIN token_count tc
    WHERE (tipo_filter IS NULL OR m.tipo = tipo_filter)
      AND (
        -- Index path 1: FTS GIN
        (q_has_fts AND m.fts_nombre @@ q_ts)
        -- Index path 2: trigram similarity operator (uses gin_trgm)
        OR (m.nombre_norm % qnorm)
        -- Index path 3: short/single-token containment via trigram
        OR (
          tc.n = 1
          AND m.nombre_norm LIKE (SELECT token FROM q_tokens LIMIT 1) || '%'
        )
      )
      AND (
        -- Strictness (same rules as 20260826200000), applied only to candidates
        m.nombre_norm LIKE '%' || qnorm || '%'
        OR (
          tc.n >= 2
          AND NOT EXISTS (
            SELECT 1
            FROM q_tokens qt
            WHERE position(' ' || qt.token || ' ' IN ' ' || m.nombre_norm || ' ') = 0
          )
          AND similarity(m.nombre_norm, qnorm) >= 0.35
        )
        OR (
          tc.n = 1
          AND m.nombre_norm LIKE '%' || (SELECT token FROM q_tokens LIMIT 1) || '%'
          AND similarity(m.nombre_norm, qnorm) >= 0.55
        )
        OR (
          tc.n = 0
          AND similarity(m.nombre_norm, qnorm) >= 0.6
        )
      )
  )
  SELECT *
  FROM candidates
  ORDER BY score DESC, mention_id ASC
  LIMIT lim_i;
END;
$$;

COMMENT ON FUNCTION public.search_cjf_mentions(TEXT, INT, TEXT) IS
  'CJF legal name search — FTS/trigram candidate generation + token strictness (fast on 100k+ mentions)';
