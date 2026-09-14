-- Allow worker/BFF to persist monitoring failures as alerts.
ALTER TABLE public.entity_alerts
  DROP CONSTRAINT IF EXISTS entity_alerts_alert_type_check;

ALTER TABLE public.entity_alerts
  ADD CONSTRAINT entity_alerts_alert_type_check CHECK (alert_type IN (
    'risk_change', 'new_match', 'new_media_mention', 'status_change', 'run_failed'
  ));
