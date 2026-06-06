-- Documentos internos del cliente (S3 + metadata Postgres)

CREATE TABLE IF NOT EXISTS public.client_storage_profiles (
  client_id          INTEGER PRIMARY KEY REFERENCES public.companies(client_id) ON DELETE RESTRICT,
  s3_prefix          TEXT NOT NULL,
  initialized_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  initialized_by     TEXT NOT NULL DEFAULT 'system'
);

CREATE TABLE IF NOT EXISTS public.documents (
  document_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id          INTEGER NOT NULL REFERENCES public.companies(client_id) ON DELETE RESTRICT,
  user_id            BIGINT NOT NULL REFERENCES public.nuwa_users(id) ON DELETE RESTRICT,
  filename           TEXT NOT NULL,
  original_filename  TEXT NOT NULL,
  mime_type          TEXT,
  file_size_bytes    BIGINT,
  file_type          TEXT NOT NULL DEFAULT 'otro'
    CHECK (file_type IN (
      'contract','legal','compliance','financial','kyc','corporativo',
      'fiscal','identificacion','interno','otro'
    )),
  category           TEXT,
  description        TEXT,
  tags               JSONB NOT NULL DEFAULT '[]'::jsonb,
  s3_bucket          TEXT NOT NULL,
  s3_key             TEXT NOT NULL,
  status             TEXT NOT NULL DEFAULT 'pending_upload'
    CHECK (status IN ('pending_upload','uploaded','processing','ready','failed','deleted')),
  extracted_json     JSONB,
  extracted_text     TEXT,
  summary            TEXT,
  document_date      DATE,
  primary_entity_id  UUID REFERENCES public.entities(id) ON DELETE SET NULL,
  source_id          BIGINT REFERENCES public.sources(id) ON DELETE SET NULL,
  request_id         TEXT,
  error_message      TEXT,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  deleted_at         TIMESTAMPTZ,
  UNIQUE (client_id, s3_key)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_documents_client_request_id
  ON public.documents (client_id, request_id)
  WHERE request_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_documents_client_created
  ON public.documents (client_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_documents_client_status
  ON public.documents (client_id, status);
CREATE INDEX IF NOT EXISTS idx_documents_primary_entity
  ON public.documents (primary_entity_id)
  WHERE primary_entity_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_documents_extracted_gin
  ON public.documents USING gin (extracted_json jsonb_path_ops);
CREATE INDEX IF NOT EXISTS idx_documents_fts
  ON public.documents USING gin (to_tsvector('spanish', COALESCE(extracted_text, '')));

CREATE TABLE IF NOT EXISTS public.document_entity_links (
  link_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id          INTEGER NOT NULL REFERENCES public.companies(client_id) ON DELETE RESTRICT,
  document_id        UUID NOT NULL REFERENCES public.documents(document_id) ON DELETE CASCADE,
  entity_id          UUID NOT NULL REFERENCES public.entities(id) ON DELETE CASCADE,
  role               TEXT,
  is_primary         BOOLEAN NOT NULL DEFAULT FALSE,
  confidence         NUMERIC(5,2),
  mention_source     TEXT NOT NULL DEFAULT 'grok'
    CHECK (mention_source IN ('grok','manual','match')),
  mention_payload    JSONB,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (document_id, entity_id)
);

CREATE INDEX IF NOT EXISTS idx_doc_entity_links_document ON public.document_entity_links (document_id);
CREATE INDEX IF NOT EXISTS idx_doc_entity_links_entity ON public.document_entity_links (entity_id);
CREATE INDEX IF NOT EXISTS idx_doc_entity_links_client ON public.document_entity_links (client_id);

CREATE OR REPLACE FUNCTION public.trg_documents_set_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN NEW.updated_at := now(); RETURN NEW; END; $$;

DROP TRIGGER IF EXISTS trg_documents_updated_at ON public.documents;
CREATE TRIGGER trg_documents_updated_at
  BEFORE UPDATE ON public.documents FOR EACH ROW EXECUTE FUNCTION public.trg_documents_set_updated_at();

-- category document_mention para entidades auto-creadas desde documentos
ALTER TABLE public.entities DROP CONSTRAINT IF EXISTS entities_category_check;
ALTER TABLE public.entities ADD CONSTRAINT entities_category_check CHECK (category IN (
  'screening', 'background_check', 'employee', 'director',
  'vendor', 'client', 'associate', 'pep', 'representative', 'beneficial_owner',
  'document_mention'
));

COMMENT ON TABLE public.documents IS 'Metadata de documentos; binarios en S3.';
COMMENT ON TABLE public.document_entity_links IS 'Menciones/partes extraídas vinculadas a entities.';
