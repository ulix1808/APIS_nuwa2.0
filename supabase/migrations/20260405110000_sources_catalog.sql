-- Catálogo de fuentes (Supabase). Debe aplicarse ANTES de risk_entity_chunks (FK).
-- OpenAPI: openapi/openapi.yaml

CREATE TABLE IF NOT EXISTS public.sources (
  id bigserial PRIMARY KEY,

  name text NOT NULL,

  risk_level smallint NOT NULL CHECK (risk_level BETWEEN 1 AND 3),

  visibility text NOT NULL CHECK (visibility IN ('public', 'private')),

  client_id integer NOT NULL,

  created_by_user_id integer NOT NULL,

  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,

  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE public.sources IS 'Catálogo Nuwa 2.0; admin Nuwa (clientId=1,userId=1) → fuentes públicas';

CREATE OR REPLACE FUNCTION public.set_sources_updated_at()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_sources_updated_at ON public.sources;
CREATE TRIGGER trg_sources_updated_at
  BEFORE UPDATE ON public.sources
  FOR EACH ROW EXECUTE FUNCTION public.set_sources_updated_at();

CREATE INDEX IF NOT EXISTS idx_sources_client_id ON public.sources (client_id);
CREATE INDEX IF NOT EXISTS idx_sources_visibility ON public.sources (visibility);

ALTER TABLE public.sources ENABLE ROW LEVEL SECURITY;
