-- Si existía una columna antigua de estado de guardado, eliminarla (ciclo de vida en `status` + timestamps).
ALTER TABLE public.reports DROP COLUMN IF EXISTS report_save_state;
