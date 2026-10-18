-- Source imports in the background (S3 + worker Lambda) with sync / incremental modes and history.
--
-- source_imports: one row per uploaded file; the worker writes progress and the result here
--   (the UI polls it). The original file stays in S3 at s3_key.
-- risk_entity_chunks: record_key (the record's identity in its source: RFC / CURP / folio, or
--   "h:<row_hash>" when the source has no key) and row_hash (hash of the normalized content, to tell
--   a real change from a re-upload of the same data).
-- risk_entity_chunk_versions: the previous text of a record that an import changed or removed.

CREATE TABLE IF NOT EXISTS public.source_imports (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source_id bigint NOT NULL REFERENCES public.sources (id) ON DELETE CASCADE,
  client_id integer NOT NULL,
  user_id integer NOT NULL,
  status text NOT NULL DEFAULT 'pending_upload'
    CHECK (status IN ('pending_upload', 'queued', 'processing', 'completed', 'failed')),
  mode text NOT NULL CHECK (mode IN ('sync', 'incremental', 'replace')),
  s3_key text NOT NULL,
  file_name text NOT NULL,
  file_size bigint,
  content_type text,
  -- headerRowIndex, sheetName, columnMapping {header: role}, keyField, repairText, riskLevel, entityType
  config jsonb NOT NULL DEFAULT '{}'::jsonb,
  -- {phase, rowsRead, rowsTotal, message}
  progress jsonb NOT NULL DEFAULT '{}'::jsonb,
  -- {rows, inserted, updated, unchanged, removed, duplicatesInFile, issues[], rowCount}
  result jsonb,
  error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  started_at timestamptz,
  finished_at timestamptz,
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_source_imports_source ON public.source_imports (source_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_source_imports_client ON public.source_imports (client_id, created_at DESC);

ALTER TABLE public.risk_entity_chunks
  ADD COLUMN IF NOT EXISTS record_key text,
  ADD COLUMN IF NOT EXISTS row_hash text,
  ADD COLUMN IF NOT EXISTS last_import_id uuid;

CREATE INDEX IF NOT EXISTS idx_risk_entity_chunks_source_record_key
  ON public.risk_entity_chunks (source_id, record_key);

CREATE TABLE IF NOT EXISTS public.risk_entity_chunk_versions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  chunk_id uuid,
  source_id bigint NOT NULL REFERENCES public.sources (id) ON DELETE CASCADE,
  record_key text,
  row_hash text,
  chunk_text text NOT NULL,
  -- updated: replaced by a newer version; removed: no longer in the source (sync);
  -- superseded: an older duplicate of the same key found when keys were first assigned
  change text NOT NULL CHECK (change IN ('updated', 'removed', 'superseded')),
  valid_from timestamptz,
  valid_to timestamptz NOT NULL DEFAULT now(),
  import_id uuid REFERENCES public.source_imports (id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_risk_entity_chunk_versions_key
  ON public.risk_entity_chunk_versions (source_id, record_key, valid_to DESC);

COMMENT ON TABLE public.source_imports IS 'Cargas de archivos de fuentes procesadas en segundo plano (S3 + Lambda worker)';
COMMENT ON TABLE public.risk_entity_chunk_versions IS 'Histórico: versión anterior de un registro cambiado o retirado por una carga';
COMMENT ON COLUMN public.risk_entity_chunks.record_key IS 'Identidad del registro en su fuente (RFC/CURP/folio normalizado) o h:<row_hash> sin llave';
COMMENT ON COLUMN public.risk_entity_chunks.row_hash IS 'Hash del contenido normalizado (fechas ISO, montos numéricos, sin acentos ni vacíos)';
