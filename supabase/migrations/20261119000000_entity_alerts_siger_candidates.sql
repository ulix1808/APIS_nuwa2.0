-- SIGER: when a company name matches several companies and neither the report nor Nuwa Intelligence can
-- tell which one was screened, the nuwa-siger container leaves an alert with the candidates and their
-- partners; the analyst picks one in Alertas ("Es esta").
ALTER TABLE public.entity_alerts
  DROP CONSTRAINT IF EXISTS entity_alerts_alert_type_check;
ALTER TABLE public.entity_alerts
  ADD CONSTRAINT entity_alerts_alert_type_check CHECK (alert_type IN (
    'risk_change', 'new_match', 'new_media_mention', 'status_change', 'run_failed', 'list_match',
    'siger_candidates'
  ));
