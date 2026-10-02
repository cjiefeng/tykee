-- 0002: random_pick arguments stored per decision so 🎲 reroll can re-run the pick (§8.4).
ALTER TABLE decisions ADD COLUMN context_json TEXT;
CREATE INDEX ix_decisions_chat_time ON decisions(chat_id, created_at);
CREATE INDEX ix_decisions_tg_message ON decisions(chat_id, tg_message_id);
