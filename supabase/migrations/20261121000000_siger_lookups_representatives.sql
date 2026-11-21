-- SIGER: the container also sends the company's current legal representative(s), next to the partners.
-- Same shape as partners (name, personKind, rfc, curp, role, fem, ...). A person can be in both lists.

ALTER TABLE public.siger_lookups
  ADD COLUMN IF NOT EXISTS representatives JSONB NOT NULL DEFAULT '[]'::jsonb;  -- legal representatives as returned by SIGER

COMMENT ON COLUMN public.siger_lookups.representatives IS 'SIGER: representante(s) legal(es) vigente(s) de la empresa, mismo formato que partners más role';
