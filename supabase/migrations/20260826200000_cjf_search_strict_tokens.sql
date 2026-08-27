-- Tighten CJF name search: require all significant query tokens in nombre_norm.
-- Fixes false positives matching only one apellido (e.g. Rodríguez).

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
  WITH
  qn AS (
    SELECT lower(trim(q)) AS qnorm
  ),
  -- Significant tokens: length >= 3, drop common Spanish/legal fillers
  q_tokens AS (
    SELECT t.token
    FROM qn,
    LATERAL unnest(string_to_array(qn.qnorm, ' ')) AS t(token)
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
      similarity(m.nombre_norm, (SELECT qnorm FROM qn))::real AS score,
      d.url_sise,
      d.tema,
      d.sintesis,
      d.numero_expediente,
      d.materia,
      d.fecha_sentencia,
      d.tipo_asunto
    FROM public.cjf_mentions m
    JOIN public.cjf_documents d ON d.documento_id = m.documento_id
    CROSS JOIN qn
    CROSS JOIN token_count tc
    WHERE length(qn.qnorm) >= 2
      AND (tipo_filter IS NULL OR m.tipo = tipo_filter)
      AND (
        -- Full query contained (best case)
        m.nombre_norm LIKE '%' || qn.qnorm || '%'
        OR (
          -- All significant tokens present as whole words
          tc.n >= 2
          AND NOT EXISTS (
            SELECT 1
            FROM q_tokens qt
            WHERE position(' ' || qt.token || ' ' IN ' ' || m.nombre_norm || ' ') = 0
          )
          AND similarity(m.nombre_norm, qn.qnorm) >= 0.35
        )
        OR (
          -- Single-token query: require high similarity or containment
          tc.n = 1
          AND (
            m.nombre_norm LIKE '%' || (SELECT token FROM q_tokens LIMIT 1) || '%'
          )
          AND similarity(m.nombre_norm, qn.qnorm) >= 0.55
        )
        OR (
          -- No significant tokens (very short query): fall back to high trigram only
          tc.n = 0
          AND similarity(m.nombre_norm, qn.qnorm) >= 0.6
        )
      )
  )
  SELECT *
  FROM candidates
  ORDER BY score DESC, mention_id ASC
  LIMIT GREATEST(1, LEAST(COALESCE(lim, 25), 100));
$$;

COMMENT ON FUNCTION public.search_cjf_mentions(TEXT, INT, TEXT) IS
  'CJF legal name search — requires all significant query tokens (len>=3) in nombre_norm to reduce apellido-only false positives';
