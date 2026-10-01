-- 0001: initial schema (design §5.2, incl. §15.2 import columns and v1.8 amendments).
-- Applied by app/db/migrate.py inside one transaction; the runner records the version.

-- Users ---------------------------------------------------------------
CREATE TABLE users (
  id            INTEGER PRIMARY KEY,
  telegram_id   INTEGER NOT NULL UNIQUE,
  slug          TEXT NOT NULL UNIQUE,
  display_name  TEXT NOT NULL,
  timezone      TEXT NOT NULL DEFAULT 'UTC',
  enabled       INTEGER NOT NULL DEFAULT 1
);

-- Settings (hot-reloaded; dashboard writes, bot reads per request) -------
CREATE TABLE settings (
  key         TEXT PRIMARY KEY,
  value_json  TEXT NOT NULL,
  updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Decision categories & options ------------------------------------------
CREATE TABLE categories (
  id               INTEGER PRIMARY KEY,
  slug             TEXT NOT NULL UNIQUE,
  display_name     TEXT NOT NULL,
  description      TEXT NOT NULL,
  embedding        BLOB,
  recency_tau_days REAL NOT NULL DEFAULT 3.0,
  default_n        INTEGER NOT NULL DEFAULT 1,
  allow_generated  INTEGER NOT NULL DEFAULT 1,
  created_by       TEXT NOT NULL DEFAULT 'bot',
  merged_into      INTEGER REFERENCES categories(id),
  created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE category_aliases (
  alias        TEXT PRIMARY KEY,
  category_id  INTEGER NOT NULL REFERENCES categories(id)
);

CREATE TABLE options (
  id           INTEGER PRIMARY KEY,
  category_id  INTEGER NOT NULL REFERENCES categories(id),
  name         TEXT NOT NULL,
  tags_json    TEXT NOT NULL DEFAULT '[]',
  base_weight  REAL NOT NULL DEFAULT 1.0,
  owner        TEXT NOT NULL DEFAULT 'shared',
  active       INTEGER NOT NULL DEFAULT 1,
  created_by   TEXT NOT NULL DEFAULT 'admin',
  UNIQUE (category_id, name)
);

CREATE TABLE option_prefs (
  option_id   INTEGER NOT NULL REFERENCES options(id),
  user_id     INTEGER NOT NULL REFERENCES users(id),
  multiplier  REAL NOT NULL DEFAULT 1.0,
  PRIMARY KEY (option_id, user_id)
);

-- Decision log ------------------------------------------------------------
CREATE TABLE decisions (
  id            INTEGER PRIMARY KEY,
  category_id   INTEGER NOT NULL REFERENCES categories(id),
  option_id     INTEGER REFERENCES options(id),
  choice_text   TEXT NOT NULL,
  for_users     TEXT NOT NULL,
  asked_by      INTEGER NOT NULL REFERENCES users(id),
  status        TEXT NOT NULL,
  source        TEXT NOT NULL DEFAULT 'live',
  chat_id       INTEGER,
  tg_message_id INTEGER,
  created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX ix_decisions_cat_time ON decisions(category_id, created_at);

-- Conversation history ------------------------------------------------------
CREATE TABLE messages (
  id            INTEGER PRIMARY KEY,
  chat_id       INTEGER NOT NULL,
  tg_message_id INTEGER,
  user_id       INTEGER REFERENCES users(id),
  role          TEXT NOT NULL,
  kind          TEXT NOT NULL DEFAULT 'text',
  content       TEXT NOT NULL,
  created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX ix_messages_chat_time ON messages(chat_id, created_at);
CREATE UNIQUE INDEX ux_messages_tg ON messages(chat_id, tg_message_id)
  WHERE tg_message_id IS NOT NULL;

CREATE TABLE chat_summaries (
  chat_id     INTEGER PRIMARY KEY,
  summary     TEXT NOT NULL,
  upto_msg_id INTEGER NOT NULL
);

-- Second brain index (derived from /data/vault; safe to drop & rebuild) ------
CREATE TABLE notes (
  id          INTEGER PRIMARY KEY,
  path        TEXT NOT NULL UNIQUE,
  owner       TEXT NOT NULL,
  type        TEXT NOT NULL,
  title       TEXT NOT NULL,
  file_hash   TEXT NOT NULL,
  pinned      INTEGER NOT NULL DEFAULT 0,
  updated_at  TEXT NOT NULL
);

CREATE TABLE chunks (
  id          INTEGER PRIMARY KEY,
  note_id     INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
  ord         INTEGER NOT NULL,
  heading     TEXT,
  text        TEXT NOT NULL,
  chunk_hash  TEXT NOT NULL,
  embed_model TEXT NOT NULL
);

CREATE TABLE links (
  src_note_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
  dst_path    TEXT NOT NULL,
  PRIMARY KEY (src_note_id, dst_path)
);

CREATE VIRTUAL TABLE chunks_fts USING fts5(
  text, content='chunks', content_rowid='id', tokenize='porter unicode61'
);

CREATE VIRTUAL TABLE chunks_vec USING vec0(
  embedding float[384]
);

-- Memory inbox -----------------------------------------------------------------
CREATE TABLE memory_inbox (
  id          INTEGER PRIMARY KEY,
  owner       TEXT NOT NULL,
  target_path TEXT NOT NULL,
  content     TEXT NOT NULL,
  reason      TEXT,
  status      TEXT NOT NULL DEFAULT 'pending',
  created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Bootstrap import (§15) ---------------------------------------------------------
CREATE TABLE import_jobs (
  id              INTEGER PRIMARY KEY,
  source          TEXT NOT NULL,
  file_sha256     TEXT NOT NULL UNIQUE,
  since           TEXT NOT NULL,
  status          TEXT NOT NULL,  -- uploaded|configured|parsed|extracting|consolidating|review|done|failed|cancelled
  msg_count       INTEGER,
  window_count    INTEGER,
  cost_usd        REAL NOT NULL DEFAULT 0,
  chats_json      TEXT,
  sender_map_json TEXT,
  consent_at      TEXT,
  est_cost_usd    REAL,
  created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE import_windows (
  id            INTEGER PRIMARY KEY,
  job_id        INTEGER NOT NULL REFERENCES import_jobs(id),
  chat_ref      TEXT NOT NULL,
  start_ts      TEXT NOT NULL,
  end_ts        TEXT NOT NULL,
  text          TEXT,            -- nulled out after APPLY
  status        TEXT NOT NULL DEFAULT 'pending',
  batch_id      TEXT,
  result_json   TEXT
);

CREATE TABLE import_items (
  id            INTEGER PRIMARY KEY,
  job_id        INTEGER NOT NULL REFERENCES import_jobs(id),
  kind          TEXT NOT NULL,
  payload_json  TEXT NOT NULL,
  evidence_json TEXT NOT NULL,
  confidence    REAL NOT NULL,
  status        TEXT NOT NULL DEFAULT 'pending'
);

-- Usage & cost --------------------------------------------------------------------
CREATE TABLE usage (
  id                 INTEGER PRIMARY KEY,
  user_id            INTEGER REFERENCES users(id),
  purpose            TEXT NOT NULL,
  chat_id            INTEGER,
  import_job_id      INTEGER REFERENCES import_jobs(id),
  model              TEXT NOT NULL,
  input_tokens       INTEGER NOT NULL,
  output_tokens      INTEGER NOT NULL,
  cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
  cache_write_tokens INTEGER NOT NULL DEFAULT 0,
  cost_usd           REAL NOT NULL,
  created_at         TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX ix_usage_time ON usage(created_at);

-- Ambient participation (§10.2) ------------------------------------------------------
CREATE TABLE chat_state (
  chat_id              INTEGER PRIMARY KEY,
  muted_until          TEXT,
  last_unprompted_at   TEXT,
  unprompted_today     INTEGER NOT NULL DEFAULT 0,
  cooldown_multiplier  REAL NOT NULL DEFAULT 1.0
);

CREATE TABLE ambient_log (
  id             INTEGER PRIMARY KEY,
  chat_id        INTEGER NOT NULL,
  from_msg_id    INTEGER NOT NULL,
  to_msg_id      INTEGER NOT NULL,
  action         TEXT NOT NULL,
  rule           TEXT,
  reason         TEXT,
  confidence     REAL,
  feedback       TEXT,
  created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE schema_version (version INTEGER NOT NULL);
