-- anivag controlled migration: keep ParseJob processing time on the job row.
--
-- Stop parse workers first. This migration folds the now-removed attempt
-- timing history into series_parse_job, then drops the two detail tables.
-- Run 20260918_unix_time_ms.sql first when upgrading an existing installation.

DROP PROCEDURE IF EXISTS anivag_assert_parse_timing_migratable;
DELIMITER //
CREATE PROCEDURE anivag_assert_parse_timing_migratable()
BEGIN
    IF EXISTS (
        SELECT 1
        FROM series_parse_job
        WHERE status = 'RUNNING'
        LIMIT 1
    ) THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'Stop/reconcile every RUNNING ParseJob before folding timing data';
    END IF;
END//
DELIMITER ;

CALL anivag_assert_parse_timing_migratable();
DROP PROCEDURE anivag_assert_parse_timing_migratable;

ALTER TABLE series_parse_job
    ADD COLUMN IF NOT EXISTS processing_duration_ms BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS processing_timing_complete BOOL NOT NULL DEFAULT TRUE,
    ADD CONSTRAINT ck_series_parse_job_processing_duration
        CHECK (processing_duration_ms >= 0);

DROP PROCEDURE IF EXISTS anivag_fold_parse_job_timing;
DELIMITER //
CREATE PROCEDURE anivag_fold_parse_job_timing()
BEGIN
    -- A Job that ran before timing existed has no trustworthy duration.
    UPDATE series_parse_job
    SET
        processing_duration_ms = 0,
        processing_timing_complete = CASE
            WHEN attempt_count = 0 THEN TRUE
            ELSE FALSE
        END;

    IF EXISTS (
        SELECT 1
        FROM information_schema.tables
        WHERE table_schema = DATABASE()
          AND table_name = 'parse_job_attempt_timing'
    ) THEN
        UPDATE series_parse_job AS job
        LEFT JOIN (
            SELECT
                job_id,
                COUNT(*) AS recorded_attempts,
                COUNT(DISTINCT attempt_count) AS distinct_attempts,
                MIN(attempt_count) AS first_attempt,
                MAX(attempt_count) AS last_attempt,
                SUM(COALESCE(duration_ms, 0)) AS total_duration_ms,
                SUM(CASE WHEN timing_complete THEN 0 ELSE 1 END) AS incomplete_attempts
            FROM parse_job_attempt_timing
            GROUP BY job_id
        ) AS attempts ON attempts.job_id = job.job_id
        SET
            job.processing_duration_ms = COALESCE(attempts.total_duration_ms, 0),
            job.processing_timing_complete = CASE
                WHEN job.attempt_count = 0 THEN TRUE
                WHEN attempts.recorded_attempts = job.attempt_count
                    AND attempts.distinct_attempts = job.attempt_count
                    AND attempts.first_attempt = 1
                    AND attempts.last_attempt = job.attempt_count
                    AND attempts.incomplete_attempts = 0 THEN TRUE
                ELSE FALSE
            END;

    END IF;

    DROP TABLE IF EXISTS video_chunk_stage_timing;
    DROP TABLE IF EXISTS parse_job_attempt_timing;
END//
DELIMITER ;

CALL anivag_fold_parse_job_timing();
DROP PROCEDURE anivag_fold_parse_job_timing;
