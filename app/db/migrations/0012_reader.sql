-- 0012: read-only account reader (§10.7, M10). Reader rows live in `messages` next to the bot's,
-- separated by `source`: a DM's peer id (from Jack's account) equals the bot's chat_id with that
-- person, so every query on messages by chat_id also filters by source.
ALTER TABLE messages ADD COLUMN source TEXT NOT NULL DEFAULT 'bot';   -- 'bot' | 'account_reader'

-- The bot's own uniqueness rule only covers bot rows now; reader rows have their own.
DROP INDEX ux_messages_tg;
CREATE UNIQUE INDEX ux_messages_tg ON messages(chat_id, tg_message_id)
  WHERE tg_message_id IS NOT NULL AND source = 'bot';
CREATE UNIQUE INDEX ux_messages_reader ON messages(source, chat_id, tg_message_id)
  WHERE source = 'account_reader';
CREATE INDEX ix_messages_source_chat ON messages(source, chat_id, id);

CREATE TABLE reader_chats (
  id              INTEGER PRIMARY KEY,
  peer_id         INTEGER NOT NULL,            -- resolved, marked id (user > 0, group < 0)
  access_hash     INTEGER,                     -- users/supergroups: needed to address the peer
  kind            TEXT NOT NULL,               -- 'user' | 'group' | 'topic'
  thread_id       INTEGER,                     -- forum topic, when kind='topic'
  label           TEXT NOT NULL,
  enabled         INTEGER NOT NULL DEFAULT 1,
  consent_at      TEXT,                        -- NULL = not readable
  interval_min    INTEGER NOT NULL DEFAULT 30,
  retention_days  INTEGER NOT NULL DEFAULT 7,
  last_msg_id     INTEGER NOT NULL DEFAULT 0,  -- Telegram message id cursor (0 = not started)
  baseline_msg_id INTEGER,                     -- newest id when reading started; Backfill stops here
  harvest_msg_id  INTEGER NOT NULL DEFAULT 0,  -- messages.id harvest cursor
  last_fetch_at   TEXT,
  fetch_day       TEXT,                        -- household date of fetched_today
  fetched_today   INTEGER NOT NULL DEFAULT 0,
  last_harvest_at TEXT,
  last_harvest    TEXT,                        -- short result, e.g. 'done: 2 decisions, 1 fact'
  status          TEXT NOT NULL DEFAULT 'idle',-- 'idle'|'ok'|'flood_wait'|'revoked'|'not_found'|'error'
  error           TEXT,
  created_at      TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (peer_id, thread_id)
);

CREATE TABLE reader_audit (
  id          INTEGER PRIMARY KEY,
  action      TEXT NOT NULL,                   -- 'add'|'remove'|'enable'|'disable'|'consent'|'unconsent'|'login'|'logout'|...
  chat_label  TEXT,
  actor       TEXT NOT NULL,                   -- 'dashboard' | 'system'
  detail      TEXT,
  created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Harvest runs of reader chats point at their reader_chats row (NULL for group topics).
ALTER TABLE harvest_runs ADD COLUMN reader_chat_id INTEGER;
