-- AniVAG controlled migration: absolute timestamps become signed BIGINT Unix
-- milliseconds. Media-relative *_ms columns and duration fields are unchanged.
--
-- STOP: this historical schema cannot be converted with one implicit timezone.
-- Application writes stored UTC-naive DATETIME values, while MySQL NOW()-based
-- defaults/on-update expressions stored Asia/Shanghai wall-clock values. Some
-- columns, especially updated_at, can therefore contain both provenances.
-- A blanket UNIX_TIMESTAMP/TIMESTAMPDIFF conversion would silently shift data.
--
-- Required operator procedure:
--   1. Take and verify a restorable physical backup.
--   2. Inventory every column listed by the query below.
--   3. Determine provenance for each row batch from deployment/write history.
--   4. Copy affected tables into an audit schema before changing any value.
--   5. For every batch, use the conversion template below with an explicit
--      source offset and a reviewed WHERE predicate.
--   6. Verify row counts, null counts, min/max instants, and sampled business
--      events against external logs before swapping columns.
--   7. If a row's provenance cannot be established, do not guess: rebuild the
--      affected development/test database or stop and perform data recovery.
--
-- server.database.create_schema deliberately rejects legacy DATETIME columns,
-- so application rollout remains fail-closed until this procedure is complete.

SELECT table_name, column_name, data_type, column_default, extra
FROM information_schema.columns
WHERE table_schema = DATABASE()
  AND (
      (table_name = 'series' AND column_name IN ('created_at', 'updated_at'))
      OR (table_name = 'video' AND column_name IN ('created_at', 'updated_at'))
      OR (table_name = 'video_chunk' AND column_name IN ('created_at', 'updated_at'))
      OR (table_name = 'video_chunk_info' AND column_name = 'parsed_at')
      OR (table_name = 'context_snapshot' AND column_name = 'created_at')
      OR (table_name = 'video_chunk_embedding' AND column_name IN ('created_at', 'updated_at'))
      OR (table_name = 'index_sync_intent' AND column_name IN ('available_at', 'synced_at', 'created_at', 'updated_at'))
      OR (table_name = 'series_parse_job' AND column_name IN ('available_at', 'lease_expires_at', 'heartbeat_at', 'started_at', 'completed_at', 'created_at', 'updated_at'))
      OR (table_name = 'video_chunk_checkpoint' AND column_name = 'updated_at')
      OR (table_name = 'upload_session' AND column_name IN ('completion_started_at', 'expires_at', 'completed_at', 'created_at', 'updated_at'))
      OR (table_name = 'upload_segment' AND column_name = 'created_at')
      OR (table_name = 'agent_conversation' AND column_name IN ('last_message_at', 'created_at', 'updated_at'))
      OR (table_name = 'agent_message' AND column_name IN ('created_at', 'updated_at'))
      OR (table_name = 'agent_message_timing' AND column_name = 'started_at')
      OR (table_name = 'parse_usage_attempt' AND column_name = 'started_at')
      OR (table_name = 'parse_model_call' AND column_name IN ('started_at', 'created_at', 'updated_at'))
  )
ORDER BY table_name, ordinal_position;

-- Intentionally abort an unreviewed execution. Remove this statement only in
-- an operator-owned copy containing the complete, reviewed per-batch plan.
SIGNAL SQLSTATE '45000'
    SET MESSAGE_TEXT = 'Unix-ms migration requires an audited per-batch timezone plan; do not convert DATETIME implicitly';

-- Per-column/per-batch conversion template (not executable as-is):
--
-- ALTER TABLE `<table>` ADD COLUMN `<column>_unix_ms` BIGINT NULL;
--
-- -- UTC-naive batch. The predicate must identify provenance, not infer it from
-- -- the timestamp value itself.
-- UPDATE `<table>`
-- SET `<column>_unix_ms` =
--     TIMESTAMPDIFF(
--         MICROSECOND,
--         '1970-01-01 00:00:00',
--         CONVERT_TZ(`<column>`, '+00:00', '+00:00')
--     ) DIV 1000
-- WHERE `<reviewed UTC provenance predicate>`;
--
-- -- Asia/Shanghai wall-clock batch (UTC+08:00, no daylight-saving changes in
-- -- the relevant deployment period).
-- UPDATE `<table>`
-- SET `<column>_unix_ms` =
--     TIMESTAMPDIFF(
--         MICROSECOND,
--         '1970-01-01 00:00:00',
--         CONVERT_TZ(`<column>`, '+08:00', '+00:00')
--     ) DIV 1000
-- WHERE `<reviewed MySQL NOW() provenance predicate>`;
--
-- -- Must return zero before swapping the column. Add the original column's
-- -- nullability to this check when NULL is valid.
-- SELECT COUNT(*) AS unconverted_rows
-- FROM `<table>`
-- WHERE `<column>` IS NOT NULL AND `<column>_unix_ms` IS NULL;
--
-- -- Only after all verification: drop dependent indexes, drop the DATETIME
-- -- column, rename the staged BIGINT column, and recreate indexes. Preserve the
-- -- exact original NULL/NOT NULL contract. Application defaults now come from
-- -- now_unix_time_ms; do not recreate CURRENT_TIMESTAMP/NOW() defaults.
-- ALTER TABLE `<table>`
--     DROP COLUMN `<column>`,
--     CHANGE COLUMN `<column>_unix_ms` `<column>` BIGINT <NULLABILITY>;

-- Final type verification. This query must return no rows after the reviewed
-- migration has converted every listed absolute-time column.
SELECT table_name, column_name, data_type
FROM information_schema.columns
WHERE table_schema = DATABASE()
  AND data_type IN ('datetime', 'timestamp')
  AND column_name IN (
      'created_at', 'updated_at', 'available_at', 'synced_at', 'parsed_at',
      'lease_expires_at', 'heartbeat_at', 'started_at', 'completed_at',
      'completion_started_at', 'expires_at', 'last_message_at'
  );
