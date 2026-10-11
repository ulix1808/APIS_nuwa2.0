-- Relations between a screened subject and the entities its report links it to (media
-- co-mentions, list/catalog relations). One row per (report, related entity). Rows are
-- computed by the BFF (same rules as the "Entidades Relacionadas" table: confidence floor,
-- user dismissals, homonyms, aliases) and replaced on every save/update of the report.
--
-- *_key / *_alias_keys hold the BFF normalized keys (accent-free, lowercase, corporate
-- suffixes and spaces removed) so lookups by name or alias are exact index hits.

CREATE TABLE IF NOT EXISTS public.entity_relations (
  id                   BIGSERIAL PRIMARY KEY,
  client_id            INTEGER NOT NULL,
  report_folio         TEXT NOT NULL,
  status               TEXT NOT NULL DEFAULT 'active'
                       CHECK (status IN ('active', 'deleted')),

  subject_entity_id    TEXT,
  subject_name         TEXT NOT NULL,
  subject_key          TEXT NOT NULL,
  subject_aliases      JSONB NOT NULL DEFAULT '[]'::jsonb,
  subject_alias_keys   TEXT[] NOT NULL DEFAULT '{}',
  subject_party_type   TEXT,

  related_entity_id    TEXT,
  related_name         TEXT NOT NULL,
  related_key          TEXT NOT NULL,
  related_aliases      JSONB NOT NULL DEFAULT '[]'::jsonb,
  related_alias_keys   TEXT[] NOT NULL DEFAULT '{}',
  related_party_type   TEXT,

  relation_type        TEXT,
  relationship         TEXT,
  risk_level           TEXT,
  confidence           INTEGER,
  source_type          TEXT,
  source_id            TEXT,
  source_url           TEXT,
  source_title         TEXT,
  source_excerpt       TEXT,

  report_created_at    TIMESTAMPTZ,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_entity_relations_folio
  ON public.entity_relations (client_id, report_folio);

CREATE INDEX IF NOT EXISTS idx_entity_relations_subject_key
  ON public.entity_relations (client_id, subject_key) WHERE status = 'active';

CREATE INDEX IF NOT EXISTS idx_entity_relations_related_key
  ON public.entity_relations (client_id, related_key) WHERE status = 'active';

CREATE INDEX IF NOT EXISTS idx_entity_relations_subject_entity
  ON public.entity_relations (client_id, subject_entity_id) WHERE status = 'active' AND subject_entity_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_entity_relations_related_entity
  ON public.entity_relations (client_id, related_entity_id) WHERE status = 'active' AND related_entity_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_entity_relations_subject_alias_keys
  ON public.entity_relations USING GIN (subject_alias_keys);

CREATE INDEX IF NOT EXISTS idx_entity_relations_related_alias_keys
  ON public.entity_relations USING GIN (related_alias_keys);
