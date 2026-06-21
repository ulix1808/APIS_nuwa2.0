-- Fuentes y chunks auto-creados por documents/finalize: risk_level 0 (sin riesgo de lista).
-- Aplica a filas históricas con metadata.documentId o nombre doc:%

UPDATE public.sources s
SET risk_level = 0,
    updated_at = now()
WHERE (
  (s.metadata ? 'documentId' AND COALESCE(s.metadata->>'documentId', '') <> '')
  OR s.name LIKE 'doc:%'
)
AND s.risk_level <> 0;

UPDATE public.risk_entity_chunks c
SET risk_level = 0,
    updated_at = now()
FROM public.sources s
WHERE c.source_id = s.id
  AND (
    (s.metadata ? 'documentId' AND COALESCE(s.metadata->>'documentId', '') <> '')
    OR s.name LIKE 'doc:%'
  )
  AND c.risk_level <> 0;
