-- 0010: Google Maps links → places (§10.5). Only named businesses become places; links that
-- resolve to coordinates, an address or a home are cached as 'unnamed' without their final URL.
CREATE TABLE places (
  id             INTEGER PRIMARY KEY,
  name           TEXT NOT NULL,
  name_norm      TEXT NOT NULL,               -- decisions.text.normalise(name), for dedupe
  google_id      TEXT UNIQUE,                 -- 'cid:<n>' (ftid/cid) or 'gpid:<id>' (venue)
  lat            REAL,
  lng            REAL,
  address        TEXT,                        -- when the link or venue carries one
  maps_url       TEXT NOT NULL,               -- canonical link to send back
  note_path      TEXT,                        -- shared/places/<slug>.md
  first_seen_at  TEXT NOT NULL,
  last_seen_at   TEXT NOT NULL,
  visit_count    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX ix_places_name_norm ON places(name_norm);

CREATE TABLE place_links (                     -- resolver cache
  url            TEXT PRIMARY KEY,             -- as pasted
  final_url      TEXT,                         -- NULL unless status = 'resolved'
  place_id       INTEGER REFERENCES places(id),
  status         TEXT NOT NULL,                -- 'resolved'|'unnamed'|'failed'|'blocked_host'
  error          TEXT,
  attempts       INTEGER NOT NULL DEFAULT 1,
  resolved_at    TEXT NOT NULL
);
CREATE INDEX ix_place_links_time ON place_links(resolved_at);

ALTER TABLE options   ADD COLUMN place_id INTEGER REFERENCES places(id);
ALTER TABLE decisions ADD COLUMN place_id INTEGER REFERENCES places(id);
-- decisions.source gains 'user' (recorded via record_decision or a shared link + "eating here")
