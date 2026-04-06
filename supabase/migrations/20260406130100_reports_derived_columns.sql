-- Campos derivados del JSON del reporte para listar/filtrar sin parsear report_json en cada lectura.

ALTER TABLE public.reports
  ADD COLUMN IF NOT EXISTS entidad text,
  ADD COLUMN IF NOT EXISTS tipo_consulta text,
  ADD COLUMN IF NOT EXISTS fecha date,
  ADD COLUMN IF NOT EXISTS hora text,
  ADD COLUMN IF NOT EXISTS nivel_riesgo text,
  ADD COLUMN IF NOT EXISTS nivel_riesgo_numerico smallint,
  ADD COLUMN IF NOT EXISTS total_listas_original integer,
  ADD COLUMN IF NOT EXISTS total_listas_activas integer,
  ADD COLUMN IF NOT EXISTS total_descartadas integer,
  ADD COLUMN IF NOT EXISTS es_actualizacion boolean,
  ADD COLUMN IF NOT EXISTS total_listas integer,
  ADD COLUMN IF NOT EXISTS total_menciones integer,
  ADD COLUMN IF NOT EXISTS grok_resumen text,
  ADD COLUMN IF NOT EXISTS grok_falsos_positivos integer,
  ADD COLUMN IF NOT EXISTS grok_confirmados integer;

CREATE INDEX IF NOT EXISTS idx_reports_user_client_created
  ON public.reports (created_by_user_id, client_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_reports_entidad ON public.reports (entidad);
CREATE INDEX IF NOT EXISTS idx_reports_tipo_consulta ON public.reports (tipo_consulta);
CREATE INDEX IF NOT EXISTS idx_reports_fecha ON public.reports (fecha DESC);
