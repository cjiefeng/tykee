-- 0003: ambient participation bookkeeping (§10.2).
-- state_day: household-TZ date the daily counters (unprompted_today, cooldown_multiplier) belong to;
-- a different day means they are treated as reset.
ALTER TABLE chat_state ADD COLUMN state_day TEXT;
-- The bot message sent for a 'respond' row, so 👎/👍 reactions can be attributed to it.
ALTER TABLE ambient_log ADD COLUMN reply_tg_message_id INTEGER;
CREATE INDEX ix_ambient_log_chat ON ambient_log(chat_id, created_at);
