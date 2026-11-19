-- SIGER (Registro Público de Comercio): partners of the companies the clients screen, looked up by the
-- nuwa-siger container. Each lookup pays fees (~$736 MXN), so one row per (client, entity) and the
-- UNIQUE key is what stops a second paid lookup of the same company.

CREATE TABLE IF NOT EXISTS public.siger_lookups (
  id            BIGSERIAL PRIMARY KEY,
  client_id     INTEGER NOT NULL,
  entity_id     UUID NOT NULL REFERENCES public.entities(id) ON DELETE CASCADE,
  report_folio  TEXT,                            -- report the partners were added to
  rfc           TEXT,
  query_name    TEXT,                            -- legal name used for the lookup
  status        TEXT NOT NULL CHECK (status IN ('running', 'ok', 'no_match', 'multiple', 'error')),
  attempts      INTEGER NOT NULL DEFAULT 1,
  fems          JSONB NOT NULL DEFAULT '[]'::jsonb,   -- folios mercantiles found
  partners      JSONB NOT NULL DEFAULT '[]'::jsonb,   -- partners as returned by SIGER
  cost          NUMERIC(10, 2),
  error         TEXT,
  name_key      TEXT,                            -- normalized legal name (reuse across clients)
  reused_from   BIGINT REFERENCES public.siger_lookups(id) ON DELETE SET NULL,  -- copied, not paid
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (client_id, entity_id)
);

CREATE INDEX IF NOT EXISTS idx_siger_lookups_client
  ON public.siger_lookups (client_id, updated_at DESC);

-- SIGER is a public registry: a company already looked up for one client is reused for another
-- (by RFC, or by exact legal name) instead of paying again.
CREATE INDEX IF NOT EXISTS idx_siger_lookups_rfc
  ON public.siger_lookups (upper(rfc)) WHERE rfc IS NOT NULL AND reused_from IS NULL;
CREATE INDEX IF NOT EXISTS idx_siger_lookups_name_key
  ON public.siger_lookups (name_key) WHERE name_key IS NOT NULL AND reused_from IS NULL;

COMMENT ON TABLE public.siger_lookups IS 'SIGER: una consulta pagada por empresa (cliente, entidad); socios y FEM encontrados';
