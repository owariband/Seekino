-- AniVAG controlled migration: media timeline milliseconds -> integer seconds.
--
-- This is a stop-the-world migration for the seconds-only m2 pipeline. It
-- deliberately rebuilds derived parse data instead of teaching runtime code to
-- read two indistinguishable integer units.
--
-- Before running:
--   1. Stop API and Parse Consumer writes.
--   2. Back up MySQL, the Qdrant collection, and the artifact directory.
--   3. Confirm the old *_ms columns exist and the new *_seconds columns do not.
--   4. Record every jobs/<job_id> artifact directory; remove those derived job
--      directories after this SQL succeeds and before the consumer restarts.
--   5. Clear/rebuild the AniVAG Qdrant collection before the API restarts. Its
--      payload has IDs rather than timeline fields, but its embedded text was
--      generated from the old millisecond facts.
--
-- This migration preserves Series, Video, UploadSession, source media, and
-- conversation text. It invalidates derived VideoChunk/Context/Embedding data,
-- parse checkpoints/model-usage attempts, and media evidence embedded in Agent
-- messages. Absolute UnixTimeMs columns and processing_duration_ms are untouched.

DELIMITER $$

DROP PROCEDURE IF EXISTS assert_video_timeline_seconds_preconditions$$
CREATE PROCEDURE assert_video_timeline_seconds_preconditions()
BEGIN
    IF EXISTS (
        SELECT 1
        FROM series_parse_job
        WHERE status = 'RUNNING'
        LIMIT 1
    ) THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'stop Parse Consumers: RUNNING jobs still exist';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM series_parse_job
        GROUP BY video_id
        HAVING COUNT(*) > 1
        LIMIT 1
    ) THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'multiple jobs exist for one video; resolve them before m2 reset';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = DATABASE()
          AND table_name = 'video'
          AND column_name = 'duration_ms'
    ) OR EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = DATABASE()
          AND table_name = 'video'
          AND column_name = 'duration_seconds'
    ) THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'video timeline schema is not the expected pre-migration version';
    END IF;
END$$

CALL assert_video_timeline_seconds_preconditions()$$
DROP PROCEDURE assert_video_timeline_seconds_preconditions$$

DELIMITER ;

-- Derived facts contain media ranges in scalar columns, nested JSON, compact
-- text, vectors, and artifact payloads. Rebuild them under the m2 contract.
-- Usage rows must be removed before jobs because their foreign keys do not
-- cascade. VideoChunk dependents are removed through ON DELETE CASCADE.
START TRANSACTION;

DELETE FROM parse_model_call;
DELETE FROM parse_usage_attempt;
DELETE FROM video_chunk_checkpoint;
DELETE FROM video_chunk;

-- Preserve the conversation itself while removing stale media evidence and
-- playback context. Timing records are processing durations and remain valid.
UPDATE agent_message
SET search_result = NULL,
    extra_context = NULL
WHERE search_result IS NOT NULL
   OR extra_context IS NOT NULL;

-- Old ParseConfig snapshots contain *Ms fields. NULL means the reset job will
-- use the deployed v2 defaults; it is safer than guessing historical intent.
UPDATE upload_session
SET parse_config = NULL
WHERE parse_config IS NOT NULL;

UPDATE series_parse_job
SET pipeline_version = 'm2-seconds',
    parse_config = NULL,
    status = 'QUEUED',
    stage = 'QUEUED',
    progress = 0,
    attempt_count = 0,
    version = version + 1,
    available_at = CAST(ROUND(UNIX_TIMESTAMP(CURRENT_TIMESTAMP(3)) * 1000) AS SIGNED),
    lease_owner = NULL,
    lease_version = lease_version + 1,
    lease_expires_at = NULL,
    heartbeat_at = NULL,
    started_at = NULL,
    completed_at = NULL,
    processing_duration_ms = 0,
    processing_timing_complete = TRUE,
    last_error = NULL;

UPDATE video
SET parse_status = 'QUEUED',
    parse_progress = 0;

-- Duration rounds up so the final partial second remains addressable.
UPDATE video
SET duration_ms = CEIL(duration_ms / 1000);

COMMIT;

-- MySQL DDL commits implicitly. The service must remain stopped until every
-- rename and verification below has completed.
ALTER TABLE video
    DROP CHECK ck_video_duration;

ALTER TABLE video
    CHANGE COLUMN duration_ms duration_seconds BIGINT NOT NULL;

ALTER TABLE video
    ADD CONSTRAINT ck_video_duration CHECK (duration_seconds > 0);

ALTER TABLE video_chunk
    DROP CHECK ck_video_chunk_range,
    DROP CHECK ck_video_chunk_analysis_range;

ALTER TABLE video_chunk
    CHANGE COLUMN start_ms start_seconds BIGINT NOT NULL,
    CHANGE COLUMN end_ms end_seconds BIGINT NOT NULL,
    CHANGE COLUMN analysis_window_start_ms analysis_window_start_seconds BIGINT NOT NULL,
    CHANGE COLUMN analysis_window_end_ms analysis_window_end_seconds BIGINT NOT NULL;

ALTER TABLE video_chunk
    ADD CONSTRAINT ck_video_chunk_range
        CHECK (start_seconds >= 0 AND end_seconds > start_seconds),
    ADD CONSTRAINT ck_video_chunk_analysis_range
        CHECK (
            analysis_window_start_seconds <= start_seconds
            AND analysis_window_end_seconds >= end_seconds
        );

ALTER TABLE video_chunk_checkpoint
    DROP CHECK ck_video_chunk_checkpoint_range;

ALTER TABLE video_chunk_checkpoint
    CHANGE COLUMN chunk_start_ms chunk_start_seconds BIGINT NULL,
    CHANGE COLUMN chunk_end_ms chunk_end_seconds BIGINT NULL;

ALTER TABLE video_chunk_checkpoint
    ADD CONSTRAINT ck_video_chunk_checkpoint_range
        CHECK (
            (chunk_start_seconds IS NULL AND chunk_end_seconds IS NULL)
            OR (
                chunk_start_seconds IS NOT NULL
                AND chunk_end_seconds IS NOT NULL
                AND chunk_start_seconds >= 0
                AND chunk_end_seconds > chunk_start_seconds
            )
        );

-- Verification: every query below must return the expected empty/zero result
-- before deploying seconds-only code.
SELECT table_name, column_name
FROM information_schema.columns
WHERE table_schema = DATABASE()
  AND (
      (table_name = 'video' AND column_name = 'duration_ms')
      OR (
          table_name = 'video_chunk'
          AND column_name IN (
              'start_ms',
              'end_ms',
              'analysis_window_start_ms',
              'analysis_window_end_ms'
          )
      )
      OR (
          table_name = 'video_chunk_checkpoint'
          AND column_name IN ('chunk_start_ms', 'chunk_end_ms')
      )
  );

SELECT
    (SELECT COUNT(*) FROM video_chunk) AS video_chunk_count,
    (SELECT COUNT(*) FROM video_chunk_checkpoint) AS checkpoint_count,
    (SELECT COUNT(*) FROM parse_usage_attempt) AS usage_attempt_count,
    (SELECT COUNT(*) FROM parse_model_call) AS model_call_count;

SELECT video_id, duration_seconds
FROM video
WHERE duration_seconds <= 0;

SELECT job_id, video_id, pipeline_version, status, stage, progress
FROM series_parse_job
WHERE pipeline_version <> 'm2-seconds'
   OR status <> 'QUEUED'
   OR stage <> 'QUEUED'
   OR progress <> 0;

SELECT message_id
FROM agent_message
WHERE search_result IS NOT NULL
   OR extra_context IS NOT NULL;

-- After verification and before restart:
--   * delete the recorded old jobs/<job_id> artifact directories;
--   * clear/recreate the Qdrant collection;
--   * deploy the seconds-only code;
--   * start API/Consumer and let the reset m2-seconds jobs rebuild derived data.
