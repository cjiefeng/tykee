-- 0004: provenance and decision bookkeeping for the memory inbox (§6.7).
ALTER TABLE memory_inbox ADD COLUMN source TEXT;        -- e.g. telegram:-100123, web:<url> (M7), topic:<name> (M4)
ALTER TABLE memory_inbox ADD COLUMN decided_at TEXT;
ALTER TABLE memory_inbox ADD COLUMN decided_by INTEGER REFERENCES users(id);
CREATE INDEX ix_memory_inbox_status ON memory_inbox(status, created_at);
