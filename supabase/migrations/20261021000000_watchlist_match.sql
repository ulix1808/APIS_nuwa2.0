-- Early-warning lists, phase 2: daily match of the clients' entities, partners and representatives against
-- our OFAC copy and the high-risk SAT sources (watchlist-match Lambda, 06:00 CDMX).

-- New alert type for the bell / Alertas: "Coincidencia en lista".
ALTER TABLE public.entity_alerts
  DROP CONSTRAINT IF EXISTS entity_alerts_alert_type_check;
ALTER TABLE public.entity_alerts
  ADD CONSTRAINT entity_alerts_alert_type_check CHECK (alert_type IN (
    'risk_change', 'new_match', 'new_media_mention', 'status_change', 'run_failed', 'list_match'
  ));

-- "Otro identificador" of an entity (foreign IDs: cédula, NIT, pasaporte…): [{type, value}].
ALTER TABLE public.entities
  ADD COLUMN IF NOT EXISTS other_ids JSONB NOT NULL DEFAULT '[]'::jsonb;

-- One row per (entity, subject, list record) ever matched: dedupe across days and the "do not repeat a
-- dismissed alert while the list record does not change" rule.
CREATE TABLE IF NOT EXISTS public.watchlist_matches (
  id               BIGSERIAL PRIMARY KEY,
  client_id        INTEGER NOT NULL,
  entity_id        UUID NOT NULL REFERENCES public.entities(id) ON DELETE CASCADE,
  subject_key      TEXT NOT NULL,                  -- 'entity' or 'rel:<name key>'
  subject_name     TEXT NOT NULL,
  subject_role     TEXT,                           -- shareholder, legal_rep, ubo, officer, business_partner
  list_code        TEXT NOT NULL,                  -- 'ofac' | 'sat'
  record_ref       TEXT NOT NULL,                  -- OFAC FixedRef, or '<source_id>:<RFC>' for SAT
  record_hash      TEXT NOT NULL,
  record_name      TEXT NOT NULL,
  matched_by       TEXT NOT NULL,                  -- rfc | curp | <id type> | rfc_curp | name | name_partial | name_fuzzy
  severity         TEXT NOT NULL CHECK (severity IN ('high', 'medium')),
  status           TEXT NOT NULL CHECK (status IN ('alerted', 'in_report', 'dismissed')),
  alert_id         UUID REFERENCES public.entity_alerts(id) ON DELETE SET NULL,
  run_id           BIGINT REFERENCES public.watchlist_runs(id) ON DELETE SET NULL,
  details          JSONB NOT NULL DEFAULT '{}'::jsonb,
  first_matched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_matched_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (entity_id, subject_key, list_code, record_ref)
);

CREATE INDEX IF NOT EXISTS idx_watchlist_matches_client
  ON public.watchlist_matches (client_id, last_matched_at DESC);
CREATE INDEX IF NOT EXISTS idx_watchlist_matches_alert
  ON public.watchlist_matches (alert_id) WHERE alert_id IS NOT NULL;

COMMENT ON TABLE public.watchlist_matches IS 'Alerta temprana: coincidencias por entidad/socio y registro de lista (dedupe y descartes)';
