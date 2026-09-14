-- Tenant-scoped platform audit (numeric client_id). Also CREATE IF NOT EXISTS in nuwa_audit_pg.py.

CREATE TABLE IF NOT EXISTS nuwa_audit_events (
  id TEXT PRIMARY KEY,
  client_id INTEGER NOT NULL,
  user_id INTEGER,
  user_name TEXT NOT NULL DEFAULT '',
  user_role TEXT NOT NULL DEFAULT '',
  event_type TEXT NOT NULL,
  category TEXT NOT NULL,
  target TEXT NOT NULL DEFAULT '',
  target_id TEXT,
  details TEXT NOT NULL DEFAULT '',
  metadata JSONB,
  ip_address TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_nuwa_audit_client_created
  ON nuwa_audit_events (client_id, created_at DESC);
