-- Seekino controlled migration: Interval indexing and task failure boundaries.
-- Run against a backed-up database with ALL application writers stopped.
-- This script supports only empty V2 task/result tables and empty (or absent)
-- ES publication tables. It refuses existing task/results instead of guessing
-- whether an old COMPLETE row includes indexing or how FAILED should resume.
-- V1 data and unrelated business tables are not changed.
-- MySQL DDL implicitly commits: this is NOT an atomic multi-DDL transaction.
-- Inspect the schema after any DDL error before retrying; never use --force.
SET time_zone = '+00:00';

DELIMITER $$
DROP PROCEDURE IF EXISTS migrate_task_indexing_contract$$
CREATE PROCEDURE migrate_task_indexing_contract()
BEGIN
    DECLARE source_count INT DEFAULT 0;
    DECLARE check_count INT DEFAULT 0;

    SELECT COUNT(*) INTO source_count
    FROM information_schema.tables
    WHERE table_schema = DATABASE()
      AND table_type = 'BASE TABLE'
      AND table_name IN ('video_parse_task', 'chunk_parse_task',
                         'chunk_parse_content', 'interval_parse_task',
                         'interval_parse_content');
    IF source_count <> 5 THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'Task indexing migration requires all five V2 task/result tables';
    END IF;

    IF EXISTS (SELECT 1 FROM video_parse_task LIMIT 1)
       OR EXISTS (SELECT 1 FROM chunk_parse_task LIMIT 1)
       OR EXISTS (SELECT 1 FROM chunk_parse_content LIMIT 1)
       OR EXISTS (SELECT 1 FROM interval_parse_task LIMIT 1)
       OR EXISTS (SELECT 1 FROM interval_parse_content LIMIT 1) THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'Task indexing migration requires empty V2 task/result tables; nonempty data needs a separate migration';
    END IF;

    IF EXISTS (SELECT 1 FROM information_schema.tables
               WHERE table_schema = DATABASE() AND table_name = 'es_index_build') THEN
        IF EXISTS (SELECT 1 FROM es_index_build LIMIT 1) THEN
            SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'Task indexing migration refuses nonempty es_index_build';
        END IF;
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.tables
               WHERE table_schema = DATABASE() AND table_name = 'es_index_build_chunk') THEN
        IF EXISTS (SELECT 1 FROM es_index_build_chunk LIMIT 1) THEN
            SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'Task indexing migration refuses nonempty es_index_build_chunk';
        END IF;
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.tables
               WHERE table_schema = DATABASE() AND table_name = 'es_video_publication') THEN
        IF EXISTS (SELECT 1 FROM es_video_publication LIMIT 1) THEN
            SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'Task indexing migration refuses nonempty es_video_publication';
        END IF;
    END IF;

    SELECT COUNT(*) INTO check_count
    FROM information_schema.table_constraints
    WHERE table_schema = DATABASE() AND constraint_type = 'CHECK'
      AND (
          (table_name = 'video_parse_task' AND constraint_name IN
              ('ck_video_parse_task_status', 'ck_video_parse_task_lease_state'))
          OR (table_name = 'interval_parse_task' AND constraint_name IN
              ('ck_interval_parse_task_status', 'ck_interval_parse_task_lease_state'))
      );
    IF check_count <> 4 THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'Task indexing migration requires the four named Video/Interval status and lease CHECK constraints';
    END IF;

    -- All row guards run before any application-table DDL. A SIGNAL stops this
    -- CALL, so no later ALTER/DROP in the procedure runs after a failed guard.
    ALTER TABLE video_parse_task
        DROP CHECK ck_video_parse_task_status,
        DROP CHECK ck_video_parse_task_lease_state,
        ADD CONSTRAINT ck_video_parse_task_status CHECK (
            status IN ('PENDING', 'PREPARING', 'PARSING', 'INDEXING',
                       'COMPLETE', 'PARSING_FAILED', 'INDEXING_FAILED')
        ),
        ADD CONSTRAINT ck_video_parse_task_lease_state CHECK (
            (status IN ('PREPARING', 'PARSING', 'INDEXING')
             AND lease_owner IS NOT NULL
             AND lease_expire_time IS NOT NULL
             AND heartbeat_time IS NOT NULL)
            OR (lease_owner IS NULL
                AND lease_expire_time IS NULL
                AND heartbeat_time IS NULL)
        );

    ALTER TABLE interval_parse_task
        DROP CHECK ck_interval_parse_task_status,
        DROP CHECK ck_interval_parse_task_lease_state,
        ADD CONSTRAINT ck_interval_parse_task_status CHECK (
            status IN ('WAITING', 'REFINING', 'INDEXING', 'COMPLETE',
                       'PARSING_FAILED', 'INDEXING_FAILED')
        ),
        ADD CONSTRAINT ck_interval_parse_task_lease_state CHECK (
            (status IN ('REFINING', 'INDEXING')
             AND lease_owner IS NOT NULL AND lease_expire_time IS NOT NULL)
            OR (status IN ('WAITING', 'INDEXING', 'COMPLETE',
                          'PARSING_FAILED', 'INDEXING_FAILED')
                AND lease_owner IS NULL AND lease_expire_time IS NULL)
        );

    DROP TABLE IF EXISTS es_video_publication;
    DROP TABLE IF EXISTS es_index_build_chunk;
    DROP TABLE IF EXISTS es_index_build;
END$$
CALL migrate_task_indexing_contract()$$
DROP PROCEDURE migrate_task_indexing_contract$$
DELIMITER ;
