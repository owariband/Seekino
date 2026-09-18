-- AniVAG controlled migration: durable ParseJob attempt and per-VideoChunk
-- operation timing. Absolute timestamps are Unix epoch milliseconds; elapsed
-- values are independently measured monotonic-clock durations.
--
-- Run 20260918_unix_time_ms.sql's audited timestamp migration first when
-- upgrading an existing installation.
-- Stop all parse workers before this migration.  A legacy RUNNING lease has no
-- attempt row, so it must finish or be explicitly reconciled before reapers
-- are restarted.  Historical attempts remain intentionally incomplete rather
-- than being backfilled with guessed durations.

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
            SET MESSAGE_TEXT = 'Stop/reconcile every legacy RUNNING ParseJob before adding timing tables';
    END IF;
END//
DELIMITER ;

CALL anivag_assert_parse_timing_migratable();
DROP PROCEDURE anivag_assert_parse_timing_migratable;

CREATE TABLE IF NOT EXISTS parse_job_attempt_timing (
    job_id VARCHAR(36) NOT NULL,
    attempt_count INTEGER NOT NULL,
    lease_version INTEGER NOT NULL,
    started_at BIGINT NOT NULL,
    completed_at BIGINT NULL,
    duration_ms BIGINT NULL,
    outcome VARCHAR(16) NOT NULL,
    timing_complete BOOL NOT NULL,
    PRIMARY KEY (job_id, attempt_count, lease_version),
    CONSTRAINT ck_parse_job_attempt_timing_attempt_count
        CHECK (attempt_count > 0),
    CONSTRAINT ck_parse_job_attempt_timing_lease_version
        CHECK (lease_version > 0),
    CONSTRAINT ck_parse_job_attempt_timing_timestamps
        CHECK (started_at >= 0 AND (completed_at IS NULL OR completed_at >= 0)),
    CONSTRAINT ck_parse_job_attempt_timing_duration
        CHECK (duration_ms IS NULL OR duration_ms >= 0),
    CONSTRAINT ck_parse_job_attempt_timing_outcome
        CHECK (outcome IN ('RUNNING', 'SUCCEEDED', 'RETRY', 'FAILED', 'LEASE_EXPIRED')),
    CONSTRAINT ck_parse_job_attempt_timing_terminal_state CHECK (
        (outcome = 'RUNNING' AND completed_at IS NULL
            AND duration_ms IS NULL AND timing_complete = 0)
        OR (outcome IN ('SUCCEEDED', 'RETRY', 'FAILED')
            AND completed_at IS NOT NULL AND duration_ms IS NOT NULL
            AND timing_complete = 1)
        OR (outcome = 'LEASE_EXPIRED' AND completed_at IS NOT NULL
            AND duration_ms IS NULL AND timing_complete = 0)
    ),
    CONSTRAINT fk_parse_job_attempt_timing_job
        FOREIGN KEY (job_id) REFERENCES series_parse_job (job_id)
        ON DELETE CASCADE,
    INDEX ix_parse_job_attempt_timing_job_started (job_id, started_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS video_chunk_stage_timing (
    job_id VARCHAR(36) NOT NULL,
    attempt_count INTEGER NOT NULL,
    lease_version INTEGER NOT NULL,
    sequence_no INTEGER NOT NULL,
    stage VARCHAR(32) NOT NULL,
    video_chunk_id VARCHAR(36) NOT NULL,
    started_at BIGINT NOT NULL,
    completed_at BIGINT NULL,
    duration_ms BIGINT NULL,
    status VARCHAR(16) NOT NULL,
    PRIMARY KEY (job_id, attempt_count, lease_version, sequence_no, stage),
    CONSTRAINT fk_video_chunk_stage_timing_attempt
        FOREIGN KEY (job_id, attempt_count, lease_version)
        REFERENCES parse_job_attempt_timing (job_id, attempt_count, lease_version)
        ON DELETE CASCADE,
    CONSTRAINT ck_video_chunk_stage_timing_attempt_count
        CHECK (attempt_count > 0),
    CONSTRAINT ck_video_chunk_stage_timing_lease_version
        CHECK (lease_version > 0),
    CONSTRAINT ck_video_chunk_stage_timing_sequence
        CHECK (sequence_no >= 0),
    CONSTRAINT ck_video_chunk_stage_timing_stage
        CHECK (stage IN ('SPLIT', 'ASR', 'PARSE', 'TEXT_EMBEDDING', 'VISION_EMBEDDING', 'INDEX')),
    CONSTRAINT ck_video_chunk_stage_timing_status
        CHECK (status IN ('RUNNING', 'SUCCEEDED', 'FAILED', 'LEASE_EXPIRED')),
    CONSTRAINT ck_video_chunk_stage_timing_timestamps
        CHECK (started_at >= 0 AND (completed_at IS NULL OR completed_at >= 0)),
    CONSTRAINT ck_video_chunk_stage_timing_duration
        CHECK (duration_ms IS NULL OR duration_ms >= 0),
    CONSTRAINT ck_video_chunk_stage_timing_terminal_state CHECK (
        (status = 'RUNNING' AND completed_at IS NULL AND duration_ms IS NULL)
        OR (status IN ('SUCCEEDED', 'FAILED')
            AND completed_at IS NOT NULL AND duration_ms IS NOT NULL)
        OR (status = 'LEASE_EXPIRED'
            AND completed_at IS NOT NULL AND duration_ms IS NULL)
    ),
    INDEX ix_video_chunk_stage_timing_job_sequence (job_id, sequence_no)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
