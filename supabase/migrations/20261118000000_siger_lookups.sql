-- SIGER (Registro Público de Comercio): partners of the companies the clients screen, looked up by the
-- nuwa-siger container. Each lookup pays fees (~$736 MXN), so one row per (client, entity) and the
-- UNIQUE key is what stops a second paid lookup of the same company.

CREATE TABLE IF NOT EXISTS public.siger_lookups (
  id            BIGSERIAL PRIMARY KEY,
  client_id     INTEGER NOT NULL,
  entity_id     UUID NOT NULL REFERENCES public.entities(id) ON DELETE CASCADE,
  report_folio  TEXT,                            -- report the partners were added to
  search_name   TEXT,                            -- name as the client screened it ("Gráficas Corona")
  rfc           TEXT,                            -- RFC of the company chosen in SIGER (or the client's)
  legal_name    TEXT,                            -- full legal name chosen in SIGER ("Gráficas Corona J.E., S.A. de C.V.")
  legal_key     TEXT,                            -- legal_name normalized, legal form kept: exact reuse
  base_key      TEXT,                            -- legal_name without legal form: only candidates, never auto-reuse
  aliases       JSONB NOT NULL DEFAULT '[]'::jsonb,  -- names that resolved to this company
  match_reason  TEXT,                            -- rfc | exact_name | report_context | single_result | reused
  status        TEXT NOT NULL CHECK (status IN ('running', 'ok', 'no_match', 'multiple', 'error')),
  attempts      INTEGER NOT NULL DEFAULT 1,
  fems          JSONB NOT NULL DEFAULT '[]'::jsonb,   -- folios mercantiles of the chosen company
  candidates    JSONB NOT NULL DEFAULT '[]'::jsonb,   -- SIGER results considered when there was more than one
  partners      JSONB NOT NULL DEFAULT '[]'::jsonb,   -- partners as returned by SIGER
  cost          NUMERIC(10, 2),
  error         TEXT,
  reused_from   BIGINT REFERENCES public.siger_lookups(id) ON DELETE SET NULL,  -- copied, not paid
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (client_id, entity_id)
);

CREATE INDEX IF NOT EXISTS idx_siger_lookups_client
  ON public.siger_lookups (client_id, updated_at DESC);

-- SIGER is a public registry: a company already looked up for one client is reused for another, but
-- only by RFC or by the exact legal name with its legal form. A name without legal form can match
-- several companies, so base_key only offers candidates the container validates against the report.
CREATE INDEX IF NOT EXISTS idx_siger_lookups_rfc
  ON public.siger_lookups (upper(rfc)) WHERE rfc IS NOT NULL AND reused_from IS NULL;
CREATE INDEX IF NOT EXISTS idx_siger_lookups_legal_key
  ON public.siger_lookups (legal_key) WHERE legal_key IS NOT NULL AND reused_from IS NULL;
CREATE INDEX IF NOT EXISTS idx_siger_lookups_base_key
  ON public.siger_lookups (base_key) WHERE base_key IS NOT NULL AND reused_from IS NULL;

COMMENT ON TABLE public.siger_lookups IS 'SIGER: una consulta pagada por empresa (cliente, entidad); empresa elegida, alias, socios y FEM';
