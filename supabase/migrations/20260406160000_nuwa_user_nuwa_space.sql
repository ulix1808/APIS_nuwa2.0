-- Compañía Nuwa (client_id=1) y super admin id=1 con email nuwa@nuwa.space.
-- Hash pbkdf2_sha256 (100k iteraciones, nuwa_password.hash_password); contraseña en texto plano: rotar en prod tras primer login.
-- Aplicar con psql / apply_migrations.sh tras RBAC seed.

INSERT INTO public.companies (client_id, name, details)
VALUES (1, 'Nuwa', '{"type":"platform"}'::jsonb)
ON CONFLICT (client_id) DO UPDATE SET
  name = EXCLUDED.name,
  updated_at = now();

INSERT INTO public.nuwa_users (id, client_id, email, password_hash, full_name, role_id)
VALUES (
  1,
  1,
  'nuwa@nuwa.space',
  'pbkdf2_sha256$fff56556ebc4e18873aeb7e4cf854f9a$3d66c38de03071662f0dc4102032e851e6f2aa861f431b2aed82b68f685826b8',
  'Nuwa Super Admin',
  (SELECT id FROM public.nuwa_roles WHERE slug = 'super_admin' LIMIT 1)
)
ON CONFLICT (id) DO UPDATE SET
  client_id = EXCLUDED.client_id,
  email = EXCLUDED.email,
  password_hash = EXCLUDED.password_hash,
  full_name = EXCLUDED.full_name,
  role_id = EXCLUDED.role_id,
  updated_at = now();

SELECT setval(
  pg_get_serial_sequence('public.nuwa_users', 'id'),
  GREATEST((SELECT COALESCE(MAX(id), 1) FROM public.nuwa_users), 1)
);
