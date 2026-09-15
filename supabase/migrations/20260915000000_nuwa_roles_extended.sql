-- Extended app roles for platform admin invite/update (slug lookup, not hardcoded ids).
INSERT INTO public.nuwa_roles (slug, name) VALUES
  ('compliance_officer', 'Compliance Officer'),
  ('analyst', 'Analyst'),
  ('viewer', 'Viewer')
ON CONFLICT (slug) DO NOTHING;
