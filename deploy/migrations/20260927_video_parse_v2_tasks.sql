-- Seekino controlled migration: Video Parse V2 task/result tables and UTC time.
-- Historical 2026-09-27 schema. For the current task/Interval indexing contract,
-- follow with 20260928_task_indexing_contract.sql while the V2 tables are empty.
-- Fresh databases should use the current application's init-db instead.
--
-- This migration is intentionally additive for V1 parse data. It does not
-- convert SeriesParseJob/checkpoint rows into V2 results, delete old results,
-- or enqueue work. Stop API/upload/parse writers and take a verified backup
-- before running an operator-owned copy of this script.
--
-- Application code treats MySQL TIMESTAMP values as UTC. Pin this migration
-- session too, so Unix-millisecond conversion is independent of host timezone.
SET time_zone = '+00:00';

DELIMITER $$

DROP PROCEDURE IF EXISTS assert_video_parse_v2_timestamp_source_schema$$
CREATE PROCEDURE assert_video_parse_v2_timestamp_source_schema()
BEGIN
    DECLARE source_column_count INT DEFAULT 0;
    DECLARE invalid_source_column_count INT DEFAULT 0;

    SELECT
        COUNT(*),
        COALESCE(SUM(
            CASE
                WHEN data_type = 'bigint'
                     AND column_type NOT LIKE '%unsigned%' THEN 0
                ELSE 1
            END
        ), 0)
    INTO source_column_count, invalid_source_column_count
    FROM information_schema.columns
    WHERE table_schema = DATABASE()
      AND (
          (table_name = 'video' AND column_name IN ('created_at', 'updated_at'))
          OR (table_name = 'video_chunk' AND column_name IN ('created_at', 'updated_at'))
          OR (table_name = 'upload_session' AND column_name IN ('completion_started_at', 'expires_at', 'completed_at', 'created_at', 'updated_at'))
          OR (table_name = 'upload_segment' AND column_name = 'created_at')
          OR (table_name = 'parse_usage_attempt' AND column_name = 'started_at')
          OR (table_name = 'parse_model_call' AND column_name IN ('started_at', 'created_at', 'updated_at'))
      );

    IF source_column_count <> 14 OR invalid_source_column_count <> 0 THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'Video Parse V2 timestamp migration requires all 14 source columns as signed BIGINT';
    END IF;
END$$

CALL assert_video_parse_v2_timestamp_source_schema()$$
DROP PROCEDURE assert_video_parse_v2_timestamp_source_schema$$

DELIMITER ;

DELIMITER $$

DROP PROCEDURE IF EXISTS assert_video_parse_v2_timestamp_range$$
CREATE PROCEDURE assert_video_parse_v2_timestamp_range()
BEGIN
    DECLARE minimum_timestamp_ms BIGINT DEFAULT 1000;
    DECLARE maximum_timestamp_ms BIGINT DEFAULT 2147483647999;

    -- MySQL TIMESTAMP supports this Unix-millisecond range. NULL remains NULL.
    IF EXISTS (
        SELECT 1 FROM video
        WHERE created_at NOT BETWEEN minimum_timestamp_ms AND maximum_timestamp_ms
           OR updated_at NOT BETWEEN minimum_timestamp_ms AND maximum_timestamp_ms
        LIMIT 1
    ) OR EXISTS (
        SELECT 1 FROM video_chunk
        WHERE created_at NOT BETWEEN minimum_timestamp_ms AND maximum_timestamp_ms
           OR updated_at NOT BETWEEN minimum_timestamp_ms AND maximum_timestamp_ms
        LIMIT 1
    ) OR EXISTS (
        SELECT 1 FROM upload_session
        WHERE (completion_started_at IS NOT NULL AND completion_started_at NOT BETWEEN minimum_timestamp_ms AND maximum_timestamp_ms)
           OR expires_at NOT BETWEEN minimum_timestamp_ms AND maximum_timestamp_ms
           OR (completed_at IS NOT NULL AND completed_at NOT BETWEEN minimum_timestamp_ms AND maximum_timestamp_ms)
           OR created_at NOT BETWEEN minimum_timestamp_ms AND maximum_timestamp_ms
           OR updated_at NOT BETWEEN minimum_timestamp_ms AND maximum_timestamp_ms
        LIMIT 1
    ) OR EXISTS (
        SELECT 1 FROM upload_segment
        WHERE created_at NOT BETWEEN minimum_timestamp_ms AND maximum_timestamp_ms
        LIMIT 1
    ) OR EXISTS (
        SELECT 1 FROM parse_usage_attempt
        WHERE started_at NOT BETWEEN minimum_timestamp_ms AND maximum_timestamp_ms
        LIMIT 1
    ) OR EXISTS (
        SELECT 1 FROM parse_model_call
        WHERE started_at NOT BETWEEN minimum_timestamp_ms AND maximum_timestamp_ms
           OR created_at NOT BETWEEN minimum_timestamp_ms AND maximum_timestamp_ms
           OR updated_at NOT BETWEEN minimum_timestamp_ms AND maximum_timestamp_ms
        LIMIT 1
    ) THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'Video Parse V2 timestamp source is outside the MySQL TIMESTAMP range';
    END IF;
END$$

CALL assert_video_parse_v2_timestamp_range()$$
DROP PROCEDURE assert_video_parse_v2_timestamp_range$$

DELIMITER ;

-- Stage UTC TIMESTAMP(3) values beside the signed BIGINT source columns.
ALTER TABLE video
    ADD COLUMN created_at_v2 TIMESTAMP(3) NULL,
    ADD COLUMN updated_at_v2 TIMESTAMP(3) NULL;

UPDATE video
SET created_at_v2 = TIMESTAMPADD(
        MICROSECOND,
        MOD(created_at, 1000) * 1000,
        TIMESTAMPADD(SECOND, created_at DIV 1000, '1970-01-01 00:00:00')
    ),
    updated_at_v2 = TIMESTAMPADD(
        MICROSECOND,
        MOD(updated_at, 1000) * 1000,
        TIMESTAMPADD(SECOND, updated_at DIV 1000, '1970-01-01 00:00:00')
    );

ALTER TABLE video_chunk
    ADD COLUMN created_at_v2 TIMESTAMP(3) NULL,
    ADD COLUMN updated_at_v2 TIMESTAMP(3) NULL;

UPDATE video_chunk
SET created_at_v2 = TIMESTAMPADD(
        MICROSECOND,
        MOD(created_at, 1000) * 1000,
        TIMESTAMPADD(SECOND, created_at DIV 1000, '1970-01-01 00:00:00')
    ),
    updated_at_v2 = TIMESTAMPADD(
        MICROSECOND,
        MOD(updated_at, 1000) * 1000,
        TIMESTAMPADD(SECOND, updated_at DIV 1000, '1970-01-01 00:00:00')
    );

ALTER TABLE upload_session
    ADD COLUMN completion_started_at_v2 TIMESTAMP(3) NULL,
    ADD COLUMN expires_at_v2 TIMESTAMP(3) NULL,
    ADD COLUMN completed_at_v2 TIMESTAMP(3) NULL,
    ADD COLUMN created_at_v2 TIMESTAMP(3) NULL,
    ADD COLUMN updated_at_v2 TIMESTAMP(3) NULL;

UPDATE upload_session
SET completion_started_at_v2 = CASE
        WHEN completion_started_at IS NULL THEN NULL
        ELSE TIMESTAMPADD(
            MICROSECOND,
            MOD(completion_started_at, 1000) * 1000,
            TIMESTAMPADD(SECOND, completion_started_at DIV 1000, '1970-01-01 00:00:00')
        )
    END,
    expires_at_v2 = TIMESTAMPADD(
        MICROSECOND,
        MOD(expires_at, 1000) * 1000,
        TIMESTAMPADD(SECOND, expires_at DIV 1000, '1970-01-01 00:00:00')
    ),
    completed_at_v2 = CASE
        WHEN completed_at IS NULL THEN NULL
        ELSE TIMESTAMPADD(
            MICROSECOND,
            MOD(completed_at, 1000) * 1000,
            TIMESTAMPADD(SECOND, completed_at DIV 1000, '1970-01-01 00:00:00')
        )
    END,
    created_at_v2 = TIMESTAMPADD(
        MICROSECOND,
        MOD(created_at, 1000) * 1000,
        TIMESTAMPADD(SECOND, created_at DIV 1000, '1970-01-01 00:00:00')
    ),
    updated_at_v2 = TIMESTAMPADD(
        MICROSECOND,
        MOD(updated_at, 1000) * 1000,
        TIMESTAMPADD(SECOND, updated_at DIV 1000, '1970-01-01 00:00:00')
    );

ALTER TABLE upload_segment
    ADD COLUMN created_at_v2 TIMESTAMP(3) NULL;

UPDATE upload_segment
SET created_at_v2 = TIMESTAMPADD(
    MICROSECOND,
    MOD(created_at, 1000) * 1000,
    TIMESTAMPADD(SECOND, created_at DIV 1000, '1970-01-01 00:00:00')
);

ALTER TABLE parse_usage_attempt
    ADD COLUMN started_at_v2 TIMESTAMP(3) NULL;

UPDATE parse_usage_attempt
SET started_at_v2 = TIMESTAMPADD(
    MICROSECOND,
    MOD(started_at, 1000) * 1000,
    TIMESTAMPADD(SECOND, started_at DIV 1000, '1970-01-01 00:00:00')
);

ALTER TABLE parse_model_call
    ADD COLUMN started_at_v2 TIMESTAMP(3) NULL,
    ADD COLUMN created_at_v2 TIMESTAMP(3) NULL,
    ADD COLUMN updated_at_v2 TIMESTAMP(3) NULL;

UPDATE parse_model_call
SET started_at_v2 = TIMESTAMPADD(
        MICROSECOND,
        MOD(started_at, 1000) * 1000,
        TIMESTAMPADD(SECOND, started_at DIV 1000, '1970-01-01 00:00:00')
    ),
    created_at_v2 = TIMESTAMPADD(
        MICROSECOND,
        MOD(created_at, 1000) * 1000,
        TIMESTAMPADD(SECOND, created_at DIV 1000, '1970-01-01 00:00:00')
    ),
    updated_at_v2 = TIMESTAMPADD(
        MICROSECOND,
        MOD(updated_at, 1000) * 1000,
        TIMESTAMPADD(SECOND, updated_at DIV 1000, '1970-01-01 00:00:00')
    );

DELIMITER $$

DROP PROCEDURE IF EXISTS assert_video_parse_v2_timestamp_copy$$
CREATE PROCEDURE assert_video_parse_v2_timestamp_copy()
BEGIN
    IF EXISTS (
        SELECT 1 FROM video
        WHERE created_at_v2 IS NULL OR updated_at_v2 IS NULL
           OR TIMESTAMPDIFF(MICROSECOND, '1970-01-01 00:00:00', created_at_v2) DIV 1000 <> created_at
           OR TIMESTAMPDIFF(MICROSECOND, '1970-01-01 00:00:00', updated_at_v2) DIV 1000 <> updated_at
        LIMIT 1
    ) OR EXISTS (
        SELECT 1 FROM video_chunk
        WHERE created_at_v2 IS NULL OR updated_at_v2 IS NULL
           OR TIMESTAMPDIFF(MICROSECOND, '1970-01-01 00:00:00', created_at_v2) DIV 1000 <> created_at
           OR TIMESTAMPDIFF(MICROSECOND, '1970-01-01 00:00:00', updated_at_v2) DIV 1000 <> updated_at
        LIMIT 1
    ) OR EXISTS (
        SELECT 1 FROM upload_session
        WHERE (completion_started_at IS NULL) <> (completion_started_at_v2 IS NULL)
           OR expires_at_v2 IS NULL
           OR (completed_at IS NULL) <> (completed_at_v2 IS NULL)
           OR created_at_v2 IS NULL
           OR updated_at_v2 IS NULL
           OR (completion_started_at IS NOT NULL AND TIMESTAMPDIFF(MICROSECOND, '1970-01-01 00:00:00', completion_started_at_v2) DIV 1000 <> completion_started_at)
           OR TIMESTAMPDIFF(MICROSECOND, '1970-01-01 00:00:00', expires_at_v2) DIV 1000 <> expires_at
           OR (completed_at IS NOT NULL AND TIMESTAMPDIFF(MICROSECOND, '1970-01-01 00:00:00', completed_at_v2) DIV 1000 <> completed_at)
           OR TIMESTAMPDIFF(MICROSECOND, '1970-01-01 00:00:00', created_at_v2) DIV 1000 <> created_at
           OR TIMESTAMPDIFF(MICROSECOND, '1970-01-01 00:00:00', updated_at_v2) DIV 1000 <> updated_at
        LIMIT 1
    ) OR EXISTS (
        SELECT 1 FROM upload_segment
        WHERE created_at_v2 IS NULL
           OR TIMESTAMPDIFF(MICROSECOND, '1970-01-01 00:00:00', created_at_v2) DIV 1000 <> created_at
        LIMIT 1
    ) OR EXISTS (
        SELECT 1 FROM parse_usage_attempt
        WHERE started_at_v2 IS NULL
           OR TIMESTAMPDIFF(MICROSECOND, '1970-01-01 00:00:00', started_at_v2) DIV 1000 <> started_at
        LIMIT 1
    ) OR EXISTS (
        SELECT 1 FROM parse_model_call
        WHERE started_at_v2 IS NULL OR created_at_v2 IS NULL OR updated_at_v2 IS NULL
           OR TIMESTAMPDIFF(MICROSECOND, '1970-01-01 00:00:00', started_at_v2) DIV 1000 <> started_at
           OR TIMESTAMPDIFF(MICROSECOND, '1970-01-01 00:00:00', created_at_v2) DIV 1000 <> created_at
           OR TIMESTAMPDIFF(MICROSECOND, '1970-01-01 00:00:00', updated_at_v2) DIV 1000 <> updated_at
        LIMIT 1
    ) THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'Video Parse V2 timestamp copy did not round-trip exactly';
    END IF;
END$$

CALL assert_video_parse_v2_timestamp_copy()$$
DROP PROCEDURE assert_video_parse_v2_timestamp_copy$$

DELIMITER ;

-- Swap only after every source value has passed the exact millisecond check.
ALTER TABLE video
    DROP COLUMN created_at,
    DROP COLUMN updated_at,
    CHANGE COLUMN created_at_v2 created_at TIMESTAMP(3) NOT NULL,
    CHANGE COLUMN updated_at_v2 updated_at TIMESTAMP(3) NOT NULL;

ALTER TABLE video_chunk
    DROP COLUMN created_at,
    DROP COLUMN updated_at,
    CHANGE COLUMN created_at_v2 created_at TIMESTAMP(3) NOT NULL,
    CHANGE COLUMN updated_at_v2 updated_at TIMESTAMP(3) NOT NULL;

ALTER TABLE upload_session
    DROP COLUMN completion_started_at,
    DROP COLUMN expires_at,
    DROP COLUMN completed_at,
    DROP COLUMN created_at,
    DROP COLUMN updated_at,
    CHANGE COLUMN completion_started_at_v2 completion_started_at TIMESTAMP(3) NULL,
    CHANGE COLUMN expires_at_v2 expires_at TIMESTAMP(3) NOT NULL,
    CHANGE COLUMN completed_at_v2 completed_at TIMESTAMP(3) NULL,
    CHANGE COLUMN created_at_v2 created_at TIMESTAMP(3) NOT NULL,
    CHANGE COLUMN updated_at_v2 updated_at TIMESTAMP(3) NOT NULL;

ALTER TABLE upload_segment
    DROP COLUMN created_at,
    CHANGE COLUMN created_at_v2 created_at TIMESTAMP(3) NOT NULL;

ALTER TABLE parse_usage_attempt
    DROP COLUMN started_at,
    CHANGE COLUMN started_at_v2 started_at TIMESTAMP(3) NOT NULL;

ALTER TABLE parse_model_call
    DROP INDEX ix_parse_model_call_job_started,
    DROP COLUMN started_at,
    DROP COLUMN created_at,
    DROP COLUMN updated_at,
    CHANGE COLUMN started_at_v2 started_at TIMESTAMP(3) NOT NULL,
    CHANGE COLUMN created_at_v2 created_at TIMESTAMP(3) NOT NULL,
    CHANGE COLUMN updated_at_v2 updated_at TIMESTAMP(3) NOT NULL,
    ADD INDEX ix_parse_model_call_job_started (job_id, started_at);

CREATE TABLE video_parse_task (
    id VARCHAR(36) NOT NULL,
    video_id VARCHAR(36) NOT NULL,
    pipeline_version VARCHAR(128) NOT NULL,
    status VARCHAR(16) NOT NULL,
    parse_config JSON NOT NULL,
    chunk_count INT NOT NULL,
    version INT NOT NULL,
    next_execute_time TIMESTAMP(3) NOT NULL,
    attempt_count INT NOT NULL DEFAULT 0,
    max_attempts INT NOT NULL,
    lease_owner VARCHAR(128) NULL,
    lease_version INT NOT NULL DEFAULT 0,
    lease_expire_time TIMESTAMP(3) NULL,
    heartbeat_time TIMESTAMP(3) NULL,
    start_time TIMESTAMP(3) NULL,
    finish_time TIMESTAMP(3) NULL,
    processing_duration_ms BIGINT NOT NULL DEFAULT 0,
    processing_timing_complete BOOL NOT NULL DEFAULT TRUE,
    last_error JSON NULL,
    is_test BOOL NOT NULL DEFAULT FALSE,
    is_deleted BOOL NOT NULL DEFAULT FALSE,
    detail JSON NOT NULL,
    create_time TIMESTAMP(3) NOT NULL,
    update_time TIMESTAMP(3) NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT fk_video_parse_task_video
        FOREIGN KEY (video_id) REFERENCES video(video_id) ON DELETE CASCADE,
    CONSTRAINT uq_video_parse_task_video_version UNIQUE (video_id, version),
    CONSTRAINT ck_video_parse_task_status
        CHECK (status IN ('PENDING', 'PREPARING', 'RUNNING', 'COMPLETE', 'FAILED')),
    CONSTRAINT ck_video_parse_task_chunk_count CHECK (chunk_count > 0),
    CONSTRAINT ck_video_parse_task_version CHECK (version > 0),
    CONSTRAINT ck_video_parse_task_attempt_count CHECK (attempt_count >= 0),
    CONSTRAINT ck_video_parse_task_max_attempts CHECK (max_attempts > 0),
    CONSTRAINT ck_video_parse_task_lease_version CHECK (lease_version >= 0),
    CONSTRAINT ck_video_parse_task_processing_duration CHECK (processing_duration_ms >= 0),
    CONSTRAINT ck_video_parse_task_lease_state CHECK (
        (status IN ('PREPARING', 'RUNNING')
            AND lease_owner IS NOT NULL
            AND lease_expire_time IS NOT NULL
            AND heartbeat_time IS NOT NULL)
        OR
        (status IN ('PENDING', 'COMPLETE', 'FAILED')
            AND lease_owner IS NULL
            AND lease_expire_time IS NULL
            AND heartbeat_time IS NULL)
    ),
    INDEX ix_video_parse_task_due (status, next_execute_time),
    INDEX ix_video_parse_task_lease (status, lease_expire_time),
    INDEX ix_video_parse_task_video_status (video_id, status, version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE chunk_parse_task (
    id VARCHAR(36) NOT NULL,
    video_parse_task_id VARCHAR(36) NOT NULL,
    video_chunk_id VARCHAR(36) NOT NULL,
    status VARCHAR(16) NOT NULL,
    next_execute_time TIMESTAMP(3) NOT NULL,
    retry_count INT NOT NULL DEFAULT 0,
    lease_owner VARCHAR(128) NULL,
    lease_version INT NOT NULL DEFAULT 0,
    lease_expire_time TIMESTAMP(3) NULL,
    start_time TIMESTAMP(3) NULL,
    finish_time TIMESTAMP(3) NULL,
    last_error JSON NULL,
    detail JSON NULL,
    create_time TIMESTAMP(3) NOT NULL,
    update_time TIMESTAMP(3) NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT fk_chunk_parse_task_parent
        FOREIGN KEY (video_parse_task_id) REFERENCES video_parse_task(id) ON DELETE CASCADE,
    CONSTRAINT fk_chunk_parse_task_video_chunk
        FOREIGN KEY (video_chunk_id) REFERENCES video_chunk(video_chunk_id) ON DELETE CASCADE,
    CONSTRAINT uq_chunk_parse_task_parent_chunk UNIQUE (video_parse_task_id, video_chunk_id),
    CONSTRAINT uq_chunk_parse_task_id_parent UNIQUE (id, video_parse_task_id),
    CONSTRAINT ck_chunk_parse_task_status
        CHECK (status IN ('QUEUED', 'UNDERSTANDING', 'COMPLETE', 'FAILED')),
    CONSTRAINT ck_chunk_parse_task_retry_count CHECK (retry_count >= 0),
    CONSTRAINT ck_chunk_parse_task_lease_version CHECK (lease_version >= 0),
    CONSTRAINT ck_chunk_parse_task_lease_state CHECK (
        (status = 'UNDERSTANDING' AND lease_owner IS NOT NULL AND lease_expire_time IS NOT NULL)
        OR
        (status <> 'UNDERSTANDING' AND lease_owner IS NULL AND lease_expire_time IS NULL)
    ),
    INDEX ix_chunk_parse_task_due (status, next_execute_time),
    INDEX ix_chunk_parse_task_lease (status, lease_expire_time),
    INDEX ix_chunk_parse_task_parent_status (video_parse_task_id, status, video_chunk_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE chunk_parse_content (
    id VARCHAR(36) NOT NULL,
    chunk_parse_task_id VARCHAR(36) NOT NULL,
    content JSON NULL,
    version INT NOT NULL DEFAULT 1,
    detail JSON NULL,
    create_time TIMESTAMP(3) NOT NULL,
    update_time TIMESTAMP(3) NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT fk_chunk_parse_content_task
        FOREIGN KEY (chunk_parse_task_id) REFERENCES chunk_parse_task(id) ON DELETE CASCADE,
    CONSTRAINT uq_chunk_parse_content_task UNIQUE (chunk_parse_task_id),
    CONSTRAINT ck_chunk_parse_content_version CHECK (version > 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE interval_parse_task (
    id VARCHAR(36) NOT NULL,
    chunk_parse_task_id VARCHAR(36) NOT NULL,
    video_parse_task_id VARCHAR(36) NOT NULL,
    chunk_parse_task_ids JSON NOT NULL,
    start_sequence_no INT NOT NULL,
    chunk_count INT NOT NULL,
    ready_mask BIGINT NOT NULL DEFAULT 0,
    status VARCHAR(16) NOT NULL,
    next_execute_time TIMESTAMP(3) NOT NULL,
    retry_count INT NOT NULL DEFAULT 0,
    lease_owner VARCHAR(128) NULL,
    lease_version INT NOT NULL DEFAULT 0,
    lease_expire_time TIMESTAMP(3) NULL,
    start_time TIMESTAMP(3) NULL,
    finish_time TIMESTAMP(3) NULL,
    last_error JSON NULL,
    create_time TIMESTAMP(3) NOT NULL,
    update_time TIMESTAMP(3) NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT fk_interval_parse_task_parent
        FOREIGN KEY (video_parse_task_id) REFERENCES video_parse_task(id) ON DELETE CASCADE,
    CONSTRAINT fk_interval_parse_task_anchor_parent
        FOREIGN KEY (chunk_parse_task_id, video_parse_task_id)
        REFERENCES chunk_parse_task(id, video_parse_task_id) ON DELETE CASCADE,
    CONSTRAINT uq_interval_parse_task_parent_start UNIQUE (video_parse_task_id, start_sequence_no),
    CONSTRAINT ck_interval_parse_task_start_sequence CHECK (start_sequence_no >= 0),
    CONSTRAINT ck_interval_parse_task_chunk_count CHECK (chunk_count BETWEEN 1 AND 63),
    CONSTRAINT ck_interval_parse_task_ready_mask
        CHECK (ready_mask >= 0 AND (ready_mask >> chunk_count) = 0),
    CONSTRAINT ck_interval_parse_task_status
        CHECK (status IN ('WAITING', 'REFINING', 'COMPLETE', 'FAILED')),
    CONSTRAINT ck_interval_parse_task_retry_count CHECK (retry_count >= 0),
    CONSTRAINT ck_interval_parse_task_lease_version CHECK (lease_version >= 0),
    CONSTRAINT ck_interval_parse_task_lease_state CHECK (
        (status = 'REFINING' AND lease_owner IS NOT NULL AND lease_expire_time IS NOT NULL)
        OR
        (status <> 'REFINING' AND lease_owner IS NULL AND lease_expire_time IS NULL)
    ),
    INDEX ix_interval_parse_task_due (status, next_execute_time),
    INDEX ix_interval_parse_task_lease (status, lease_expire_time),
    INDEX ix_interval_parse_task_parent_status (video_parse_task_id, status, start_sequence_no)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE interval_parse_content (
    interval_parse_task_id VARCHAR(36) NOT NULL,
    content JSON NOT NULL,
    detail JSON NULL,
    create_time TIMESTAMP(3) NOT NULL,
    update_time TIMESTAMP(3) NOT NULL,
    PRIMARY KEY (interval_parse_task_id),
    CONSTRAINT fk_interval_parse_content_task
        FOREIGN KEY (interval_parse_task_id) REFERENCES interval_parse_task(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

ALTER TABLE upload_session
    ADD COLUMN video_parse_config JSON NULL AFTER parse_config,
    ADD COLUMN detail JSON NULL AFTER video_parse_config,
    ADD COLUMN initial_video_parse_task_id VARCHAR(36) NULL AFTER job_id,
    ADD CONSTRAINT fk_upload_session_initial_video_parse_task
        FOREIGN KEY (initial_video_parse_task_id)
        REFERENCES video_parse_task(id) ON DELETE SET NULL,
    ADD INDEX ix_upload_session_initial_video_parse_task_id (initial_video_parse_task_id);

ALTER TABLE parse_usage_attempt
    ADD COLUMN video_parse_task_id VARCHAR(36) NULL AFTER job_id,
    ADD CONSTRAINT fk_parse_usage_attempt_video_parse_task
        FOREIGN KEY (video_parse_task_id)
        REFERENCES video_parse_task(id) ON DELETE SET NULL,
    ADD INDEX ix_parse_usage_attempt_video_parse_task_id (video_parse_task_id);

ALTER TABLE parse_model_call
    ADD COLUMN video_parse_task_id VARCHAR(36) NULL AFTER job_id,
    ADD CONSTRAINT fk_parse_model_call_video_parse_task
        FOREIGN KEY (video_parse_task_id)
        REFERENCES video_parse_task(id) ON DELETE SET NULL,
    ADD INDEX ix_parse_model_call_video_parse_task_started
        (video_parse_task_id, started_at);

-- Verification: no V1 rows are synthesized as V2 tasks; historical config
-- remains nullable because it cannot be reconstructed safely.
SELECT
    (SELECT COUNT(*) FROM video_parse_task) AS video_parse_task_count,
    (SELECT COUNT(*) FROM chunk_parse_task) AS chunk_parse_task_count,
    (SELECT COUNT(*) FROM chunk_parse_content) AS chunk_parse_content_count,
    (SELECT COUNT(*) FROM interval_parse_task) AS interval_parse_task_count,
    (SELECT COUNT(*) FROM interval_parse_content) AS interval_parse_content_count;

DELIMITER $$

DROP PROCEDURE IF EXISTS assert_video_parse_v2_timestamp_schema$$
CREATE PROCEDURE assert_video_parse_v2_timestamp_schema()
BEGIN
    DECLARE actual_timestamp_column_count INT DEFAULT 0;
    DECLARE invalid_timestamp_column_count INT DEFAULT 0;

    SELECT
        COUNT(*),
        COALESCE(SUM(
            CASE
                WHEN data_type = 'timestamp' AND datetime_precision = 3 THEN 0
                ELSE 1
            END
        ), 0)
    INTO actual_timestamp_column_count, invalid_timestamp_column_count
    FROM information_schema.columns
    WHERE table_schema = DATABASE()
      AND (
          (table_name = 'video' AND column_name IN ('created_at', 'updated_at'))
          OR (table_name = 'video_chunk' AND column_name IN ('created_at', 'updated_at'))
          OR (table_name = 'upload_session' AND column_name IN ('completion_started_at', 'expires_at', 'completed_at', 'created_at', 'updated_at'))
          OR (table_name = 'upload_segment' AND column_name = 'created_at')
          OR (table_name = 'parse_usage_attempt' AND column_name = 'started_at')
          OR (table_name = 'parse_model_call' AND column_name IN ('started_at', 'created_at', 'updated_at'))
          OR (table_name = 'video_parse_task' AND column_name IN ('next_execute_time', 'lease_expire_time', 'heartbeat_time', 'start_time', 'finish_time', 'create_time', 'update_time'))
          OR (table_name = 'chunk_parse_task' AND column_name IN ('next_execute_time', 'lease_expire_time', 'start_time', 'finish_time', 'create_time', 'update_time'))
          OR (table_name = 'chunk_parse_content' AND column_name IN ('create_time', 'update_time'))
          OR (table_name = 'interval_parse_task' AND column_name IN ('next_execute_time', 'lease_expire_time', 'start_time', 'finish_time', 'create_time', 'update_time'))
          OR (table_name = 'interval_parse_content' AND column_name IN ('create_time', 'update_time'))
      );

    IF actual_timestamp_column_count <> 37
       OR invalid_timestamp_column_count <> 0 THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'Video Parse V2 absolute timestamp columns must all exist as TIMESTAMP(3)';
    END IF;
END$$

CALL assert_video_parse_v2_timestamp_schema()$$
DROP PROCEDURE assert_video_parse_v2_timestamp_schema$$

DELIMITER ;
