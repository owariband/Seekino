-- anivag controlled migration: Series-level ASR retrieval language hint.
-- Existing Series remain unset until a user confirms the stored transcript language.

ALTER TABLE series
    ADD COLUMN transcript_language_hint VARCHAR(64) NULL AFTER description;

-- Optional operator verification after values are configured. This query must
-- return no rows: application writes use normalized BCP-47 tags such as ja,
-- zh-Hans, en-US or ko.
SELECT series_id, name, transcript_language_hint
FROM series
WHERE transcript_language_hint IS NOT NULL
  AND transcript_language_hint NOT REGEXP
      '^([A-Za-z]{2,8}(-[A-Za-z0-9]{1,8})*|x(-[A-Za-z0-9]{1,8})+)$';
