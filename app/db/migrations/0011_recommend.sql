-- 0011: place recommendations (§10.6). No Google Places API: areas come from a public gazetteer
-- seed plus user-defined neighbourhood areas, attributes carry their provenance.
CREATE TABLE areas (
  id         INTEGER PRIMARY KEY,
  name       TEXT NOT NULL UNIQUE,            -- 'Tiong Bahru'
  kind       TEXT NOT NULL,                   -- 'planning_area'|'subzone'|'mrt'|'neighbourhood'|'user'
  lat        REAL NOT NULL,
  lng        REAL NOT NULL,
  radius_m   INTEGER NOT NULL,
  source     TEXT NOT NULL DEFAULT 'seed'     -- 'seed' | 'user'
);
CREATE TABLE area_aliases (
  alias      TEXT PRIMARY KEY,                -- normalised: 'tb', 'tiong bahru mrt', '中峇鲁'
  area_id    INTEGER NOT NULL REFERENCES areas(id) ON DELETE CASCADE
);

ALTER TABLE places ADD COLUMN status  TEXT NOT NULL DEFAULT 'visited';  -- 'visited' | 'unvisited'
ALTER TABLE places ADD COLUMN area_id INTEGER REFERENCES areas(id);
-- The category a web find was searched for (save_place_candidates), so it's a candidate there
-- before it has an option or a decision.
ALTER TABLE places ADD COLUMN category_hint INTEGER REFERENCES categories(id);

CREATE TABLE place_attributes (
  place_id   INTEGER NOT NULL REFERENCES places(id) ON DELETE CASCADE,
  key        TEXT NOT NULL,                   -- 'pet_friendly', 'kid_friendly', 'halal', …
  value      TEXT NOT NULL,                   -- pet_friendly: 'yes'|'outdoor_only'|'no'|'unknown'
  source     TEXT NOT NULL,                   -- 'user' | 'web'
  evidence   TEXT,                            -- quote, URL, or size limit note
  checked_at TEXT NOT NULL,
  PRIMARY KEY (place_id, key)
);
