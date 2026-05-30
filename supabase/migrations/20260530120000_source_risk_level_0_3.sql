-- Catálogo de fuentes y chunks: cuatro niveles de riesgo (0–3), alineado con el front.
-- 0 = bajo, 1 = medio, 2 = alto, 3 = crítico

ALTER TABLE public.sources
  DROP CONSTRAINT IF EXISTS sources_risk_level_check;

ALTER TABLE public.sources
  ADD CONSTRAINT sources_risk_level_check CHECK (risk_level BETWEEN 0 AND 3);

ALTER TABLE public.risk_entity_chunks
  DROP CONSTRAINT IF EXISTS risk_entity_chunks_risk_level_check;

ALTER TABLE public.risk_entity_chunks
  ADD CONSTRAINT risk_entity_chunks_risk_level_check CHECK (risk_level BETWEEN 0 AND 3);

COMMENT ON COLUMN public.sources.risk_level IS '0=bajo, 1=medio, 2=alto, 3=crítico';
COMMENT ON COLUMN public.risk_entity_chunks.risk_level IS '0=bajo, 1=medio, 2=alto, 3=crítico; hereda de sources si ingest omite riskLevel';
