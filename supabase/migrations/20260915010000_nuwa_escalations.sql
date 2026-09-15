-- Tenant compliance escalations (numeric client_id). Also CREATE IF NOT EXISTS in nuwa_escalations_pg.py.

CREATE TABLE IF NOT EXISTS public.nuwa_escalations (
  id            TEXT PRIMARY KEY,
  client_id     INTEGER NOT NULL,
  entity_name   TEXT NOT NULL,
  entity_id     TEXT,
  client_name   TEXT,
  context       TEXT NOT NULL DEFAULT 'screening',
  risk_level    TEXT NOT NULL DEFAULT 'high',
  actions       JSONB NOT NULL DEFAULT '[]'::jsonb,
  notes         TEXT NOT NULL DEFAULT '',
  priority      TEXT NOT NULL DEFAULT 'urgent',
  status        TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'resolved')),
  quick_resolve BOOLEAN NOT NULL DEFAULT false,
  report_id     TEXT,
  notify_email  TEXT,
  action_path   TEXT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  resolved_at   TIMESTAMPTZ,
  resolution    JSONB,
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_nuwa_escalations_client_created
  ON public.nuwa_escalations (client_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_nuwa_escalations_client_status
  ON public.nuwa_escalations (client_id, status);
