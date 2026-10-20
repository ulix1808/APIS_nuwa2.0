-- Early-warning lists ("alerta temprana"): our own copy of the OFAC sanctions lists, refreshed daily
-- by the watchlist-sync Lambda (SDN_ADVANCED.XML + CONS_ADVANCED.XML), with the delta of every refresh.
-- Independent of screening: nothing in the screening flow reads these tables.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- One row per list record (OFAC FixedRef). A record that leaves the list keeps its row with removed_at.
CREATE TABLE IF NOT EXISTS public.watchlist_entries (
  id             BIGSERIAL PRIMARY KEY,
  list_code      TEXT NOT NULL,                       -- 'ofac'
  ext_id         TEXT NOT NULL,                       -- OFAC FixedRef (= ent_num of the CSV)
  party_type     TEXT NOT NULL CHECK (party_type IN ('individual', 'organization', 'vessel', 'aircraft', 'other')),
  name           TEXT NOT NULL,
  name_norm      TEXT NOT NULL,                       -- accent-free lower case, for search
  aliases        JSONB NOT NULL DEFAULT '[]'::jsonb,  -- [{name, type: aka|fka|nka, lowQuality}]
  lists          TEXT[] NOT NULL DEFAULT '{}',        -- 'SDN List', 'Consolidated List', ...
  programs       TEXT[] NOT NULL DEFAULT '{}',        -- 'SDGT', 'ILLICIT-DRUGS-EO14059', ...
  designated_on  DATE,                                -- OFAC designation date
  birth_dates    TEXT[] NOT NULL DEFAULT '{}',
  nationalities  TEXT[] NOT NULL DEFAULT '{}',
  countries      TEXT[] NOT NULL DEFAULT '{}',
  details        JSONB NOT NULL DEFAULT '{}'::jsonb,  -- {ids, addresses, linked, features}
  content_hash   TEXT NOT NULL,
  first_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),  -- first sync that saw it
  last_seen_at   TIMESTAMPTZ NOT NULL DEFAULT now(),  -- last sync that saw it
  changed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),  -- added, changed, removed or restored
  removed_at     TIMESTAMPTZ,
  UNIQUE (list_code, ext_id)
);

CREATE INDEX IF NOT EXISTS idx_watchlist_entries_active
  ON public.watchlist_entries (list_code, party_type) WHERE removed_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_watchlist_entries_changed
  ON public.watchlist_entries (changed_at);
CREATE INDEX IF NOT EXISTS idx_watchlist_entries_name_trgm
  ON public.watchlist_entries USING gin (name_norm gin_trgm_ops);

-- Identity documents of each record, normalized (upper case letters and digits).
-- id_type: rfc, curp, cedula, passport, national_id, tax_id, folio_mercantil, ... (slug of the OFAC label otherwise)
CREATE TABLE IF NOT EXISTS public.watchlist_entry_ids (
  entry_id  BIGINT NOT NULL REFERENCES public.watchlist_entries(id) ON DELETE CASCADE,
  id_type   TEXT NOT NULL,
  id_value  TEXT NOT NULL,
  label     TEXT,
  country   TEXT,
  PRIMARY KEY (entry_id, id_type, id_value)
);

CREATE INDEX IF NOT EXISTS idx_watchlist_entry_ids_value
  ON public.watchlist_entry_ids (id_value);

-- Sync and match runs (admin panel: last refresh, counts, errors).
CREATE TABLE IF NOT EXISTS public.watchlist_runs (
  id            BIGSERIAL PRIMARY KEY,
  kind          TEXT NOT NULL CHECK (kind IN ('sync', 'match')),
  list_code     TEXT,
  status        TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'ok', 'unchanged', 'failed')),
  trigger       TEXT NOT NULL DEFAULT 'schedule',     -- schedule | manual
  published_on  DATE,                                 -- DateOfIssue of the files
  files         JSONB NOT NULL DEFAULT '{}'::jsonb,   -- {name: {lastModified, sha256, bytes}}
  counts        JSONB NOT NULL DEFAULT '{}'::jsonb,   -- {total, added, changed, removed, restored, ...}
  error         TEXT,
  started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at   TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_watchlist_runs_kind
  ON public.watchlist_runs (kind, list_code, started_at DESC);

-- Delta of every sync: what entered, changed, left or came back, with the previous version.
CREATE TABLE IF NOT EXISTS public.watchlist_entry_changes (
  id        BIGSERIAL PRIMARY KEY,
  entry_id  BIGINT NOT NULL REFERENCES public.watchlist_entries(id) ON DELETE CASCADE,
  run_id    BIGINT NOT NULL REFERENCES public.watchlist_runs(id) ON DELETE CASCADE,
  change    TEXT NOT NULL CHECK (change IN ('added', 'changed', 'removed', 'restored')),
  previous  JSONB,                                    -- record before the change (changed / removed)
  at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_watchlist_entry_changes_run
  ON public.watchlist_entry_changes (run_id);
CREATE INDEX IF NOT EXISTS idx_watchlist_entry_changes_at
  ON public.watchlist_entry_changes (at);

COMMENT ON TABLE public.watchlist_entries IS 'Listas de alerta temprana (OFAC SDN + Consolidated), copia propia actualizada a diario';
COMMENT ON TABLE public.watchlist_entry_changes IS 'Delta de cada sincronización: altas, cambios, bajas y reingresos';
