-- RBAC + compañías + usuarios de aplicación + reportes (Nuwa 2.0)
-- Orden: roles → companies → nuwa_users → reports
-- Contraseña seed usuario id=1: "ChangeMe!" (pbkdf2_sha256) — cambiar en producción.

CREATE TABLE IF NOT EXISTS public.nuwa_roles (
  id smallserial PRIMARY KEY,
  slug text NOT NULL UNIQUE,
  name text NOT NULL
);

INSERT INTO public.nuwa_roles (slug, name) VALUES
  ('super_admin', 'Super Admin'),
  ('admin', 'Admin'),
  ('user', 'User')
ON CONFLICT (slug) DO NOTHING;

CREATE TABLE IF NOT EXISTS public.companies (
  id bigserial PRIMARY KEY,
  client_id integer NOT NULL UNIQUE,
  name text NOT NULL,
  details jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION public.set_companies_updated_at()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN NEW.updated_at = now(); RETURN NEW; END; $$;

DROP TRIGGER IF EXISTS trg_companies_updated_at ON public.companies;
CREATE TRIGGER trg_companies_updated_at
  BEFORE UPDATE ON public.companies FOR EACH ROW EXECUTE FUNCTION public.set_companies_updated_at();

CREATE TABLE IF NOT EXISTS public.nuwa_users (
  id bigserial PRIMARY KEY,
  client_id integer NOT NULL REFERENCES public.companies (client_id) ON DELETE RESTRICT,
  email text NOT NULL,
  password_hash text NOT NULL,
  full_name text NOT NULL,
  role_id smallint NOT NULL REFERENCES public.nuwa_roles (id),
  is_active boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (client_id, email)
);

CREATE OR REPLACE FUNCTION public.set_nuwa_users_updated_at()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN NEW.updated_at = now(); RETURN NEW; END; $$;

DROP TRIGGER IF EXISTS trg_nuwa_users_updated_at ON public.nuwa_users;
CREATE TRIGGER trg_nuwa_users_updated_at
  BEFORE UPDATE ON public.nuwa_users FOR EACH ROW EXECUTE FUNCTION public.set_nuwa_users_updated_at();

CREATE INDEX IF NOT EXISTS idx_nuwa_users_client_id ON public.nuwa_users (client_id);
CREATE INDEX IF NOT EXISTS idx_nuwa_users_role_id ON public.nuwa_users (role_id);

CREATE TABLE IF NOT EXISTS public.reports (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  folio text NOT NULL,
  client_id integer NOT NULL,
  created_by_user_id bigint NOT NULL REFERENCES public.nuwa_users (id) ON DELETE RESTRICT,
  report_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  search_context jsonb NOT NULL DEFAULT '{}'::jsonb,
  title text,
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'archived', 'deleted')),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (client_id, folio)
);

CREATE OR REPLACE FUNCTION public.set_reports_updated_at()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN NEW.updated_at = now(); RETURN NEW; END; $$;

DROP TRIGGER IF EXISTS trg_reports_updated_at ON public.reports;
CREATE TRIGGER trg_reports_updated_at
  BEFORE UPDATE ON public.reports FOR EACH ROW EXECUTE FUNCTION public.set_reports_updated_at();

CREATE INDEX IF NOT EXISTS idx_reports_client_id ON public.reports (client_id);
CREATE INDEX IF NOT EXISTS idx_reports_created_by ON public.reports (created_by_user_id);
CREATE INDEX IF NOT EXISTS idx_reports_folio ON public.reports (folio);
CREATE INDEX IF NOT EXISTS idx_reports_status ON public.reports (status);
CREATE INDEX IF NOT EXISTS idx_reports_created_at ON public.reports (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_reports_report_json_gin ON public.reports USING gin (report_json);

-- Seed: compañía Nuwa + super admin (userId=1, clientId=1)
INSERT INTO public.companies (client_id, name, details)
VALUES (1, 'Nuwa', '{"type":"platform"}'::jsonb)
ON CONFLICT (client_id) DO NOTHING;

INSERT INTO public.nuwa_users (id, client_id, email, password_hash, full_name, role_id)
VALUES (
  1,
  1,
  'admin@nuwa.local',
  'pbkdf2_sha256$62d2d8c7f9b444e5940f6ededf5af065$5a85445dbbcae175efcf249c3c92ea34a9e0020aa7320ffd84fed4172b90fcfa',
  'Nuwa Super Admin',
  (SELECT id FROM public.nuwa_roles WHERE slug = 'super_admin' LIMIT 1)
)
ON CONFLICT (id) DO NOTHING;

SELECT setval(
  pg_get_serial_sequence('public.nuwa_users', 'id'),
  GREATEST((SELECT COALESCE(MAX(id), 1) FROM public.nuwa_users), 1)
);

ALTER TABLE public.companies ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.nuwa_users ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.reports ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.nuwa_roles ENABLE ROW LEVEL SECURITY;
