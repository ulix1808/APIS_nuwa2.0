-- When each record was first and last published by its source (traceability for incremental loads).
-- A record whose content did not change keeps one row: its last_seen_at moves to the newest upload.
-- A record that changed is a new row; the old one stays with the last_seen_at of its last upload.
-- NULL = loaded before this column existed (use created_at).

ALTER TABLE public.risk_entity_chunks
  ADD COLUMN IF NOT EXISTS first_seen_at timestamptz,
  ADD COLUMN IF NOT EXISTS last_seen_at timestamptz;

COMMENT ON COLUMN public.risk_entity_chunks.first_seen_at IS 'Primera carga en que la fuente publicó este registro (NULL: usar created_at)';
COMMENT ON COLUMN public.risk_entity_chunks.last_seen_at IS 'Última carga en que la fuente publicó este registro sin cambios (NULL: usar created_at)';
