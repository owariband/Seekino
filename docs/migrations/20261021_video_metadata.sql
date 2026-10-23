-- anivag controlled migration: optional per-video viewing metadata.
-- Existing videos retain the episode-number title and keyframe cover fallback.

ALTER TABLE video
    ADD COLUMN display_name VARCHAR(255) NULL AFTER upload_sequence,
    ADD COLUMN description LONGTEXT NULL AFTER display_name,
    ADD COLUMN cover_uri LONGTEXT NULL AFTER description;
