-- 0008: scheduled nudges (§10.3). The schedule itself lives in settings (`nudges.items`); this
-- table records each nudge's outcome per household day, so a nudge fires at most once a day and
-- the dashboard can show why one was skipped.
CREATE TABLE nudge_runs (
  id           INTEGER PRIMARY KEY,
  nudge_id     TEXT NOT NULL,
  day          TEXT NOT NULL,             -- household-TZ date (YYYY-MM-DD)
  status       TEXT NOT NULL,             -- 'sent' | 'skipped' | 'failed'
  reason       TEXT,
  decision_id  INTEGER REFERENCES decisions(id),
  created_at   TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (nudge_id, day)
);
