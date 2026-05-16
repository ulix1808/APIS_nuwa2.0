-- Categorías de fuentes; FK opcional desde public.sources.

CREATE TABLE IF NOT EXISTS public.source_categories (
  id bigserial PRIMARY KEY,
  slug text NOT NULL,
  name_es text NOT NULL,
  name_en text,
  is_active boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_source_categories_slug UNIQUE (slug)
);

COMMENT ON TABLE public.source_categories IS 'Taxonomía del catálogo de fuentes (slug estable para API).';

CREATE OR REPLACE FUNCTION public.set_source_categories_updated_at()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_source_categories_updated_at ON public.source_categories;
CREATE TRIGGER trg_source_categories_updated_at
  BEFORE UPDATE ON public.source_categories
  FOR EACH ROW EXECUTE FUNCTION public.set_source_categories_updated_at();

CREATE INDEX IF NOT EXISTS idx_source_categories_is_active ON public.source_categories (is_active);

ALTER TABLE public.sources
  ADD COLUMN IF NOT EXISTS source_category_id bigint REFERENCES public.source_categories(id);

COMMENT ON COLUMN public.sources.source_category_id IS 'FK a source_categories; POST /v1/sources exige categoría activa (contrato API).';

CREATE INDEX IF NOT EXISTS idx_sources_source_category_id ON public.sources (source_category_id);

ALTER TABLE public.source_categories ENABLE ROW LEVEL SECURITY;

INSERT INTO public.source_categories (slug, name_es, name_en, is_active) VALUES
  ('fiscal', 'Fiscal / Tributario', NULL, true),
  ('financial', 'Reguladores Financieros', NULL, true),
  ('aml', 'PLD / Antilavado', NULL, true),
  ('anticorruption', 'Anticorrupcion / Sector Publico', NULL, true),
  ('competition', 'Competencia / Consumidor', NULL, true),
  ('health', 'Salud / Seguridad Alimentaria', NULL, true),
  ('environment', 'Ambiental / Energia', NULL, true),
  ('labor', 'Laboral / Seguridad Social', NULL, true),
  ('data_protection', 'Proteccion de Datos', NULL, true),
  ('international', 'Internacional', NULL, true)
ON CONFLICT (slug) DO UPDATE SET
  name_es = EXCLUDED.name_es,
  name_en = EXCLUDED.name_en,
  is_active = EXCLUDED.is_active;
