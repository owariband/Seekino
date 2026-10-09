-- AniVAG controlled migration: the pre-identity 22-table schema -> 24 tables.
-- Prepared for review; the application never executes this file automatically.
-- Apply after the existing V2/runtime and media-millisecond migrations, with
-- API/Worker writes stopped and a verified backup. MySQL DDL implicitly commits:
-- stop on the first error, inspect partial state, and never use mysql --force.
-- This script supports the known local-default legacy scope only. Other scope
-- IDs need an explicit ownership mapping before migration; they are not guessed.
-- Existing resource IDs, scope values, timestamps, model usage and JSON remain.
-- Historical creators/actors stay NULL. Local conversations explicitly acquire
-- local-user ownership. No course is published and no content changes Tenant.
SET time_zone = '+00:00';

DELIMITER $$
DROP PROCEDURE IF EXISTS assert_user_tenant_identity_source$$
CREATE PROCEDURE assert_user_tenant_identity_source()
BEGIN
    DECLARE scope_columns INT DEFAULT 0;
    DECLARE prerequisite_tables INT DEFAULT 0;
    DECLARE legacy_indexes INT DEFAULT 0;
    IF DATABASE() IS NULL THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'select the intended database explicitly';
    END IF;
    SELECT COUNT(*) INTO prerequisite_tables FROM information_schema.tables
    WHERE table_schema = DATABASE() AND table_name IN
        ('series', 'upload_session', 'agent_conversation', 'model_usage',
         'parse_model_call', 'series_parse_job', 'video_parse_task');
    SELECT COUNT(*) INTO scope_columns FROM information_schema.columns
    WHERE table_schema = DATABASE() AND column_name = 'workspace_id'
      AND table_name IN ('series', 'upload_session', 'agent_conversation',
                        'model_usage', 'parse_model_call');
    IF prerequisite_tables <> 7 OR scope_columns <> 5 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'expected complete pre-identity V2 schema with five workspace_id columns';
    END IF;
    SELECT COUNT(DISTINCT CONCAT(table_name, ':', index_name)) INTO legacy_indexes
    FROM information_schema.statistics WHERE table_schema = DATABASE() AND (
        (table_name = 'series' AND index_name = 'uq_series_workspace_name')
        OR (table_name = 'agent_conversation' AND index_name IN
            ('ix_agent_conversation_workspace_id', 'uq_agent_conversation_workspace_idempotency'))
        OR (table_name = 'upload_session' AND index_name = 'uq_upload_session_workspace_idempotency')
        OR (table_name = 'model_usage' AND index_name IN
            ('ix_model_usage_workspace_started', 'ix_model_usage_workspace_run_started'))
    );
    IF legacy_indexes <> 6 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'legacy scope index names differ; review before any identity DDL';
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.tables
               WHERE table_schema = DATABASE() AND table_name IN ('user', 'tenant'))
       OR EXISTS (SELECT 1 FROM information_schema.columns
                  WHERE table_schema = DATABASE() AND (
                    (table_name IN ('series', 'upload_session', 'agent_conversation',
                                    'model_usage', 'parse_model_call') AND column_name = 'tenant_id')
                    OR (table_name = 'series' AND column_name = 'is_published')
                    OR (table_name = 'agent_conversation' AND column_name = 'owner_user_id')
                    OR (table_name IN ('upload_session', 'series_parse_job', 'video_parse_task')
                        AND column_name = 'created_by_user_id')
                    OR (table_name = 'model_usage' AND column_name = 'actor_user_id')
                  )) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'identity migration already started; inspect state rather than rerun';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_schema = DATABASE() AND table_name = 'video'
                     AND column_name = 'duration_seconds'
                     AND column_comment = 'media_time_milliseconds') THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'apply the controlled media-millisecond migration first';
    END IF;
    IF EXISTS (SELECT 1 FROM series WHERE workspace_id IS NULL OR CAST(workspace_id AS BINARY) <> CAST('local-default' AS BINARY))
       OR EXISTS (SELECT 1 FROM upload_session WHERE workspace_id IS NULL OR CAST(workspace_id AS BINARY) <> CAST('local-default' AS BINARY))
       OR EXISTS (SELECT 1 FROM agent_conversation WHERE workspace_id IS NULL OR CAST(workspace_id AS BINARY) <> CAST('local-default' AS BINARY))
       OR EXISTS (SELECT 1 FROM model_usage WHERE workspace_id IS NULL OR CAST(workspace_id AS BINARY) <> CAST('local-default' AS BINARY))
       OR EXISTS (SELECT 1 FROM parse_model_call WHERE workspace_id IS NULL OR CAST(workspace_id AS BINARY) <> CAST('local-default' AS BINARY)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'legacy scopes need explicit mapping; only local-default is supported';
    END IF;
    IF EXISTS (SELECT 1 FROM series_parse_job WHERE lease_owner IS NOT NULL)
       OR EXISTS (SELECT 1 FROM video_parse_task WHERE lease_owner IS NOT NULL)
       OR EXISTS (SELECT 1 FROM chunk_parse_task WHERE lease_owner IS NOT NULL)
       OR EXISTS (SELECT 1 FROM interval_parse_task WHERE lease_owner IS NOT NULL) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'stop writers and release parse leases before identity migration';
    END IF;
END$$
CALL assert_user_tenant_identity_source()$$
DROP PROCEDURE assert_user_tenant_identity_source$$
DELIMITER ;

CREATE TABLE `user` (
    user_id VARCHAR(36) NOT NULL,
    email VARCHAR(254) NULL,
    is_guest BOOLEAN NOT NULL,
    status VARCHAR(16) NOT NULL,
    auth_version INTEGER NOT NULL,
    role VARCHAR(16) NOT NULL,
    created_at BIGINT NOT NULL,
    updated_at BIGINT NOT NULL,
    PRIMARY KEY (user_id),
    UNIQUE KEY uq_user_email (email),
    CONSTRAINT ck_user_auth_version CHECK (auth_version >= 1),
    CONSTRAINT ck_user_status CHECK (status IN ('ACTIVE', 'DISABLED')),
    CONSTRAINT ck_user_role CHECK (role IN ('user', 'admin')),
    CONSTRAINT ck_guest_email CHECK (is_guest = 0 OR email IS NULL),
    CONSTRAINT ck_guest_role CHECK (is_guest = 0 OR role = 'user')
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE tenant (
    tenant_id VARCHAR(64) NOT NULL,
    owner_user_id VARCHAR(36) NULL,
    type VARCHAR(16) NOT NULL,
    status VARCHAR(16) NOT NULL,
    name VARCHAR(255) NOT NULL,
    created_at BIGINT NOT NULL,
    updated_at BIGINT NOT NULL,
    PRIMARY KEY (tenant_id),
    KEY ix_tenant_owner_user_id (owner_user_id),
    CONSTRAINT fk_tenant_owner_user FOREIGN KEY (owner_user_id) REFERENCES `user` (user_id),
    CONSTRAINT ck_tenant_type CHECK (type IN ('local', 'public', 'personal')),
    CONSTRAINT ck_tenant_status CHECK (status IN ('ACTIVE', 'DISABLED')),
    CONSTRAINT ck_owned_tenant_owner CHECK (type = 'public' OR owner_user_id IS NOT NULL),
    CONSTRAINT ck_public_tenant_owner CHECK (type <> 'public' OR owner_user_id IS NULL)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

SET @identity_created_at = CAST(UNIX_TIMESTAMP(CURRENT_TIMESTAMP(3)) * 1000 AS SIGNED);
INSERT INTO `user` (user_id, email, is_guest, status, auth_version, role, created_at, updated_at)
VALUES ('local-user', NULL, FALSE, 'ACTIVE', 1, 'admin', @identity_created_at, @identity_created_at);
INSERT INTO tenant (tenant_id, owner_user_id, type, status, name, created_at, updated_at)
VALUES ('local-default', 'local-user', 'local', 'ACTIVE', '本地资料', @identity_created_at, @identity_created_at),
       ('platform-public', NULL, 'public', 'ACTIVE', '公开课程', @identity_created_at, @identity_created_at);

ALTER TABLE series
    RENAME COLUMN workspace_id TO tenant_id,
    RENAME INDEX uq_series_workspace_name TO uq_series_tenant_name,
    ADD COLUMN is_published BOOLEAN NOT NULL DEFAULT FALSE AFTER status;

ALTER TABLE agent_conversation
    RENAME COLUMN workspace_id TO tenant_id,
    RENAME INDEX ix_agent_conversation_workspace_id TO ix_agent_conversation_tenant_id,
    ADD COLUMN owner_user_id VARCHAR(64) NULL AFTER tenant_id;
UPDATE agent_conversation SET owner_user_id = 'local-user';
ALTER TABLE agent_conversation
    MODIFY COLUMN owner_user_id VARCHAR(64) NOT NULL,
    DROP INDEX uq_agent_conversation_workspace_idempotency,
    ADD CONSTRAINT uq_agent_conversation_tenant_idempotency
        UNIQUE (tenant_id, owner_user_id, idempotency_key);

ALTER TABLE upload_session
    RENAME COLUMN workspace_id TO tenant_id,
    ADD COLUMN created_by_user_id VARCHAR(64) NULL AFTER tenant_id,
    DROP INDEX uq_upload_session_workspace_idempotency,
    ADD CONSTRAINT uq_upload_session_tenant_idempotency
        UNIQUE (tenant_id, created_by_user_id, idempotency_key);

ALTER TABLE model_usage
    RENAME COLUMN workspace_id TO tenant_id,
    RENAME INDEX ix_model_usage_workspace_started TO ix_model_usage_tenant_started,
    RENAME INDEX ix_model_usage_workspace_run_started TO ix_model_usage_tenant_run_started,
    ADD COLUMN actor_user_id VARCHAR(64) NULL AFTER tenant_id,
    ADD INDEX ix_model_usage_tenant_actor_started (tenant_id, actor_user_id, started_at);

ALTER TABLE parse_model_call
    RENAME COLUMN workspace_id TO tenant_id;
ALTER TABLE series_parse_job ADD COLUMN created_by_user_id VARCHAR(64) NULL AFTER job_id;
ALTER TABLE video_parse_task ADD COLUMN created_by_user_id VARCHAR(64) NULL AFTER id;

-- Read verification: no historical identity is invented and nothing is public.
SELECT type, COUNT(*) AS tenant_count FROM tenant GROUP BY type;
SELECT COUNT(*) AS unexpectedly_published FROM series WHERE is_published = TRUE;
SELECT COUNT(*) AS missing_local_owner FROM agent_conversation WHERE owner_user_id <> 'local-user';
SELECT COUNT(*) AS attributed_historical_calls FROM model_usage WHERE actor_user_id IS NOT NULL;
-- Old nested evidence workspaceId is handled by the repository read adapter;
-- no recursive JSON replacement runs here. ES migration is a separate dry-run
-- script: python -m scripts.migrate_tenant_index. Keep its old physical index.
