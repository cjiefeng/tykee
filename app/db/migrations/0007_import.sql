-- 0007: bootstrap import bookkeeping (§15). Jobs remember what the export contains and how they
-- were configured; windows remember which Telegram message ids they cover (dedupe across
-- overlapping exports); items point at the category proposal they belong to (review merges).
ALTER TABLE import_jobs ADD COLUMN filename TEXT;
ALTER TABLE import_jobs ADD COLUMN format TEXT;                -- 'single' | 'account'
ALTER TABLE import_jobs ADD COLUMN meta_json TEXT;             -- validation summary: chats, senders
ALTER TABLE import_jobs ADD COLUMN until TEXT;                 -- date range end (since = start)
ALTER TABLE import_jobs ADD COLUMN skipped_out_of_scope INTEGER NOT NULL DEFAULT 0;
ALTER TABLE import_jobs ADD COLUMN consolidation_json TEXT;    -- cached step results, so a retry doesn't pay twice
ALTER TABLE import_jobs ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE import_jobs ADD COLUMN error TEXT;
ALTER TABLE import_jobs ADD COLUMN polled_at TEXT;
ALTER TABLE import_jobs ADD COLUMN summary_json TEXT;          -- what APPLY did

ALTER TABLE import_windows ADD COLUMN ord INTEGER NOT NULL DEFAULT 0;
ALTER TABLE import_windows ADD COLUMN first_msg_id INTEGER;
ALTER TABLE import_windows ADD COLUMN last_msg_id INTEGER;
ALTER TABLE import_windows ADD COLUMN msg_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE import_windows ADD COLUMN est_tokens INTEGER NOT NULL DEFAULT 0;
ALTER TABLE import_windows ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE import_windows ADD COLUMN skipped_out_of_scope INTEGER NOT NULL DEFAULT 0;
CREATE INDEX ix_import_windows_job ON import_windows(job_id, status);
CREATE INDEX ix_import_windows_chat ON import_windows(chat_ref, first_msg_id);

ALTER TABLE import_items ADD COLUMN category_item_id INTEGER REFERENCES import_items(id);
ALTER TABLE import_items ADD COLUMN ref TEXT;                  -- episode id / proposal key
CREATE INDEX ix_import_items_job ON import_items(job_id, kind, status);
-- import_jobs.status: uploaded|configured|extracting|consolidating|review|applying|done|failed|cancelled
-- import_windows.status: pending|submitted|done|failed
-- import_items.kind: category|option|note|decision|unmapped; status: pending|approved|rejected|merged
