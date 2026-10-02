-- 0005: forum topics (§10.4). Tykee reads every topic but answers only in the answer topic.
ALTER TABLE messages ADD COLUMN thread_id INTEGER;   -- NULL for DMs, non-forum groups and pre-M4 rows
CREATE INDEX ix_messages_chat_thread ON messages(chat_id, thread_id, id);

CREATE TABLE forum_topics (
  chat_id       INTEGER NOT NULL,
  thread_id     INTEGER NOT NULL,                     -- 1 = General
  name          TEXT,                                 -- NULL until seen; admin can label
  closed        INTEGER NOT NULL DEFAULT 0,
  last_seen_at  TEXT,
  PRIMARY KEY (chat_id, thread_id)
);

CREATE TABLE topic_harvest (
  chat_id         INTEGER NOT NULL,
  thread_id       INTEGER NOT NULL,                   -- 0 = pre-M4 rows without a topic
  last_msg_id     INTEGER NOT NULL,                   -- messages.id cursor
  last_run_at     TEXT,                               -- last LLM harvest
  last_tick_at    TEXT,                               -- last code-only check
  PRIMARY KEY (chat_id, thread_id)
);

-- One row per LLM harvest of a topic, for the dashboard's Ambient page.
CREATE TABLE harvest_runs (
  id            INTEGER PRIMARY KEY,
  chat_id       INTEGER NOT NULL,
  thread_id     INTEGER NOT NULL,
  from_msg_id   INTEGER NOT NULL,
  to_msg_id     INTEGER NOT NULL,
  messages      INTEGER NOT NULL,
  facts         INTEGER NOT NULL DEFAULT 0,          -- proposed to the inbox
  decisions     INTEGER NOT NULL DEFAULT 0,          -- recorded as source='observed'
  suggestions   INTEGER NOT NULL DEFAULT 0,          -- unknown categories / options → inbox
  skipped_out_of_scope INTEGER NOT NULL DEFAULT 0,
  status        TEXT NOT NULL,                       -- 'done' | 'error' | 'budget'
  created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
-- usage.purpose gains 'harvest'; decisions.source gains 'observed' (no schema change needed)
