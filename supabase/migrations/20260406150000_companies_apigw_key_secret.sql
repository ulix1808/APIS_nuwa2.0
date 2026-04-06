-- Valor de x-api-key del tenant (texto plano) para que login/front puedan recuperarlo.
-- Riesgo: si la base se filtra, se filtran las keys. Preferible cifrado/KMS en evolución.

ALTER TABLE public.companies
  ADD COLUMN IF NOT EXISTS apigw_key_secret text;

COMMENT ON COLUMN public.companies.apigw_key_secret IS 'Secreto API Gateway (cabecera x-api-key) del tenant; no exponer en listados públicos.';
