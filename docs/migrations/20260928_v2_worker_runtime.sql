-- AniVAG controlled migration: Video Parse V2 Worker runtime accounting.
-- Apply after 20260927_video_parse_v2_tasks.sql and
-- 20260928_task_indexing_contract.sql with all application writers stopped.
-- MySQL DDL implicitly commits; inspect the schema after any error and do not
-- retry this file with --force.
SET time_zone = '+00:00';

DELIMITER $$
DROP PROCEDURE IF EXISTS assert_v2_worker_runtime_prerequisites$$
CREATE PROCEDURE assert_v2_worker_runtime_prerequisites()
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = DATABASE() AND table_name = 'video_parse_task'
    ) OR NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = DATABASE() AND table_name = 'parse_model_call'
          AND column_name = 'video_parse_task_id'
    ) THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'V2 Worker runtime requires the V2 task and usage-link migration';
    END IF;

    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = DATABASE() AND table_name = 'video_parse_usage_attempt'
    ) THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'video_parse_usage_attempt already exists; inspect migration state';
    END IF;

    IF EXISTS (
        SELECT 1 FROM parse_model_call
        WHERE job_id IS NULL AND video_parse_task_id IS NULL
        LIMIT 1
    ) OR EXISTS (
        SELECT 1 FROM parse_model_call
        WHERE job_id IS NOT NULL AND video_parse_task_id IS NOT NULL
        LIMIT 1
    ) THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'parse_model_call contains ambiguous task ownership';
    END IF;
END$$
CALL assert_v2_worker_runtime_prerequisites()$$
DROP PROCEDURE assert_v2_worker_runtime_prerequisites$$
DELIMITER ;

CREATE TABLE video_parse_usage_attempt (
    attempt_id VARCHAR(36) NOT NULL,
    video_parse_task_id VARCHAR(36) NOT NULL,
    execution_kind VARCHAR(16) NOT NULL,
    execution_task_id VARCHAR(36) NOT NULL,
    attempt_count INT NOT NULL,
    lease_version INT NOT NULL,
    started_at TIMESTAMP(3) NOT NULL,
    recording_complete BOOL NOT NULL DEFAULT FALSE,
    PRIMARY KEY (attempt_id),
    CONSTRAINT fk_video_parse_usage_attempt_task
        FOREIGN KEY (video_parse_task_id)
        REFERENCES video_parse_task(id) ON DELETE CASCADE,
    CONSTRAINT uq_video_parse_usage_attempt_lease
        UNIQUE (
            video_parse_task_id,
            execution_kind,
            execution_task_id,
            attempt_count,
            lease_version
        ),
    CONSTRAINT ck_video_parse_usage_attempt_execution_kind CHECK (
        execution_kind IN ('video', 'chunk', 'interval', 'prefix')
    ),
    INDEX ix_video_parse_usage_attempt_task (video_parse_task_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

ALTER TABLE parse_model_call
    DROP FOREIGN KEY fk_parse_model_call_video_parse_task,
    MODIFY COLUMN job_id VARCHAR(36) NULL,
    ADD COLUMN execution_kind VARCHAR(16) NULL AFTER video_parse_task_id,
    ADD COLUMN execution_task_id VARCHAR(36) NULL AFTER execution_kind,
    ADD CONSTRAINT fk_parse_model_call_video_parse_task
        FOREIGN KEY (video_parse_task_id)
        REFERENCES video_parse_task(id) ON DELETE CASCADE,
    ADD CONSTRAINT ck_parse_model_call_exactly_one_task CHECK (
        (job_id IS NOT NULL) <> (video_parse_task_id IS NOT NULL)
    ),
    ADD CONSTRAINT ck_parse_model_call_execution_identity CHECK (
        (video_parse_task_id IS NULL
            AND execution_kind IS NULL
            AND execution_task_id IS NULL)
        OR
        (video_parse_task_id IS NOT NULL
            AND execution_kind IS NOT NULL
            AND execution_task_id IS NOT NULL)
    ),
    ADD CONSTRAINT ck_parse_model_call_execution_kind CHECK (
        execution_kind IS NULL
        OR execution_kind IN ('video', 'chunk', 'interval', 'prefix')
    ),
    ADD INDEX ix_parse_model_call_v2_execution (
        video_parse_task_id,
        execution_kind,
        execution_task_id,
        started_at
    );

-- Queue admission, model-call slots, scheduler dispatch timestamps and Prefix
-- leases/cursors are stored below video_parse_task.detail._worker_runtime_v2.
-- The application replaces that JSON namespace under a locked parent row; no
-- extra DDL is required for those durable runtime values.

SELECT
    (SELECT COUNT(*) FROM video_parse_usage_attempt) AS v2_usage_attempt_count,
    (SELECT COUNT(*) FROM parse_model_call
     WHERE (job_id IS NOT NULL) = (video_parse_task_id IS NOT NULL))
        AS ambiguous_model_call_count;
