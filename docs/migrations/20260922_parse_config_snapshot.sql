-- Persist the resolved configuration selected when an upload session starts.
-- Existing rows remain NULL because their historical parameters are unknown.
-- Apply before starting code that reads the new columns.

ALTER TABLE upload_session
    ADD COLUMN IF NOT EXISTS parse_config JSON NULL;

ALTER TABLE series_parse_job
    ADD COLUMN IF NOT EXISTS parse_config JSON NULL;
