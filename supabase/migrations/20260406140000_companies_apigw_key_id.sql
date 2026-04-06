-- API Gateway: una API key por compañía (mapeo id de key AWS → tenant).
-- El valor secreto de la key no se guarda en BD; solo el id devuelto por AWS.

ALTER TABLE public.companies
  ADD COLUMN IF NOT EXISTS apigw_key_id text;

CREATE UNIQUE INDEX IF NOT EXISTS companies_apigw_key_id_key
  ON public.companies (apigw_key_id)
  WHERE apigw_key_id IS NOT NULL;

COMMENT ON COLUMN public.companies.apigw_key_id IS 'ID de API Key en API Gateway (x-api-key); NULL = pendiente de provisionar o legado.';
