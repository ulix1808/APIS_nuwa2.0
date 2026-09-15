-- Attribution fields for who created an escalation (BFF audit trail).
ALTER TABLE public.nuwa_escalations
  ADD COLUMN IF NOT EXISTS created_by_user_id TEXT,
  ADD COLUMN IF NOT EXISTS created_by_name TEXT,
  ADD COLUMN IF NOT EXISTS created_by_email TEXT;
