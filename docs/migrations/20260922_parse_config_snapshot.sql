-- Persist the resolved configuration selected when an upload session starts.
-- Existing rows remain NULL because their historical parameters are unknown.
-- Apply once, before starting code that reads the new columns.
-- Check that both columns are absent first; MySQL 8.4 does not support
-- ADD COLUMN IF NOT EXISTS.

ALTER TABLE upload_session
    ADD COLUMN parse_config JSON NULL;

ALTER TABLE series_parse_job
    ADD COLUMN parse_config JSON NULL;
