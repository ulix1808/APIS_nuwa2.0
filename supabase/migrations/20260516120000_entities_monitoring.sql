-- Nuwa 2.0 — Entidades de negocio (PF/PM) y monitoreo continuo
-- Alineado con docs/PLAN_ENTIDADES.md

-- ---------------------------------------------------------------------------
-- entities
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.entities (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id           INTEGER NOT NULL REFERENCES public.companies(client_id) ON DELETE RESTRICT,
  created_by_user_id  BIGINT NOT NULL REFERENCES public.nuwa_users(id) ON DELETE RESTRICT,
  updated_by_user_id  BIGINT REFERENCES public.nuwa_users(id) ON DELETE SET NULL,

  -- Nombre canónico para listados (PF: nombres+apellidos; PM: razón social)
  name                TEXT NOT NULL,
  name_normalized     TEXT NOT NULL,
  party_type          TEXT NOT NULL CHECK (party_type IN ('individual', 'organization')),
  -- Etiqueta UI / reportes: Persona física | Persona moral
  party_type_label    TEXT NOT NULL CHECK (party_type_label IN ('Persona física', 'Persona moral')),
  -- PF: campos del formulario de consulta
  first_name          TEXT,
  last_name           TEXT,
  -- PM: razón social / denominación
  legal_name          TEXT,
  category            TEXT NOT NULL DEFAULT 'screening' CHECK (category IN (
    'screening', 'background_check', 'employee', 'director',
    'vendor', 'client', 'associate', 'pep', 'representative', 'beneficial_owner'
  )),

  rfc                 TEXT,
  curp                TEXT,
  country             TEXT,

  risk_level          TEXT CHECK (risk_level IN ('low', 'medium', 'high', 'critical')),
  status              TEXT NOT NULL DEFAULT 'active' CHECK (status IN (
    'active', 'under_review', 'flagged', 'cleared', 'inactive', 'deleted'
  )),

  -- Rol / Relación (catálogo distinto PF vs PM; ver docs/PLAN_ENTIDADES.md)
  relationship_role   TEXT,
  -- Sujeto hijo de una PM padre (screening múltiple con "Asociar a una empresa")
  parent_entity_id    UUID REFERENCES public.entities(id) ON DELETE SET NULL,
  -- Screening múltiple: nombre en un solo campo (antes de parsear first/last o legal_name)
  full_name           TEXT,

  last_screening_at   TIMESTAMPTZ,
  last_report_folio   TEXT,
  metadata            JSONB NOT NULL DEFAULT '{}',

  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  deleted_at          TIMESTAMPTZ,
  deleted_by_user_id  BIGINT REFERENCES public.nuwa_users(id)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_entities_client_rfc
  ON public.entities (client_id, upper(rfc))
  WHERE rfc IS NOT NULL AND status <> 'deleted';

CREATE UNIQUE INDEX IF NOT EXISTS uq_entities_client_curp
  ON public.entities (client_id, upper(curp))
  WHERE curp IS NOT NULL AND status <> 'deleted';

CREATE INDEX IF NOT EXISTS idx_entities_client_status ON public.entities (client_id, status);
CREATE INDEX IF NOT EXISTS idx_entities_client_party ON public.entities (client_id, party_type);
CREATE INDEX IF NOT EXISTS idx_entities_client_category ON public.entities (client_id, category);
CREATE INDEX IF NOT EXISTS idx_entities_name_norm ON public.entities (client_id, name_normalized);
CREATE INDEX IF NOT EXISTS idx_entities_legal_name_norm ON public.entities (client_id, name_normalized)
  WHERE legal_name IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_entities_last_screening ON public.entities (client_id, last_screening_at DESC)
  WHERE status <> 'deleted';
CREATE INDEX IF NOT EXISTS idx_entities_parent ON public.entities (parent_entity_id)
  WHERE parent_entity_id IS NOT NULL;

-- PF: al menos nombre o identificador; PM: al menos razón social o RFC
ALTER TABLE public.entities ADD CONSTRAINT chk_entities_individual_fields CHECK (
  party_type <> 'individual'
  OR (first_name IS NOT NULL OR last_name IS NOT NULL OR rfc IS NOT NULL OR curp IS NOT NULL)
);
ALTER TABLE public.entities ADD CONSTRAINT chk_entities_organization_fields CHECK (
  party_type <> 'organization'
  OR (legal_name IS NOT NULL OR rfc IS NOT NULL)
);

CREATE OR REPLACE FUNCTION public.trg_entities_set_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_entities_updated_at ON public.entities;
CREATE TRIGGER trg_entities_updated_at
  BEFORE UPDATE ON public.entities
  FOR EACH ROW EXECUTE FUNCTION public.trg_entities_set_updated_at();

-- ---------------------------------------------------------------------------
-- entity_monitoring
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.entity_monitoring (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  entity_id           UUID NOT NULL REFERENCES public.entities(id) ON DELETE CASCADE,
  client_id           INTEGER NOT NULL REFERENCES public.companies(client_id) ON DELETE RESTRICT,

  is_enabled          BOOLEAN NOT NULL DEFAULT true,
  frequency           TEXT NOT NULL DEFAULT 'weekly' CHECK (frequency IN (
    'weekly', 'monthly', 'semi-annual', 'annual'
  )),
  sources             TEXT[] NOT NULL DEFAULT ARRAY['compliance','media']::TEXT[],

  last_run_at         TIMESTAMPTZ,
  next_run_at         TIMESTAMPTZ,
  last_run_status     TEXT CHECK (last_run_status IN ('ok', 'error', 'skipped')),
  last_error          TEXT,

  created_by_user_id  BIGINT NOT NULL REFERENCES public.nuwa_users(id) ON DELETE RESTRICT,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

  CONSTRAINT uq_entity_monitoring_entity UNIQUE (entity_id)
);

CREATE INDEX IF NOT EXISTS idx_entity_monitoring_due
  ON public.entity_monitoring (next_run_at)
  WHERE is_enabled = true;

CREATE INDEX IF NOT EXISTS idx_entity_monitoring_client
  ON public.entity_monitoring (client_id, is_enabled);

DROP TRIGGER IF EXISTS trg_entity_monitoring_updated_at ON public.entity_monitoring;
CREATE TRIGGER trg_entity_monitoring_updated_at
  BEFORE UPDATE ON public.entity_monitoring
  FOR EACH ROW EXECUTE FUNCTION public.trg_entities_set_updated_at();

-- ---------------------------------------------------------------------------
-- entity_monitoring_runs (log para scheduler)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.entity_monitoring_runs (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  monitoring_id       UUID NOT NULL REFERENCES public.entity_monitoring(id) ON DELETE CASCADE,
  entity_id           UUID NOT NULL REFERENCES public.entities(id) ON DELETE CASCADE,
  client_id           INTEGER NOT NULL REFERENCES public.companies(client_id) ON DELETE RESTRICT,
  started_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at         TIMESTAMPTZ,
  status              TEXT NOT NULL CHECK (status IN ('running', 'ok', 'error')),
  report_folio        TEXT,
  risk_level_before   TEXT,
  risk_level_after    TEXT,
  error_message       TEXT
);

CREATE INDEX IF NOT EXISTS idx_entity_monitoring_runs_entity
  ON public.entity_monitoring_runs (entity_id, started_at DESC);

-- ---------------------------------------------------------------------------
-- entity_alerts (fase alertas)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.entity_alerts (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id           INTEGER NOT NULL REFERENCES public.companies(client_id) ON DELETE RESTRICT,
  entity_id           UUID NOT NULL REFERENCES public.entities(id) ON DELETE CASCADE,
  alert_type          TEXT NOT NULL CHECK (alert_type IN (
    'risk_change', 'new_match', 'new_media_mention', 'status_change'
  )),
  severity            TEXT NOT NULL CHECK (severity IN ('low', 'medium', 'high')),
  title               TEXT NOT NULL,
  description         TEXT,
  payload             JSONB NOT NULL DEFAULT '{}',
  status              TEXT NOT NULL DEFAULT 'new' CHECK (status IN (
    'new', 'reviewed', 'dismissed', 'escalated'
  )),
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_entity_alerts_client_status
  ON public.entity_alerts (client_id, status, created_at DESC);

-- ---------------------------------------------------------------------------
-- reports.entity_id
-- ---------------------------------------------------------------------------
ALTER TABLE public.reports
  ADD COLUMN IF NOT EXISTS entity_id UUID REFERENCES public.entities(id) ON DELETE SET NULL;

ALTER TABLE public.reports
  ADD COLUMN IF NOT EXISTS parent_entity_id UUID REFERENCES public.entities(id) ON DELETE SET NULL;

ALTER TABLE public.reports
  ADD COLUMN IF NOT EXISTS group_id TEXT;

ALTER TABLE public.reports
  ADD COLUMN IF NOT EXISTS group_name TEXT;

ALTER TABLE public.reports
  ADD COLUMN IF NOT EXISTS group_role TEXT;

CREATE INDEX IF NOT EXISTS idx_reports_entity
  ON public.reports (client_id, entity_id)
  WHERE status = 'active';

CREATE INDEX IF NOT EXISTS idx_reports_group
  ON public.reports (client_id, group_id)
  WHERE group_id IS NOT NULL AND status = 'active';

COMMENT ON TABLE public.entities IS
  'Entidades de negocio (PF/PM) por tenant; vinculan consultas y reportes.';
COMMENT ON TABLE public.entity_monitoring IS
  'Configuración de monitoreo continuo por entidad (frecuencia y fuentes).';
COMMENT ON COLUMN public.entity_monitoring.sources IS
  'compliance = sanciones/PEPs/regulatorio; media = medios adversos.';
