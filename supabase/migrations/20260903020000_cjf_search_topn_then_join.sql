-- Join documents only for top-N mention hits (avoid hashing thousands of docs).

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
  PERFORM set_config('pg_trgm.similarity_threshold', '0.45', true);

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
  mention_hits AS (
    SELECT
      m.id AS mention_id,
      m.documento_id,
      m.nombre,
      m.nombre_norm,
      m.tipo,
      m.rol,
      m.extraccion_fuente,
      similarity(m.nombre_norm, qnorm)::real AS score
    FROM public.cjf_mentions m
    CROSS JOIN token_count tc
    WHERE (tipo_filter IS NULL OR m.tipo = tipo_filter)
      AND (
        CASE
          WHEN q_has_fts THEN m.fts_nombre @@ q_ts
          ELSE m.nombre_norm % qnorm
        END
      )
      AND (
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
    ORDER BY score DESC, m.id ASC
    LIMIT lim_i
  )
  SELECT
    h.mention_id,
    h.documento_id,
    h.nombre,
    h.nombre_norm,
    h.tipo,
    h.rol,
    h.extraccion_fuente,
    h.score,
    d.url_sise,
    left(d.tema, 600) AS tema,
    left(d.sintesis, 600) AS sintesis,
    d.numero_expediente,
    d.materia,
    d.fecha_sentencia,
    d.tipo_asunto
  FROM mention_hits h
  JOIN public.cjf_documents d ON d.documento_id = h.documento_id
  ORDER BY h.score DESC, h.mention_id ASC;
END;
$$;

COMMENT ON FUNCTION public.search_cjf_mentions(TEXT, INT, TEXT) IS
  'CJF legal name search — FTS candidates, top-N then join docs; tema/sintesis truncated';
