-- 0006: the memory inbox also holds suggestions from the harvester (§10.4): a new category or a
-- new option seen in another topic. Approving one creates it; notes behave as before.
ALTER TABLE memory_inbox ADD COLUMN kind TEXT NOT NULL DEFAULT 'note';  -- 'note' | 'category' | 'option'
ALTER TABLE memory_inbox ADD COLUMN payload_json TEXT;                  -- kind-specific details
