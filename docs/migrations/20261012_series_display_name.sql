-- seekino controlled migration: viewer-facing Series title.
-- series.name stays the stable import key that series.id derives from; the
-- display name is optional and falls back to name in the UI when NULL.

ALTER TABLE series
    ADD COLUMN display_name VARCHAR(255) NULL AFTER name;
