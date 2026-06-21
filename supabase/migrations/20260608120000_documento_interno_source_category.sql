-- Categoría dedicada para fuentes auto-creadas por documents/finalize (no mezclar con sanciones/PEP).

INSERT INTO public.source_categories (slug, name_es, name_en, is_active) VALUES
  ('documento_interno', 'Documento interno', 'Internal document', true)
ON CONFLICT (slug) DO UPDATE SET
  name_es = EXCLUDED.name_es,
  name_en = EXCLUDED.name_en,
  is_active = EXCLUDED.is_active;

-- Reclasificar fuentes documentales existentes (metadata.documentId o nombre doc:%)
UPDATE public.sources s
SET source_category_id = sc.id,
    updated_at = now()
FROM public.source_categories sc
WHERE sc.slug = 'documento_interno'
  AND (
    (COALESCE(s.metadata, '{}'::jsonb) ? 'documentId')
    OR s.name LIKE 'doc:%'
  )
  AND (s.source_category_id IS DISTINCT FROM sc.id);
