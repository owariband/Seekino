-- AniVAG controlled migration: integer media seconds -> integer milliseconds.
-- Existing column names and BIGINT types are retained. No records are deleted.
--
-- Scope: databases with no committed parse content, checkpoints, embeddings,
-- or saved Agent media evidence. Nonempty derived content requires a separate
-- contract-aware JSON/artifact conversion or reparse; this script stops before
-- changing data in that case. It must not be used to discard existing results.
--
-- Before execution (this file is prepared, not automatically executed):
--   * Stop API/Consumer writes and back up MySQL, indices and media artifacts.
--   * Select the intended database explicitly and inspect the preflight errors.
--   * Use a client that stops on SQL errors; do not pass mysql --force.
--   * Prepare a separate ES index with _meta.media_time_unit='milliseconds'.
--     Keep the old index and artifacts until the new results are verified.
--
-- Configuration chunk/interval/overlap durations remain seconds. Absolute
-- timestamps, lease times and processing durations are already separate units
-- and are untouched. Multiplying old 9 seconds yields 9000 milliseconds; it
-- cannot recover an original duration such as 8008. Reprobe original media for
-- exact duration before preparing new Chunk topology for that video.
-- Old Parse/Checkpoint JSON must be converted or regenerated before use with
-- import, evaluation or training export tools. In particular, the standalone
-- training exporter does not pass through the Server startup unit check.
--
-- MySQL DDL implicitly commits. A pending column COMMENT prevents runtime from
-- loading an interrupted migration and prevents a second multiply on rerun.
-- If interrupted after the pending marker, inspect/restore the backup rather
-- than clearing that marker and rerunning this script.

DELIMITER $$

DROP PROCEDURE IF EXISTS migrate_video_timeline_milliseconds$$
CREATE PROCEDURE migrate_video_timeline_milliseconds()
BEGIN
    DECLARE unit_comment TEXT;
    DECLARE EXIT HANDLER FOR SQLEXCEPTION
    BEGIN
        ROLLBACK;
        RESIGNAL;
    END;

    SELECT column_comment INTO unit_comment
    FROM information_schema.columns
    WHERE table_schema = DATABASE()
      AND table_name = 'video'
      AND column_name = 'duration_seconds'
      AND data_type = 'bigint';

    IF unit_comment IS NULL OR unit_comment <> '' THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'expected unmarked seconds schema; already migrated or pending schema must not be multiplied';
    END IF;

    IF EXISTS (SELECT 1 FROM series_parse_job WHERE lease_owner IS NOT NULL)
       OR EXISTS (SELECT 1 FROM video_parse_task WHERE lease_owner IS NOT NULL)
       OR EXISTS (SELECT 1 FROM chunk_parse_task WHERE lease_owner IS NOT NULL)
       OR EXISTS (SELECT 1 FROM interval_parse_task WHERE lease_owner IS NOT NULL)
    THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'stop writers and release parse leases before media unit migration';
    END IF;

    IF EXISTS (SELECT 1 FROM video_chunk_checkpoint)
       OR EXISTS (SELECT 1 FROM video_chunk_info)
       OR EXISTS (SELECT 1 FROM context_snapshot)
       OR EXISTS (SELECT 1 FROM video_chunk_embedding)
       OR EXISTS (SELECT 1 FROM chunk_parse_content)
       OR EXISTS (SELECT 1 FROM interval_parse_content)
       OR EXISTS (SELECT 1 FROM index_sync_intent)
       OR EXISTS (
           SELECT 1 FROM video_chunk
           WHERE JSON_LENGTH(transcript) > 0 OR JSON_LENGTH(keyframe_uris) > 0
       )
       OR EXISTS (
           SELECT 1 FROM agent_message
           WHERE (search_result IS NOT NULL AND JSON_TYPE(search_result) <> 'NULL')
              OR (extra_context IS NOT NULL AND JSON_TYPE(extra_context) <> 'NULL')
       )
    THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'preserve existing parse/evidence results: contract-aware JSON and artifact migration is required';
    END IF;

    IF EXISTS (
        SELECT 1 FROM video WHERE duration_seconds > 9223372036854775
    ) OR EXISTS (
        SELECT 1 FROM video_chunk
        WHERE start_seconds > 9223372036854775
           OR end_seconds > 9223372036854775
           OR analysis_window_start_seconds > 9223372036854775
           OR analysis_window_end_seconds > 9223372036854775
    ) THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'media time would exceed signed BIGINT after multiplication';
    END IF;

    -- This marker is intentionally rejected by runtime and by this procedure.
    ALTER TABLE video MODIFY COLUMN duration_seconds BIGINT NOT NULL
        COMMENT 'media_time_milliseconds_migration_pending';

    START TRANSACTION;

    UPDATE video SET duration_seconds = duration_seconds * 1000;

    UPDATE video_chunk SET
        start_seconds = start_seconds * 1000,
        end_seconds = end_seconds * 1000,
        analysis_window_start_seconds = analysis_window_start_seconds * 1000,
        analysis_window_end_seconds = analysis_window_end_seconds * 1000;

    -- Empty by preflight, retained here to document the complete scalar contract.
    UPDATE video_chunk_checkpoint SET
        chunk_start_seconds = chunk_start_seconds * 1000,
        chunk_end_seconds = chunk_end_seconds * 1000;

    COMMIT;

    -- Publish the unit only after every applicable media value is converted.
    ALTER TABLE video MODIFY COLUMN duration_seconds BIGINT NOT NULL
        COMMENT 'media_time_milliseconds';
END$$

CALL migrate_video_timeline_milliseconds()$$
DROP PROCEDURE migrate_video_timeline_milliseconds$$

DELIMITER ;

-- Verify the marker and scalar boundaries before deploying/restarting services.
SELECT column_name, data_type, column_comment
FROM information_schema.columns
WHERE table_schema = DATABASE()
  AND table_name = 'video'
  AND column_name = 'duration_seconds';

SELECT video_id, duration_seconds FROM video WHERE duration_seconds <= 0;

SELECT video_chunk_id, start_seconds, end_seconds
FROM video_chunk
WHERE start_seconds < 0 OR end_seconds <= start_seconds;

-- The API field names are unchanged, but their values now use milliseconds.
-- Switch all backend/frontend consumers together. Old browser v2 progress is
-- converted by the frontend to v3 milliseconds; old atMs links remain precise.
