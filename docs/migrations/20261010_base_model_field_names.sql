-- Seekino controlled MySQL 8.0.16+ migration: 24 identity/V2 business tables.
-- Prepared SQL only. Stop API/Worker/queue writes, release leases, take and verify
-- a restorable backup, select the intended database, then use a client that stops
-- on errors. Never use mysql --force. The application never executes this file.
-- Preconditions: all previous identity, V2/runtime and millisecond migrations.
-- There are 77 business/identity column renames plus four common audit columns.
-- IDs, foreign-key values, media milliseconds and JSON/artifacts are preserved.
-- MySQL DDL implicitly commits; this file is NOT an atomic transaction. If an
-- error occurs after the first business ALTER, restore/review partial state.
-- Do not rerun a partially migrated database. Source manifests deliberately
-- reject both partial and completed migrations. Foreign-key checks stay ON.
-- All source schema/data checks finish before the first business-table ALTER.

DELIMITER $$
DROP PROCEDURE IF EXISTS migrate_base_model_field_names$$
CREATE PROCEDURE migrate_base_model_field_names()
BEGIN
    DECLARE previous_time_zone VARCHAR(64);
    DECLARE previous_sql_mode TEXT;
    DECLARE migration_lock VARCHAR(128);
    DECLARE lock_acquired INT DEFAULT 0;
    DECLARE violation_count BIGINT DEFAULT 0;
    DECLARE EXIT HANDLER FOR SQLEXCEPTION
    BEGIN
        -- DDL already committed is intentionally not described as rolled back.
        IF previous_time_zone IS NOT NULL THEN SET SESSION time_zone = previous_time_zone; END IF;
        IF previous_sql_mode IS NOT NULL THEN SET SESSION sql_mode = previous_sql_mode; END IF;
        IF lock_acquired = 1 THEN DO RELEASE_LOCK(migration_lock); END IF;
        RESIGNAL;
    END;
    SET previous_time_zone = @@SESSION.time_zone;
    SET previous_sql_mode = @@SESSION.sql_mode;
    IF DATABASE() IS NULL THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'select the intended database explicitly';
    END IF;
    IF VERSION() LIKE '%MariaDB%' THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'this migration requires MySQL 8.0.16+ with enforced CHECK constraints';
    END IF;
    IF CAST(SUBSTRING_INDEX(VERSION(), '.', 1) AS UNSIGNED) < 8 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'this migration requires MySQL 8.0.16+ with enforced CHECK constraints';
    END IF;
    IF (CAST(SUBSTRING_INDEX(VERSION(), '.', 1) AS UNSIGNED)=8
           AND CAST(SUBSTRING_INDEX(SUBSTRING_INDEX(VERSION(), '.', 2), '.', -1) AS UNSIGNED)=0
           AND CAST(SUBSTRING_INDEX(SUBSTRING_INDEX(VERSION(), '.', 3), '.', -1) AS UNSIGNED)<16) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'this migration requires MySQL 8.0.16+ with enforced CHECK constraints';
    END IF;
    IF @@SESSION.foreign_key_checks <> 1 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'foreign_key_checks must remain ON throughout this migration';
    END IF;
    IF @@SESSION.character_set_client <> 'utf8mb4' THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'use an utf8mb4 client with group_concat_max_len at least 1024';
    END IF;
    IF @@SESSION.group_concat_max_len < 1024 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'use an utf8mb4 client with group_concat_max_len at least 1024';
    END IF;
    SET migration_lock = CONCAT('seekino-base-model:', LEFT(SHA2(DATABASE(), 256), 40));
    SELECT GET_LOCK(migration_lock, 0) INTO lock_acquired;
    IF lock_acquired <> 1 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'another BaseModel migration owns the database migration lock';
    END IF;
    IF lock_acquired IS NULL THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'another BaseModel migration owns the database migration lock';
    END IF;
    SET SESSION time_zone = '+00:00';
    IF FIND_IN_SET('STRICT_ALL_TABLES', @@SESSION.sql_mode) = 0 THEN
        SET SESSION sql_mode = CONCAT_WS(',', NULLIF(@@SESSION.sql_mode, ''), 'STRICT_ALL_TABLES');
    END IF;

    -- Temporary manifests belong only to this connection, not to the application.
    CREATE TEMPORARY TABLE _bm_tables (table_name VARCHAR(64) PRIMARY KEY, rows_before BIGINT NULL);
    CREATE TEMPORARY TABLE _bm_source_cols (table_name VARCHAR(64), column_name VARCHAR(64),
        data_type VARCHAR(32), char_length BIGINT NULL, date_precision INT NULL,
        nullable VARCHAR(3), is_unsigned BOOLEAN, PRIMARY KEY(table_name, column_name));
    CREATE TEMPORARY TABLE _bm_target_cols LIKE _bm_source_cols;
    CREATE TEMPORARY TABLE _bm_source_fks (table_name VARCHAR(64), columns_csv VARCHAR(255),
        ref_table VARCHAR(64), ref_columns_csv VARCHAR(255), delete_rule VARCHAR(16), update_rule VARCHAR(16));
    CREATE TEMPORARY TABLE _bm_target_fks LIKE _bm_source_fks;
    CREATE TEMPORARY TABLE _bm_source_checks (table_name VARCHAR(64), check_name VARCHAR(64), clause TEXT);
    CREATE TEMPORARY TABLE _bm_target_checks LIKE _bm_source_checks;
    CREATE TEMPORARY TABLE _bm_source_keys (table_name VARCHAR(64), key_name VARCHAR(64) NULL,
        is_unique BOOLEAN, columns_csv VARCHAR(1024));
    CREATE TEMPORARY TABLE _bm_target_keys LIKE _bm_source_keys;
    INSERT INTO _bm_tables VALUES
        ('agent_conversation', NULL),
        ('model_usage', NULL),
        ('series', NULL),
        ('user', NULL),
        ('agent_message', NULL),
        ('tenant', NULL),
        ('video', NULL),
        ('series_parse_job', NULL),
        ('video_chunk', NULL),
        ('video_parse_task', NULL),
        ('chunk_parse_task', NULL),
        ('context_snapshot', NULL),
        ('index_sync_intent', NULL),
        ('parse_model_call', NULL),
        ('parse_usage_attempt', NULL),
        ('upload_session', NULL),
        ('video_chunk_checkpoint', NULL),
        ('video_chunk_embedding', NULL),
        ('video_chunk_info', NULL),
        ('video_parse_usage_attempt', NULL),
        ('chunk_parse_content', NULL),
        ('interval_parse_task', NULL),
        ('upload_segment', NULL),
        ('interval_parse_content', NULL);
    INSERT INTO _bm_source_cols VALUES
        ('agent_conversation', 'conversation_id', 'varchar', '36', NULL, 'NO', '0'),
        ('agent_conversation', 'tenant_id', 'varchar', '64', NULL, 'NO', '0'),
        ('agent_conversation', 'owner_user_id', 'varchar', '64', NULL, 'NO', '0'),
        ('agent_conversation', 'title', 'varchar', '255', NULL, 'NO', '0'),
        ('agent_conversation', 'title_is_auto', 'tinyint', NULL, NULL, 'NO', '0'),
        ('agent_conversation', 'idempotency_key', 'varchar', '128', NULL, 'NO', '0'),
        ('agent_conversation', 'next_message_sequence', 'int', NULL, NULL, 'NO', '0'),
        ('agent_conversation', 'last_message_at', 'bigint', NULL, NULL, 'YES', '0'),
        ('agent_conversation', 'created_at', 'bigint', NULL, NULL, 'NO', '0'),
        ('agent_conversation', 'updated_at', 'bigint', NULL, NULL, 'NO', '0'),
        ('model_usage', 'call_id', 'varchar', '36', NULL, 'NO', '0'),
        ('model_usage', 'tenant_id', 'varchar', '64', NULL, 'NO', '0'),
        ('model_usage', 'actor_user_id', 'varchar', '64', NULL, 'YES', '0'),
        ('model_usage', 'task_type', 'varchar', '16', NULL, 'NO', '0'),
        ('model_usage', 'run_id', 'varchar', '256', NULL, 'NO', '0'),
        ('model_usage', 'started_at', 'timestamp', NULL, '3', 'NO', '0'),
        ('model_usage', 'usage', 'json', NULL, NULL, 'NO', '0'),
        ('model_usage', 'metadata', 'json', NULL, NULL, 'NO', '0'),
        ('model_usage', 'created_at', 'timestamp', NULL, '3', 'NO', '0'),
        ('series', 'series_id', 'varchar', '36', NULL, 'NO', '0'),
        ('series', 'tenant_id', 'varchar', '64', NULL, 'NO', '0'),
        ('series', 'name', 'varchar', '255', NULL, 'NO', '0'),
        ('series', 'description', 'longtext', NULL, NULL, 'YES', '0'),
        ('series', 'transcript_language_hint', 'varchar', '64', NULL, 'YES', '0'),
        ('series', 'status', 'varchar', '32', NULL, 'NO', '0'),
        ('series', 'is_published', 'tinyint', NULL, NULL, 'NO', '0'),
        ('series', 'important_extra_data', 'json', NULL, NULL, 'YES', '0'),
        ('series', 'character_bible', 'json', NULL, NULL, 'NO', '0'),
        ('series', 'created_at', 'bigint', NULL, NULL, 'NO', '0'),
        ('series', 'updated_at', 'bigint', NULL, NULL, 'NO', '0'),
        ('user', 'user_id', 'varchar', '36', NULL, 'NO', '0'),
        ('user', 'email', 'varchar', '254', NULL, 'YES', '0'),
        ('user', 'is_guest', 'tinyint', NULL, NULL, 'NO', '0'),
        ('user', 'status', 'varchar', '16', NULL, 'NO', '0'),
        ('user', 'auth_version', 'int', NULL, NULL, 'NO', '0'),
        ('user', 'role', 'varchar', '16', NULL, 'NO', '0'),
        ('user', 'created_at', 'bigint', NULL, NULL, 'NO', '0'),
        ('user', 'updated_at', 'bigint', NULL, NULL, 'NO', '0'),
        ('agent_message', 'message_id', 'varchar', '36', NULL, 'NO', '0'),
        ('agent_message', 'conversation_id', 'varchar', '36', NULL, 'NO', '0'),
        ('agent_message', 'sequence_no', 'int', NULL, NULL, 'NO', '0'),
        ('agent_message', 'role', 'varchar', '16', NULL, 'NO', '0'),
        ('agent_message', 'status', 'varchar', '16', NULL, 'NO', '0'),
        ('agent_message', 'phase', 'varchar', '16', NULL, 'YES', '0'),
        ('agent_message', 'version', 'int', NULL, NULL, 'NO', '0'),
        ('agent_message', 'reference', 'json', NULL, NULL, 'YES', '0'),
        ('agent_message', 'extra_context', 'json', NULL, NULL, 'YES', '0'),
        ('agent_message', 'detail', 'json', NULL, NULL, 'YES', '0'),
        ('agent_message', 'content', 'longtext', NULL, NULL, 'YES', '0'),
        ('agent_message', 'series_ids', 'json', NULL, NULL, 'NO', '0'),
        ('agent_message', 'model_name', 'varchar', '255', NULL, 'YES', '0'),
        ('agent_message', 'requested_top_k', 'int', NULL, NULL, 'YES', '0'),
        ('agent_message', 'requested_run_options', 'json', NULL, NULL, 'YES', '0'),
        ('agent_message', 'requested_series_ids', 'json', NULL, NULL, 'YES', '0'),
        ('agent_message', 'search_result', 'json', NULL, NULL, 'YES', '0'),
        ('agent_message', 'last_error', 'longtext', NULL, NULL, 'YES', '0'),
        ('agent_message', 'last_error_code', 'varchar', '64', NULL, 'YES', '0'),
        ('agent_message', 'last_error_message', 'longtext', NULL, NULL, 'YES', '0'),
        ('agent_message', 'created_at', 'bigint', NULL, NULL, 'NO', '0'),
        ('agent_message', 'updated_at', 'bigint', NULL, NULL, 'NO', '0'),
        ('tenant', 'tenant_id', 'varchar', '64', NULL, 'NO', '0'),
        ('tenant', 'owner_user_id', 'varchar', '36', NULL, 'YES', '0'),
        ('tenant', 'type', 'varchar', '16', NULL, 'NO', '0'),
        ('tenant', 'status', 'varchar', '16', NULL, 'NO', '0'),
        ('tenant', 'name', 'varchar', '255', NULL, 'NO', '0'),
        ('tenant', 'created_at', 'bigint', NULL, NULL, 'NO', '0'),
        ('tenant', 'updated_at', 'bigint', NULL, NULL, 'NO', '0'),
        ('video', 'video_id', 'varchar', '36', NULL, 'NO', '0'),
        ('video', 'series_id', 'varchar', '36', NULL, 'NO', '0'),
        ('video', 'episode_order', 'int', NULL, NULL, 'NO', '0'),
        ('video', 'upload_sequence', 'int', NULL, NULL, 'NO', '0'),
        ('video', 'original_filename', 'varchar', '1024', NULL, 'NO', '0'),
        ('video', 'original_source_uri', 'longtext', NULL, NULL, 'NO', '0'),
        ('video', 'inference_proxy_uri', 'longtext', NULL, NULL, 'YES', '0'),
        ('video', 'content_hash', 'varchar', '64', NULL, 'NO', '0'),
        ('video', 'total_bytes', 'bigint', NULL, NULL, 'NO', '0'),
        ('video', 'duration_seconds', 'bigint', NULL, NULL, 'NO', '0'),
        ('video', 'upload_status', 'varchar', '32', NULL, 'NO', '0'),
        ('video', 'parse_status', 'varchar', '32', NULL, 'NO', '0'),
        ('video', 'upload_progress', 'int', NULL, NULL, 'NO', '0'),
        ('video', 'parse_progress', 'int', NULL, NULL, 'NO', '0'),
        ('video', 'created_at', 'timestamp', NULL, '3', 'NO', '0'),
        ('video', 'updated_at', 'timestamp', NULL, '3', 'NO', '0'),
        ('series_parse_job', 'job_id', 'varchar', '36', NULL, 'NO', '0'),
        ('series_parse_job', 'created_by_user_id', 'varchar', '64', NULL, 'YES', '0'),
        ('series_parse_job', 'series_id', 'varchar', '36', NULL, 'NO', '0'),
        ('series_parse_job', 'video_id', 'varchar', '36', NULL, 'NO', '0'),
        ('series_parse_job', 'pipeline_version', 'varchar', '128', NULL, 'NO', '0'),
        ('series_parse_job', 'parse_config', 'json', NULL, NULL, 'YES', '0'),
        ('series_parse_job', 'status', 'varchar', '16', NULL, 'NO', '0'),
        ('series_parse_job', 'stage', 'varchar', '32', NULL, 'NO', '0'),
        ('series_parse_job', 'progress', 'int', NULL, NULL, 'NO', '0'),
        ('series_parse_job', 'attempt_count', 'int', NULL, NULL, 'NO', '0'),
        ('series_parse_job', 'max_attempts', 'int', NULL, NULL, 'NO', '0'),
        ('series_parse_job', 'version', 'int', NULL, NULL, 'NO', '0'),
        ('series_parse_job', 'available_at', 'bigint', NULL, NULL, 'NO', '0'),
        ('series_parse_job', 'lease_owner', 'varchar', '128', NULL, 'YES', '0'),
        ('series_parse_job', 'lease_version', 'int', NULL, NULL, 'NO', '0'),
        ('series_parse_job', 'lease_expires_at', 'bigint', NULL, NULL, 'YES', '0'),
        ('series_parse_job', 'heartbeat_at', 'bigint', NULL, NULL, 'YES', '0'),
        ('series_parse_job', 'started_at', 'bigint', NULL, NULL, 'YES', '0'),
        ('series_parse_job', 'completed_at', 'bigint', NULL, NULL, 'YES', '0'),
        ('series_parse_job', 'processing_duration_ms', 'bigint', NULL, NULL, 'NO', '0'),
        ('series_parse_job', 'processing_timing_complete', 'tinyint', NULL, NULL, 'NO', '0'),
        ('series_parse_job', 'last_error', 'longtext', NULL, NULL, 'YES', '0'),
        ('series_parse_job', 'created_at', 'bigint', NULL, NULL, 'NO', '0'),
        ('series_parse_job', 'updated_at', 'bigint', NULL, NULL, 'NO', '0'),
        ('video_chunk', 'video_chunk_id', 'varchar', '36', NULL, 'NO', '0'),
        ('video_chunk', 'video_id', 'varchar', '36', NULL, 'NO', '0'),
        ('video_chunk', 'sequence_no', 'int', NULL, NULL, 'NO', '0'),
        ('video_chunk', 'start_seconds', 'bigint', NULL, NULL, 'NO', '0'),
        ('video_chunk', 'end_seconds', 'bigint', NULL, NULL, 'NO', '0'),
        ('video_chunk', 'analysis_window_start_seconds', 'bigint', NULL, NULL, 'NO', '0'),
        ('video_chunk', 'analysis_window_end_seconds', 'bigint', NULL, NULL, 'NO', '0'),
        ('video_chunk', 'chunk_media_ref', 'longtext', NULL, NULL, 'NO', '0'),
        ('video_chunk', 'analysis_window_media_ref', 'longtext', NULL, NULL, 'NO', '0'),
        ('video_chunk', 'transcript', 'json', NULL, NULL, 'NO', '0'),
        ('video_chunk', 'baseline_visual_caption', 'longtext', NULL, NULL, 'YES', '0'),
        ('video_chunk', 'keyframe_uris', 'json', NULL, NULL, 'NO', '0'),
        ('video_chunk', 'created_at', 'timestamp', NULL, '3', 'NO', '0'),
        ('video_chunk', 'updated_at', 'timestamp', NULL, '3', 'NO', '0'),
        ('video_parse_task', 'id', 'varchar', '36', NULL, 'NO', '0'),
        ('video_parse_task', 'created_by_user_id', 'varchar', '64', NULL, 'YES', '0'),
        ('video_parse_task', 'video_id', 'varchar', '36', NULL, 'NO', '0'),
        ('video_parse_task', 'pipeline_version', 'varchar', '128', NULL, 'NO', '0'),
        ('video_parse_task', 'status', 'varchar', '16', NULL, 'NO', '0'),
        ('video_parse_task', 'parse_config', 'json', NULL, NULL, 'NO', '0'),
        ('video_parse_task', 'chunk_count', 'int', NULL, NULL, 'NO', '0'),
        ('video_parse_task', 'version', 'int', NULL, NULL, 'NO', '0'),
        ('video_parse_task', 'next_execute_time', 'timestamp', NULL, '3', 'NO', '0'),
        ('video_parse_task', 'attempt_count', 'int', NULL, NULL, 'NO', '0'),
        ('video_parse_task', 'max_attempts', 'int', NULL, NULL, 'NO', '0'),
        ('video_parse_task', 'lease_owner', 'varchar', '128', NULL, 'YES', '0'),
        ('video_parse_task', 'lease_version', 'int', NULL, NULL, 'NO', '0'),
        ('video_parse_task', 'lease_expire_time', 'timestamp', NULL, '3', 'YES', '0'),
        ('video_parse_task', 'heartbeat_time', 'timestamp', NULL, '3', 'YES', '0'),
        ('video_parse_task', 'start_time', 'timestamp', NULL, '3', 'YES', '0'),
        ('video_parse_task', 'finish_time', 'timestamp', NULL, '3', 'YES', '0'),
        ('video_parse_task', 'processing_duration_ms', 'bigint', NULL, NULL, 'NO', '0'),
        ('video_parse_task', 'processing_timing_complete', 'tinyint', NULL, NULL, 'NO', '0'),
        ('video_parse_task', 'last_error', 'json', NULL, NULL, 'YES', '0'),
        ('video_parse_task', 'is_test', 'tinyint', NULL, NULL, 'NO', '0'),
        ('video_parse_task', 'is_deleted', 'tinyint', NULL, NULL, 'NO', '0'),
        ('video_parse_task', 'detail', 'json', NULL, NULL, 'NO', '0'),
        ('video_parse_task', 'create_time', 'timestamp', NULL, '3', 'NO', '0'),
        ('video_parse_task', 'update_time', 'timestamp', NULL, '3', 'NO', '0'),
        ('chunk_parse_task', 'id', 'varchar', '36', NULL, 'NO', '0'),
        ('chunk_parse_task', 'video_parse_task_id', 'varchar', '36', NULL, 'NO', '0'),
        ('chunk_parse_task', 'video_chunk_id', 'varchar', '36', NULL, 'NO', '0'),
        ('chunk_parse_task', 'status', 'varchar', '16', NULL, 'NO', '0'),
        ('chunk_parse_task', 'next_execute_time', 'timestamp', NULL, '3', 'NO', '0'),
        ('chunk_parse_task', 'retry_count', 'int', NULL, NULL, 'NO', '0'),
        ('chunk_parse_task', 'lease_owner', 'varchar', '128', NULL, 'YES', '0'),
        ('chunk_parse_task', 'lease_version', 'int', NULL, NULL, 'NO', '0'),
        ('chunk_parse_task', 'lease_expire_time', 'timestamp', NULL, '3', 'YES', '0'),
        ('chunk_parse_task', 'start_time', 'timestamp', NULL, '3', 'YES', '0'),
        ('chunk_parse_task', 'finish_time', 'timestamp', NULL, '3', 'YES', '0'),
        ('chunk_parse_task', 'last_error', 'json', NULL, NULL, 'YES', '0'),
        ('chunk_parse_task', 'detail', 'json', NULL, NULL, 'YES', '0'),
        ('chunk_parse_task', 'create_time', 'timestamp', NULL, '3', 'NO', '0'),
        ('chunk_parse_task', 'update_time', 'timestamp', NULL, '3', 'NO', '0'),
        ('context_snapshot', 'snapshot_id', 'varchar', '36', NULL, 'NO', '0'),
        ('context_snapshot', 'engine_snapshot_id', 'varchar', '128', NULL, 'NO', '0'),
        ('context_snapshot', 'series_id', 'varchar', '36', NULL, 'NO', '0'),
        ('context_snapshot', 'video_id', 'varchar', '36', NULL, 'NO', '0'),
        ('context_snapshot', 'after_chunk_sequence_no', 'int', NULL, NULL, 'NO', '0'),
        ('context_snapshot', 'compact_text', 'longtext', NULL, NULL, 'NO', '0'),
        ('context_snapshot', 'structured_state', 'json', NULL, NULL, 'NO', '0'),
        ('context_snapshot', 'created_from_video_chunk_id', 'varchar', '36', NULL, 'NO', '0'),
        ('context_snapshot', 'created_at', 'bigint', NULL, NULL, 'NO', '0'),
        ('index_sync_intent', 'video_chunk_id', 'varchar', '36', NULL, 'NO', '0'),
        ('index_sync_intent', 'operation', 'varchar', '16', NULL, 'NO', '0'),
        ('index_sync_intent', 'source_revision', 'varchar', '64', NULL, 'NO', '0'),
        ('index_sync_intent', 'generation', 'int', NULL, NULL, 'NO', '0'),
        ('index_sync_intent', 'status', 'varchar', '16', NULL, 'NO', '0'),
        ('index_sync_intent', 'attempt_count', 'int', NULL, NULL, 'NO', '0'),
        ('index_sync_intent', 'available_at', 'bigint', NULL, NULL, 'NO', '0'),
        ('index_sync_intent', 'last_error', 'longtext', NULL, NULL, 'YES', '0'),
        ('index_sync_intent', 'synced_at', 'bigint', NULL, NULL, 'YES', '0'),
        ('index_sync_intent', 'created_at', 'bigint', NULL, NULL, 'NO', '0'),
        ('index_sync_intent', 'updated_at', 'bigint', NULL, NULL, 'NO', '0'),
        ('parse_model_call', 'call_id', 'varchar', '36', NULL, 'NO', '0'),
        ('parse_model_call', 'job_id', 'varchar', '36', NULL, 'YES', '0'),
        ('parse_model_call', 'video_parse_task_id', 'varchar', '36', NULL, 'YES', '0'),
        ('parse_model_call', 'execution_kind', 'varchar', '16', NULL, 'YES', '0'),
        ('parse_model_call', 'execution_task_id', 'varchar', '36', NULL, 'YES', '0'),
        ('parse_model_call', 'attempt_count', 'int', NULL, NULL, 'NO', '0'),
        ('parse_model_call', 'lease_version', 'int', NULL, NULL, 'NO', '0'),
        ('parse_model_call', 'tenant_id', 'varchar', '64', NULL, 'NO', '0'),
        ('parse_model_call', 'series_id', 'varchar', '36', NULL, 'NO', '0'),
        ('parse_model_call', 'video_id', 'varchar', '36', NULL, 'NO', '0'),
        ('parse_model_call', 'sequence_no', 'int', NULL, NULL, 'YES', '0'),
        ('parse_model_call', 'video_chunk_id', 'varchar', '36', NULL, 'YES', '0'),
        ('parse_model_call', 'provider', 'varchar', '128', NULL, 'NO', '0'),
        ('parse_model_call', 'model', 'varchar', '255', NULL, 'NO', '0'),
        ('parse_model_call', 'category', 'varchar', '16', NULL, 'NO', '0'),
        ('parse_model_call', 'operation', 'varchar', '128', NULL, 'NO', '0'),
        ('parse_model_call', 'started_at', 'timestamp', NULL, '3', 'NO', '0'),
        ('parse_model_call', 'request_id', 'varchar', '512', NULL, 'YES', '0'),
        ('parse_model_call', 'status', 'varchar', '16', NULL, 'NO', '0'),
        ('parse_model_call', 'tokens', 'json', NULL, NULL, 'YES', '0'),
        ('parse_model_call', 'estimated_cost_cny', 'varchar', '128', NULL, 'YES', '0'),
        ('parse_model_call', 'price_snapshot', 'json', NULL, NULL, 'YES', '0'),
        ('parse_model_call', 'created_at', 'timestamp', NULL, '3', 'NO', '0'),
        ('parse_model_call', 'updated_at', 'timestamp', NULL, '3', 'NO', '0'),
        ('parse_usage_attempt', 'job_id', 'varchar', '36', NULL, 'NO', '0'),
        ('parse_usage_attempt', 'video_parse_task_id', 'varchar', '36', NULL, 'YES', '0'),
        ('parse_usage_attempt', 'attempt_count', 'int', NULL, NULL, 'NO', '0'),
        ('parse_usage_attempt', 'lease_version', 'int', NULL, NULL, 'NO', '0'),
        ('parse_usage_attempt', 'started_at', 'timestamp', NULL, '3', 'NO', '0'),
        ('parse_usage_attempt', 'recording_complete', 'tinyint', NULL, NULL, 'NO', '0'),
        ('upload_session', 'upload_session_id', 'varchar', '36', NULL, 'NO', '0'),
        ('upload_session', 'tenant_id', 'varchar', '64', NULL, 'NO', '0'),
        ('upload_session', 'created_by_user_id', 'varchar', '64', NULL, 'YES', '0'),
        ('upload_session', 'series_id', 'varchar', '36', NULL, 'NO', '0'),
        ('upload_session', 'episode_order', 'int', NULL, NULL, 'NO', '0'),
        ('upload_session', 'upload_sequence', 'int', NULL, NULL, 'NO', '0'),
        ('upload_session', 'original_filename', 'varchar', '1024', NULL, 'NO', '0'),
        ('upload_session', 'media_type', 'varchar', '255', NULL, 'NO', '0'),
        ('upload_session', 'total_size_bytes', 'bigint', NULL, NULL, 'NO', '0'),
        ('upload_session', 'preferred_segment_size_bytes', 'int', NULL, NULL, 'NO', '0'),
        ('upload_session', 'total_segments', 'int', NULL, NULL, 'NO', '0'),
        ('upload_session', 'content_sha256', 'varchar', '64', NULL, 'YES', '0'),
        ('upload_session', 'parse_config', 'json', NULL, NULL, 'YES', '0'),
        ('upload_session', 'video_parse_config', 'json', NULL, NULL, 'YES', '0'),
        ('upload_session', 'detail', 'json', NULL, NULL, 'YES', '0'),
        ('upload_session', 'completion_stage', 'varchar', '32', NULL, 'YES', '0'),
        ('upload_session', 'version', 'int', NULL, NULL, 'NO', '0'),
        ('upload_session', 'status', 'varchar', '16', NULL, 'NO', '0'),
        ('upload_session', 'idempotency_key', 'varchar', '128', NULL, 'NO', '0'),
        ('upload_session', 'request_fingerprint', 'varchar', '64', NULL, 'YES', '0'),
        ('upload_session', 'active_slot_key', 'varchar', '96', NULL, 'YES', '0'),
        ('upload_session', 'completion_token', 'varchar', '36', NULL, 'YES', '0'),
        ('upload_session', 'completion_started_at', 'timestamp', NULL, '3', 'YES', '0'),
        ('upload_session', 'expires_at', 'timestamp', NULL, '3', 'NO', '0'),
        ('upload_session', 'completed_at', 'timestamp', NULL, '3', 'YES', '0'),
        ('upload_session', 'video_id', 'varchar', '36', NULL, 'YES', '0'),
        ('upload_session', 'last_error', 'longtext', NULL, NULL, 'YES', '0'),
        ('upload_session', 'last_error_code', 'varchar', '64', NULL, 'YES', '0'),
        ('upload_session', 'job_id', 'varchar', '36', NULL, 'YES', '0'),
        ('upload_session', 'initial_video_parse_task_id', 'varchar', '36', NULL, 'YES', '0'),
        ('upload_session', 'created_at', 'timestamp', NULL, '3', 'NO', '0'),
        ('upload_session', 'updated_at', 'timestamp', NULL, '3', 'NO', '0'),
        ('video_chunk_checkpoint', 'job_id', 'varchar', '36', NULL, 'NO', '0'),
        ('video_chunk_checkpoint', 'sequence_no', 'int', NULL, NULL, 'NO', '0'),
        ('video_chunk_checkpoint', 'video_chunk_id', 'varchar', '36', NULL, 'NO', '0'),
        ('video_chunk_checkpoint', 'stage', 'varchar', '32', NULL, 'NO', '0'),
        ('video_chunk_checkpoint', 'chunk_start_seconds', 'bigint', NULL, NULL, 'YES', '0'),
        ('video_chunk_checkpoint', 'chunk_end_seconds', 'bigint', NULL, NULL, 'YES', '0'),
        ('video_chunk_checkpoint', 'chunk_media_uri', 'longtext', NULL, NULL, 'YES', '0'),
        ('video_chunk_checkpoint', 'analysis_window_media_uri', 'longtext', NULL, NULL, 'YES', '0'),
        ('video_chunk_checkpoint', 'parse_artifact_uri', 'longtext', NULL, NULL, 'YES', '0'),
        ('video_chunk_checkpoint', 'text_embedding_artifact_uri', 'longtext', NULL, NULL, 'YES', '0'),
        ('video_chunk_checkpoint', 'vision_embedding_artifact_uri', 'longtext', NULL, NULL, 'YES', '0'),
        ('video_chunk_checkpoint', 'last_error', 'longtext', NULL, NULL, 'YES', '0'),
        ('video_chunk_checkpoint', 'updated_at', 'bigint', NULL, NULL, 'NO', '0'),
        ('video_chunk_embedding', 'video_chunk_id', 'varchar', '36', NULL, 'NO', '0'),
        ('video_chunk_embedding', 'text_input', 'longtext', NULL, NULL, 'NO', '0'),
        ('video_chunk_embedding', 'text_vector', 'json', NULL, NULL, 'YES', '0'),
        ('video_chunk_embedding', 'vision_vector', 'json', NULL, NULL, 'YES', '0'),
        ('video_chunk_embedding', 'text_embedding_metadata', 'json', NULL, NULL, 'YES', '0'),
        ('video_chunk_embedding', 'vision_embedding_metadata', 'json', NULL, NULL, 'YES', '0'),
        ('video_chunk_embedding', 'created_at', 'bigint', NULL, NULL, 'NO', '0'),
        ('video_chunk_embedding', 'updated_at', 'bigint', NULL, NULL, 'NO', '0'),
        ('video_chunk_info', 'video_chunk_id', 'varchar', '36', NULL, 'NO', '0'),
        ('video_chunk_info', 'all_time_events', 'json', NULL, NULL, 'NO', '0'),
        ('video_chunk_info', 'all_time_event_text', 'longtext', NULL, NULL, 'NO', '0'),
        ('video_chunk_info', 'video_compact', 'longtext', NULL, NULL, 'NO', '0'),
        ('video_chunk_info', 'context_before_compact', 'longtext', NULL, NULL, 'NO', '0'),
        ('video_chunk_info', 'context_after_compact', 'longtext', NULL, NULL, 'NO', '0'),
        ('video_chunk_info', 'window_summary', 'longtext', NULL, NULL, 'NO', '0'),
        ('video_chunk_info', 'model_provider', 'varchar', '64', NULL, 'YES', '0'),
        ('video_chunk_info', 'model_name', 'varchar', '255', NULL, 'YES', '0'),
        ('video_chunk_info', 'pipeline_version', 'varchar', '128', NULL, 'NO', '0'),
        ('video_chunk_info', 'is_fallback', 'tinyint', NULL, NULL, 'NO', '0'),
        ('video_chunk_info', 'parse_provenance', 'json', NULL, NULL, 'YES', '0'),
        ('video_chunk_info', 'parsed_at', 'bigint', NULL, NULL, 'NO', '0'),
        ('video_parse_usage_attempt', 'attempt_id', 'varchar', '36', NULL, 'NO', '0'),
        ('video_parse_usage_attempt', 'video_parse_task_id', 'varchar', '36', NULL, 'NO', '0'),
        ('video_parse_usage_attempt', 'execution_kind', 'varchar', '16', NULL, 'NO', '0'),
        ('video_parse_usage_attempt', 'execution_task_id', 'varchar', '36', NULL, 'NO', '0'),
        ('video_parse_usage_attempt', 'attempt_count', 'int', NULL, NULL, 'NO', '0'),
        ('video_parse_usage_attempt', 'lease_version', 'int', NULL, NULL, 'NO', '0'),
        ('video_parse_usage_attempt', 'started_at', 'timestamp', NULL, '3', 'NO', '0'),
        ('video_parse_usage_attempt', 'recording_complete', 'tinyint', NULL, NULL, 'NO', '0'),
        ('chunk_parse_content', 'id', 'varchar', '36', NULL, 'NO', '0'),
        ('chunk_parse_content', 'chunk_parse_task_id', 'varchar', '36', NULL, 'NO', '0'),
        ('chunk_parse_content', 'content', 'json', NULL, NULL, 'YES', '0'),
        ('chunk_parse_content', 'version', 'int', NULL, NULL, 'NO', '0'),
        ('chunk_parse_content', 'detail', 'json', NULL, NULL, 'YES', '0'),
        ('chunk_parse_content', 'create_time', 'timestamp', NULL, '3', 'NO', '0'),
        ('chunk_parse_content', 'update_time', 'timestamp', NULL, '3', 'NO', '0'),
        ('interval_parse_task', 'id', 'varchar', '36', NULL, 'NO', '0'),
        ('interval_parse_task', 'chunk_parse_task_id', 'varchar', '36', NULL, 'NO', '0'),
        ('interval_parse_task', 'video_parse_task_id', 'varchar', '36', NULL, 'NO', '0'),
        ('interval_parse_task', 'chunk_parse_task_ids', 'json', NULL, NULL, 'NO', '0'),
        ('interval_parse_task', 'start_sequence_no', 'int', NULL, NULL, 'NO', '0'),
        ('interval_parse_task', 'chunk_count', 'int', NULL, NULL, 'NO', '0'),
        ('interval_parse_task', 'ready_mask', 'bigint', NULL, NULL, 'NO', '0'),
        ('interval_parse_task', 'status', 'varchar', '16', NULL, 'NO', '0'),
        ('interval_parse_task', 'next_execute_time', 'timestamp', NULL, '3', 'NO', '0'),
        ('interval_parse_task', 'retry_count', 'int', NULL, NULL, 'NO', '0'),
        ('interval_parse_task', 'lease_owner', 'varchar', '128', NULL, 'YES', '0'),
        ('interval_parse_task', 'lease_version', 'int', NULL, NULL, 'NO', '0'),
        ('interval_parse_task', 'lease_expire_time', 'timestamp', NULL, '3', 'YES', '0'),
        ('interval_parse_task', 'start_time', 'timestamp', NULL, '3', 'YES', '0'),
        ('interval_parse_task', 'finish_time', 'timestamp', NULL, '3', 'YES', '0'),
        ('interval_parse_task', 'last_error', 'json', NULL, NULL, 'YES', '0'),
        ('interval_parse_task', 'create_time', 'timestamp', NULL, '3', 'NO', '0'),
        ('interval_parse_task', 'update_time', 'timestamp', NULL, '3', 'NO', '0'),
        ('upload_segment', 'upload_session_id', 'varchar', '36', NULL, 'NO', '0'),
        ('upload_segment', 'segment_number', 'int', NULL, NULL, 'NO', '0'),
        ('upload_segment', 'start_byte', 'bigint', NULL, NULL, 'NO', '0'),
        ('upload_segment', 'end_byte_exclusive', 'bigint', NULL, NULL, 'NO', '0'),
        ('upload_segment', 'actual_size_bytes', 'int', NULL, NULL, 'NO', '0'),
        ('upload_segment', 'checksum_sha256', 'varchar', '64', NULL, 'NO', '0'),
        ('upload_segment', 'artifact_uri', 'longtext', NULL, NULL, 'NO', '0'),
        ('upload_segment', 'created_at', 'timestamp', NULL, '3', 'NO', '0'),
        ('interval_parse_content', 'interval_parse_task_id', 'varchar', '36', NULL, 'NO', '0'),
        ('interval_parse_content', 'content', 'json', NULL, NULL, 'NO', '0'),
        ('interval_parse_content', 'detail', 'json', NULL, NULL, 'YES', '0'),
        ('interval_parse_content', 'create_time', 'timestamp', NULL, '3', 'NO', '0'),
        ('interval_parse_content', 'update_time', 'timestamp', NULL, '3', 'NO', '0');
    INSERT INTO _bm_target_cols VALUES
        ('agent_conversation', 'id', 'varchar', '36', NULL, 'NO', '0'),
        ('agent_conversation', 'tenant_id', 'varchar', '64', NULL, 'NO', '0'),
        ('agent_conversation', 'owner_id', 'varchar', '64', NULL, 'NO', '0'),
        ('agent_conversation', 'title', 'varchar', '255', NULL, 'NO', '0'),
        ('agent_conversation', 'title_is_auto', 'tinyint', NULL, NULL, 'NO', '0'),
        ('agent_conversation', 'idempotency_key', 'varchar', '128', NULL, 'NO', '0'),
        ('agent_conversation', 'next_message_sequence', 'int', NULL, NULL, 'NO', '0'),
        ('agent_conversation', 'last_message_time', 'bigint', NULL, NULL, 'YES', '0'),
        ('agent_conversation', 'create_time', 'bigint', NULL, NULL, 'YES', '0'),
        ('agent_conversation', 'create_date', 'datetime', NULL, '3', 'YES', '0'),
        ('agent_conversation', 'update_time', 'bigint', NULL, NULL, 'YES', '0'),
        ('agent_conversation', 'update_date', 'datetime', NULL, '3', 'YES', '0'),
        ('model_usage', 'id', 'varchar', '36', NULL, 'NO', '0'),
        ('model_usage', 'tenant_id', 'varchar', '64', NULL, 'NO', '0'),
        ('model_usage', 'user_id', 'varchar', '64', NULL, 'YES', '0'),
        ('model_usage', 'task_type', 'varchar', '16', NULL, 'NO', '0'),
        ('model_usage', 'run_id', 'varchar', '256', NULL, 'NO', '0'),
        ('model_usage', 'start_time', 'timestamp', NULL, '3', 'NO', '0'),
        ('model_usage', 'usage', 'json', NULL, NULL, 'NO', '0'),
        ('model_usage', 'metadata', 'json', NULL, NULL, 'NO', '0'),
        ('model_usage', 'create_time', 'bigint', NULL, NULL, 'YES', '0'),
        ('model_usage', 'create_date', 'datetime', NULL, '3', 'YES', '0'),
        ('model_usage', 'update_time', 'bigint', NULL, NULL, 'YES', '0'),
        ('model_usage', 'update_date', 'datetime', NULL, '3', 'YES', '0'),
        ('series', 'id', 'varchar', '36', NULL, 'NO', '0'),
        ('series', 'tenant_id', 'varchar', '64', NULL, 'NO', '0'),
        ('series', 'name', 'varchar', '255', NULL, 'NO', '0'),
        ('series', 'description', 'longtext', NULL, NULL, 'YES', '0'),
        ('series', 'transcript_language_hint', 'varchar', '64', NULL, 'YES', '0'),
        ('series', 'status', 'varchar', '32', NULL, 'NO', '0'),
        ('series', 'is_published', 'tinyint', NULL, NULL, 'NO', '0'),
        ('series', 'important_extra_data', 'json', NULL, NULL, 'YES', '0'),
        ('series', 'character_bible', 'json', NULL, NULL, 'NO', '0'),
        ('series', 'create_time', 'bigint', NULL, NULL, 'YES', '0'),
        ('series', 'create_date', 'datetime', NULL, '3', 'YES', '0'),
        ('series', 'update_time', 'bigint', NULL, NULL, 'YES', '0'),
        ('series', 'update_date', 'datetime', NULL, '3', 'YES', '0'),
        ('user', 'id', 'varchar', '36', NULL, 'NO', '0'),
        ('user', 'email', 'varchar', '254', NULL, 'YES', '0'),
        ('user', 'is_anonymous', 'tinyint', NULL, NULL, 'NO', '0'),
        ('user', 'status', 'varchar', '16', NULL, 'NO', '0'),
        ('user', 'auth_version', 'int', NULL, NULL, 'NO', '0'),
        ('user', 'is_superuser', 'tinyint', NULL, NULL, 'NO', '0'),
        ('user', 'nickname', 'varchar', '100', NULL, 'NO', '0'),
        ('user', 'avatar', 'text', NULL, NULL, 'YES', '0'),
        ('user', 'language', 'varchar', '32', NULL, 'YES', '0'),
        ('user', 'color_schema', 'varchar', '32', NULL, 'YES', '0'),
        ('user', 'timezone', 'varchar', '64', NULL, 'YES', '0'),
        ('user', 'last_login_time', 'datetime', NULL, '3', 'YES', '0'),
        ('user', 'login_channel', 'varchar', '32', NULL, 'YES', '0'),
        ('user', 'create_time', 'bigint', NULL, NULL, 'YES', '0'),
        ('user', 'create_date', 'datetime', NULL, '3', 'YES', '0'),
        ('user', 'update_time', 'bigint', NULL, NULL, 'YES', '0'),
        ('user', 'update_date', 'datetime', NULL, '3', 'YES', '0'),
        ('agent_message', 'id', 'varchar', '36', NULL, 'NO', '0'),
        ('agent_message', 'conversation_id', 'varchar', '36', NULL, 'NO', '0'),
        ('agent_message', 'sequence_no', 'int', NULL, NULL, 'NO', '0'),
        ('agent_message', 'role', 'varchar', '16', NULL, 'NO', '0'),
        ('agent_message', 'status', 'varchar', '16', NULL, 'NO', '0'),
        ('agent_message', 'phase', 'varchar', '16', NULL, 'YES', '0'),
        ('agent_message', 'version', 'int', NULL, NULL, 'NO', '0'),
        ('agent_message', 'reference', 'json', NULL, NULL, 'YES', '0'),
        ('agent_message', 'extra_context', 'json', NULL, NULL, 'YES', '0'),
        ('agent_message', 'detail', 'json', NULL, NULL, 'YES', '0'),
        ('agent_message', 'content', 'longtext', NULL, NULL, 'YES', '0'),
        ('agent_message', 'series_ids', 'json', NULL, NULL, 'NO', '0'),
        ('agent_message', 'model', 'varchar', '255', NULL, 'YES', '0'),
        ('agent_message', 'requested_top_k', 'int', NULL, NULL, 'YES', '0'),
        ('agent_message', 'requested_run_options', 'json', NULL, NULL, 'YES', '0'),
        ('agent_message', 'requested_series_ids', 'json', NULL, NULL, 'YES', '0'),
        ('agent_message', 'search_result', 'json', NULL, NULL, 'YES', '0'),
        ('agent_message', 'last_error', 'longtext', NULL, NULL, 'YES', '0'),
        ('agent_message', 'last_error_code', 'varchar', '64', NULL, 'YES', '0'),
        ('agent_message', 'last_error_message', 'longtext', NULL, NULL, 'YES', '0'),
        ('agent_message', 'create_time', 'bigint', NULL, NULL, 'YES', '0'),
        ('agent_message', 'create_date', 'datetime', NULL, '3', 'YES', '0'),
        ('agent_message', 'update_time', 'bigint', NULL, NULL, 'YES', '0'),
        ('agent_message', 'update_date', 'datetime', NULL, '3', 'YES', '0'),
        ('tenant', 'id', 'varchar', '64', NULL, 'NO', '0'),
        ('tenant', 'owner_id', 'varchar', '36', NULL, 'YES', '0'),
        ('tenant', 'type', 'varchar', '16', NULL, 'NO', '0'),
        ('tenant', 'status', 'varchar', '16', NULL, 'NO', '0'),
        ('tenant', 'name', 'varchar', '255', NULL, 'NO', '0'),
        ('tenant', 'description', 'text', NULL, NULL, 'YES', '0'),
        ('tenant', 'avatar', 'text', NULL, NULL, 'YES', '0'),
        ('tenant', 'create_time', 'bigint', NULL, NULL, 'YES', '0'),
        ('tenant', 'create_date', 'datetime', NULL, '3', 'YES', '0'),
        ('tenant', 'update_time', 'bigint', NULL, NULL, 'YES', '0'),
        ('tenant', 'update_date', 'datetime', NULL, '3', 'YES', '0'),
        ('video', 'id', 'varchar', '36', NULL, 'NO', '0'),
        ('video', 'series_id', 'varchar', '36', NULL, 'NO', '0'),
        ('video', 'episode_order', 'int', NULL, NULL, 'NO', '0'),
        ('video', 'upload_sequence', 'int', NULL, NULL, 'NO', '0'),
        ('video', 'filename', 'varchar', '1024', NULL, 'NO', '0'),
        ('video', 'source_uri', 'longtext', NULL, NULL, 'NO', '0'),
        ('video', 'proxy_uri', 'longtext', NULL, NULL, 'YES', '0'),
        ('video', 'hash', 'varchar', '64', NULL, 'NO', '0'),
        ('video', 'size_bytes', 'bigint', NULL, NULL, 'NO', '0'),
        ('video', 'duration_ms', 'bigint', NULL, NULL, 'NO', '0'),
        ('video', 'upload_status', 'varchar', '32', NULL, 'NO', '0'),
        ('video', 'parse_status', 'varchar', '32', NULL, 'NO', '0'),
        ('video', 'upload_progress', 'int', NULL, NULL, 'NO', '0'),
        ('video', 'parse_progress', 'int', NULL, NULL, 'NO', '0'),
        ('video', 'create_time', 'bigint', NULL, NULL, 'YES', '0'),
        ('video', 'create_date', 'datetime', NULL, '3', 'YES', '0'),
        ('video', 'update_time', 'bigint', NULL, NULL, 'YES', '0'),
        ('video', 'update_date', 'datetime', NULL, '3', 'YES', '0'),
        ('series_parse_job', 'id', 'varchar', '36', NULL, 'NO', '0'),
        ('series_parse_job', 'creator_id', 'varchar', '64', NULL, 'YES', '0'),
        ('series_parse_job', 'series_id', 'varchar', '36', NULL, 'NO', '0'),
        ('series_parse_job', 'video_id', 'varchar', '36', NULL, 'NO', '0'),
        ('series_parse_job', 'pipeline_version', 'varchar', '128', NULL, 'NO', '0'),
        ('series_parse_job', 'parse_config', 'json', NULL, NULL, 'YES', '0'),
        ('series_parse_job', 'status', 'varchar', '16', NULL, 'NO', '0'),
        ('series_parse_job', 'stage', 'varchar', '32', NULL, 'NO', '0'),
        ('series_parse_job', 'progress', 'int', NULL, NULL, 'NO', '0'),
        ('series_parse_job', 'attempt_count', 'int', NULL, NULL, 'NO', '0'),
        ('series_parse_job', 'max_attempts', 'int', NULL, NULL, 'NO', '0'),
        ('series_parse_job', 'version', 'int', NULL, NULL, 'NO', '0'),
        ('series_parse_job', 'next_execute_time', 'bigint', NULL, NULL, 'NO', '0'),
        ('series_parse_job', 'lease_owner', 'varchar', '128', NULL, 'YES', '0'),
        ('series_parse_job', 'lease_version', 'int', NULL, NULL, 'NO', '0'),
        ('series_parse_job', 'lease_expire_time', 'bigint', NULL, NULL, 'YES', '0'),
        ('series_parse_job', 'heartbeat_time', 'bigint', NULL, NULL, 'YES', '0'),
        ('series_parse_job', 'start_time', 'bigint', NULL, NULL, 'YES', '0'),
        ('series_parse_job', 'finish_time', 'bigint', NULL, NULL, 'YES', '0'),
        ('series_parse_job', 'processing_duration_ms', 'bigint', NULL, NULL, 'NO', '0'),
        ('series_parse_job', 'processing_timing_complete', 'tinyint', NULL, NULL, 'NO', '0'),
        ('series_parse_job', 'last_error', 'longtext', NULL, NULL, 'YES', '0'),
        ('series_parse_job', 'create_time', 'bigint', NULL, NULL, 'YES', '0'),
        ('series_parse_job', 'create_date', 'datetime', NULL, '3', 'YES', '0'),
        ('series_parse_job', 'update_time', 'bigint', NULL, NULL, 'YES', '0'),
        ('series_parse_job', 'update_date', 'datetime', NULL, '3', 'YES', '0'),
        ('video_chunk', 'id', 'varchar', '36', NULL, 'NO', '0'),
        ('video_chunk', 'video_id', 'varchar', '36', NULL, 'NO', '0'),
        ('video_chunk', 'sequence_no', 'int', NULL, NULL, 'NO', '0'),
        ('video_chunk', 'start_ms', 'bigint', NULL, NULL, 'NO', '0'),
        ('video_chunk', 'end_ms', 'bigint', NULL, NULL, 'NO', '0'),
        ('video_chunk', 'window_start_ms', 'bigint', NULL, NULL, 'NO', '0'),
        ('video_chunk', 'window_end_ms', 'bigint', NULL, NULL, 'NO', '0'),
        ('video_chunk', 'chunk_media_ref', 'longtext', NULL, NULL, 'NO', '0'),
        ('video_chunk', 'analysis_window_media_ref', 'longtext', NULL, NULL, 'NO', '0'),
        ('video_chunk', 'transcript', 'json', NULL, NULL, 'NO', '0'),
        ('video_chunk', 'baseline_visual_caption', 'longtext', NULL, NULL, 'YES', '0'),
        ('video_chunk', 'keyframe_uris', 'json', NULL, NULL, 'NO', '0'),
        ('video_chunk', 'create_time', 'bigint', NULL, NULL, 'YES', '0'),
        ('video_chunk', 'create_date', 'datetime', NULL, '3', 'YES', '0'),
        ('video_chunk', 'update_time', 'bigint', NULL, NULL, 'YES', '0'),
        ('video_chunk', 'update_date', 'datetime', NULL, '3', 'YES', '0'),
        ('video_parse_task', 'id', 'varchar', '36', NULL, 'NO', '0'),
        ('video_parse_task', 'creator_id', 'varchar', '64', NULL, 'YES', '0'),
        ('video_parse_task', 'video_id', 'varchar', '36', NULL, 'NO', '0'),
        ('video_parse_task', 'pipeline_version', 'varchar', '128', NULL, 'NO', '0'),
        ('video_parse_task', 'status', 'varchar', '16', NULL, 'NO', '0'),
        ('video_parse_task', 'parse_config', 'json', NULL, NULL, 'NO', '0'),
        ('video_parse_task', 'chunk_count', 'int', NULL, NULL, 'NO', '0'),
        ('video_parse_task', 'version', 'int', NULL, NULL, 'NO', '0'),
        ('video_parse_task', 'next_execute_time', 'timestamp', NULL, '3', 'NO', '0'),
        ('video_parse_task', 'attempt_count', 'int', NULL, NULL, 'NO', '0'),
        ('video_parse_task', 'max_attempts', 'int', NULL, NULL, 'NO', '0'),
        ('video_parse_task', 'lease_owner', 'varchar', '128', NULL, 'YES', '0'),
        ('video_parse_task', 'lease_version', 'int', NULL, NULL, 'NO', '0'),
        ('video_parse_task', 'lease_expire_time', 'timestamp', NULL, '3', 'YES', '0'),
        ('video_parse_task', 'heartbeat_time', 'timestamp', NULL, '3', 'YES', '0'),
        ('video_parse_task', 'start_time', 'timestamp', NULL, '3', 'YES', '0'),
        ('video_parse_task', 'finish_time', 'timestamp', NULL, '3', 'YES', '0'),
        ('video_parse_task', 'processing_duration_ms', 'bigint', NULL, NULL, 'NO', '0'),
        ('video_parse_task', 'processing_timing_complete', 'tinyint', NULL, NULL, 'NO', '0'),
        ('video_parse_task', 'last_error', 'json', NULL, NULL, 'YES', '0'),
        ('video_parse_task', 'is_test', 'tinyint', NULL, NULL, 'NO', '0'),
        ('video_parse_task', 'is_deleted', 'tinyint', NULL, NULL, 'NO', '0'),
        ('video_parse_task', 'detail', 'json', NULL, NULL, 'NO', '0'),
        ('video_parse_task', 'create_time', 'bigint', NULL, NULL, 'YES', '0'),
        ('video_parse_task', 'create_date', 'datetime', NULL, '3', 'YES', '0'),
        ('video_parse_task', 'update_time', 'bigint', NULL, NULL, 'YES', '0'),
        ('video_parse_task', 'update_date', 'datetime', NULL, '3', 'YES', '0'),
        ('chunk_parse_task', 'id', 'varchar', '36', NULL, 'NO', '0'),
        ('chunk_parse_task', 'video_task_id', 'varchar', '36', NULL, 'NO', '0'),
        ('chunk_parse_task', 'chunk_id', 'varchar', '36', NULL, 'NO', '0'),
        ('chunk_parse_task', 'status', 'varchar', '16', NULL, 'NO', '0'),
        ('chunk_parse_task', 'next_execute_time', 'timestamp', NULL, '3', 'NO', '0'),
        ('chunk_parse_task', 'retry_count', 'int', NULL, NULL, 'NO', '0'),
        ('chunk_parse_task', 'lease_owner', 'varchar', '128', NULL, 'YES', '0'),
        ('chunk_parse_task', 'lease_version', 'int', NULL, NULL, 'NO', '0'),
        ('chunk_parse_task', 'lease_expire_time', 'timestamp', NULL, '3', 'YES', '0'),
        ('chunk_parse_task', 'start_time', 'timestamp', NULL, '3', 'YES', '0'),
        ('chunk_parse_task', 'finish_time', 'timestamp', NULL, '3', 'YES', '0'),
        ('chunk_parse_task', 'last_error', 'json', NULL, NULL, 'YES', '0'),
        ('chunk_parse_task', 'detail', 'json', NULL, NULL, 'YES', '0'),
        ('chunk_parse_task', 'create_time', 'bigint', NULL, NULL, 'YES', '0'),
        ('chunk_parse_task', 'create_date', 'datetime', NULL, '3', 'YES', '0'),
        ('chunk_parse_task', 'update_time', 'bigint', NULL, NULL, 'YES', '0'),
        ('chunk_parse_task', 'update_date', 'datetime', NULL, '3', 'YES', '0'),
        ('context_snapshot', 'id', 'varchar', '36', NULL, 'NO', '0'),
        ('context_snapshot', 'engine_snapshot_id', 'varchar', '128', NULL, 'NO', '0'),
        ('context_snapshot', 'series_id', 'varchar', '36', NULL, 'NO', '0'),
        ('context_snapshot', 'video_id', 'varchar', '36', NULL, 'NO', '0'),
        ('context_snapshot', 'after_chunk_sequence_no', 'int', NULL, NULL, 'NO', '0'),
        ('context_snapshot', 'compact_text', 'longtext', NULL, NULL, 'NO', '0'),
        ('context_snapshot', 'structured_state', 'json', NULL, NULL, 'NO', '0'),
        ('context_snapshot', 'source_chunk_id', 'varchar', '36', NULL, 'NO', '0'),
        ('context_snapshot', 'create_time', 'bigint', NULL, NULL, 'YES', '0'),
        ('context_snapshot', 'create_date', 'datetime', NULL, '3', 'YES', '0'),
        ('context_snapshot', 'update_time', 'bigint', NULL, NULL, 'YES', '0'),
        ('context_snapshot', 'update_date', 'datetime', NULL, '3', 'YES', '0'),
        ('index_sync_intent', 'chunk_id', 'varchar', '36', NULL, 'NO', '0'),
        ('index_sync_intent', 'operation', 'varchar', '16', NULL, 'NO', '0'),
        ('index_sync_intent', 'source_revision', 'varchar', '64', NULL, 'NO', '0'),
        ('index_sync_intent', 'generation', 'int', NULL, NULL, 'NO', '0'),
        ('index_sync_intent', 'status', 'varchar', '16', NULL, 'NO', '0'),
        ('index_sync_intent', 'attempt_count', 'int', NULL, NULL, 'NO', '0'),
        ('index_sync_intent', 'next_execute_time', 'bigint', NULL, NULL, 'NO', '0'),
        ('index_sync_intent', 'last_error', 'longtext', NULL, NULL, 'YES', '0'),
        ('index_sync_intent', 'sync_time', 'bigint', NULL, NULL, 'YES', '0'),
        ('index_sync_intent', 'create_time', 'bigint', NULL, NULL, 'YES', '0'),
        ('index_sync_intent', 'create_date', 'datetime', NULL, '3', 'YES', '0'),
        ('index_sync_intent', 'update_time', 'bigint', NULL, NULL, 'YES', '0'),
        ('index_sync_intent', 'update_date', 'datetime', NULL, '3', 'YES', '0'),
        ('parse_model_call', 'id', 'varchar', '36', NULL, 'NO', '0'),
        ('parse_model_call', 'job_id', 'varchar', '36', NULL, 'YES', '0'),
        ('parse_model_call', 'video_task_id', 'varchar', '36', NULL, 'YES', '0'),
        ('parse_model_call', 'execution_kind', 'varchar', '16', NULL, 'YES', '0'),
        ('parse_model_call', 'execution_task_id', 'varchar', '36', NULL, 'YES', '0'),
        ('parse_model_call', 'attempt_count', 'int', NULL, NULL, 'NO', '0'),
        ('parse_model_call', 'lease_version', 'int', NULL, NULL, 'NO', '0'),
        ('parse_model_call', 'tenant_id', 'varchar', '64', NULL, 'NO', '0'),
        ('parse_model_call', 'series_id', 'varchar', '36', NULL, 'NO', '0'),
        ('parse_model_call', 'video_id', 'varchar', '36', NULL, 'NO', '0'),
        ('parse_model_call', 'sequence_no', 'int', NULL, NULL, 'YES', '0'),
        ('parse_model_call', 'chunk_id', 'varchar', '36', NULL, 'YES', '0'),
        ('parse_model_call', 'provider', 'varchar', '128', NULL, 'NO', '0'),
        ('parse_model_call', 'model', 'varchar', '255', NULL, 'NO', '0'),
        ('parse_model_call', 'category', 'varchar', '16', NULL, 'NO', '0'),
        ('parse_model_call', 'operation', 'varchar', '128', NULL, 'NO', '0'),
        ('parse_model_call', 'start_time', 'timestamp', NULL, '3', 'NO', '0'),
        ('parse_model_call', 'request_id', 'varchar', '512', NULL, 'YES', '0'),
        ('parse_model_call', 'status', 'varchar', '16', NULL, 'NO', '0'),
        ('parse_model_call', 'tokens', 'json', NULL, NULL, 'YES', '0'),
        ('parse_model_call', 'estimated_cost_cny', 'varchar', '128', NULL, 'YES', '0'),
        ('parse_model_call', 'price_snapshot', 'json', NULL, NULL, 'YES', '0'),
        ('parse_model_call', 'create_time', 'bigint', NULL, NULL, 'YES', '0'),
        ('parse_model_call', 'create_date', 'datetime', NULL, '3', 'YES', '0'),
        ('parse_model_call', 'update_time', 'bigint', NULL, NULL, 'YES', '0'),
        ('parse_model_call', 'update_date', 'datetime', NULL, '3', 'YES', '0'),
        ('parse_usage_attempt', 'job_id', 'varchar', '36', NULL, 'NO', '0'),
        ('parse_usage_attempt', 'video_task_id', 'varchar', '36', NULL, 'YES', '0'),
        ('parse_usage_attempt', 'attempt_count', 'int', NULL, NULL, 'NO', '0'),
        ('parse_usage_attempt', 'lease_version', 'int', NULL, NULL, 'NO', '0'),
        ('parse_usage_attempt', 'start_time', 'timestamp', NULL, '3', 'NO', '0'),
        ('parse_usage_attempt', 'recording_complete', 'tinyint', NULL, NULL, 'NO', '0'),
        ('parse_usage_attempt', 'create_time', 'bigint', NULL, NULL, 'YES', '0'),
        ('parse_usage_attempt', 'create_date', 'datetime', NULL, '3', 'YES', '0'),
        ('parse_usage_attempt', 'update_time', 'bigint', NULL, NULL, 'YES', '0'),
        ('parse_usage_attempt', 'update_date', 'datetime', NULL, '3', 'YES', '0'),
        ('upload_session', 'id', 'varchar', '36', NULL, 'NO', '0'),
        ('upload_session', 'tenant_id', 'varchar', '64', NULL, 'NO', '0'),
        ('upload_session', 'creator_id', 'varchar', '64', NULL, 'YES', '0'),
        ('upload_session', 'series_id', 'varchar', '36', NULL, 'NO', '0'),
        ('upload_session', 'episode_order', 'int', NULL, NULL, 'NO', '0'),
        ('upload_session', 'upload_sequence', 'int', NULL, NULL, 'NO', '0'),
        ('upload_session', 'filename', 'varchar', '1024', NULL, 'NO', '0'),
        ('upload_session', 'media_type', 'varchar', '255', NULL, 'NO', '0'),
        ('upload_session', 'size_bytes', 'bigint', NULL, NULL, 'NO', '0'),
        ('upload_session', 'segment_size_bytes', 'int', NULL, NULL, 'NO', '0'),
        ('upload_session', 'segment_count', 'int', NULL, NULL, 'NO', '0'),
        ('upload_session', 'sha256', 'varchar', '64', NULL, 'YES', '0'),
        ('upload_session', 'parse_config', 'json', NULL, NULL, 'YES', '0'),
        ('upload_session', 'video_parse_config', 'json', NULL, NULL, 'YES', '0'),
        ('upload_session', 'detail', 'json', NULL, NULL, 'YES', '0'),
        ('upload_session', 'completion_stage', 'varchar', '32', NULL, 'YES', '0'),
        ('upload_session', 'version', 'int', NULL, NULL, 'NO', '0'),
        ('upload_session', 'status', 'varchar', '16', NULL, 'NO', '0'),
        ('upload_session', 'idempotency_key', 'varchar', '128', NULL, 'NO', '0'),
        ('upload_session', 'request_fingerprint', 'varchar', '64', NULL, 'YES', '0'),
        ('upload_session', 'active_slot_key', 'varchar', '96', NULL, 'YES', '0'),
        ('upload_session', 'completion_token', 'varchar', '36', NULL, 'YES', '0'),
        ('upload_session', 'completion_start_time', 'timestamp', NULL, '3', 'YES', '0'),
        ('upload_session', 'expire_time', 'timestamp', NULL, '3', 'NO', '0'),
        ('upload_session', 'finish_time', 'timestamp', NULL, '3', 'YES', '0'),
        ('upload_session', 'video_id', 'varchar', '36', NULL, 'YES', '0'),
        ('upload_session', 'last_error', 'longtext', NULL, NULL, 'YES', '0'),
        ('upload_session', 'last_error_code', 'varchar', '64', NULL, 'YES', '0'),
        ('upload_session', 'job_id', 'varchar', '36', NULL, 'YES', '0'),
        ('upload_session', 'initial_task_id', 'varchar', '36', NULL, 'YES', '0'),
        ('upload_session', 'create_time', 'bigint', NULL, NULL, 'YES', '0'),
        ('upload_session', 'create_date', 'datetime', NULL, '3', 'YES', '0'),
        ('upload_session', 'update_time', 'bigint', NULL, NULL, 'YES', '0'),
        ('upload_session', 'update_date', 'datetime', NULL, '3', 'YES', '0'),
        ('video_chunk_checkpoint', 'job_id', 'varchar', '36', NULL, 'NO', '0'),
        ('video_chunk_checkpoint', 'sequence_no', 'int', NULL, NULL, 'NO', '0'),
        ('video_chunk_checkpoint', 'chunk_id', 'varchar', '36', NULL, 'NO', '0'),
        ('video_chunk_checkpoint', 'stage', 'varchar', '32', NULL, 'NO', '0'),
        ('video_chunk_checkpoint', 'start_ms', 'bigint', NULL, NULL, 'YES', '0'),
        ('video_chunk_checkpoint', 'end_ms', 'bigint', NULL, NULL, 'YES', '0'),
        ('video_chunk_checkpoint', 'chunk_media_uri', 'longtext', NULL, NULL, 'YES', '0'),
        ('video_chunk_checkpoint', 'analysis_window_media_uri', 'longtext', NULL, NULL, 'YES', '0'),
        ('video_chunk_checkpoint', 'parse_artifact_uri', 'longtext', NULL, NULL, 'YES', '0'),
        ('video_chunk_checkpoint', 'text_embedding_artifact_uri', 'longtext', NULL, NULL, 'YES', '0'),
        ('video_chunk_checkpoint', 'vision_embedding_artifact_uri', 'longtext', NULL, NULL, 'YES', '0'),
        ('video_chunk_checkpoint', 'last_error', 'longtext', NULL, NULL, 'YES', '0'),
        ('video_chunk_checkpoint', 'create_time', 'bigint', NULL, NULL, 'YES', '0'),
        ('video_chunk_checkpoint', 'create_date', 'datetime', NULL, '3', 'YES', '0'),
        ('video_chunk_checkpoint', 'update_time', 'bigint', NULL, NULL, 'YES', '0'),
        ('video_chunk_checkpoint', 'update_date', 'datetime', NULL, '3', 'YES', '0'),
        ('video_chunk_embedding', 'chunk_id', 'varchar', '36', NULL, 'NO', '0'),
        ('video_chunk_embedding', 'text_input', 'longtext', NULL, NULL, 'NO', '0'),
        ('video_chunk_embedding', 'text_vector', 'json', NULL, NULL, 'YES', '0'),
        ('video_chunk_embedding', 'vision_vector', 'json', NULL, NULL, 'YES', '0'),
        ('video_chunk_embedding', 'text_embedding_metadata', 'json', NULL, NULL, 'YES', '0'),
        ('video_chunk_embedding', 'vision_embedding_metadata', 'json', NULL, NULL, 'YES', '0'),
        ('video_chunk_embedding', 'create_time', 'bigint', NULL, NULL, 'YES', '0'),
        ('video_chunk_embedding', 'create_date', 'datetime', NULL, '3', 'YES', '0'),
        ('video_chunk_embedding', 'update_time', 'bigint', NULL, NULL, 'YES', '0'),
        ('video_chunk_embedding', 'update_date', 'datetime', NULL, '3', 'YES', '0'),
        ('video_chunk_info', 'chunk_id', 'varchar', '36', NULL, 'NO', '0'),
        ('video_chunk_info', 'all_time_events', 'json', NULL, NULL, 'NO', '0'),
        ('video_chunk_info', 'all_time_event_text', 'longtext', NULL, NULL, 'NO', '0'),
        ('video_chunk_info', 'video_compact', 'longtext', NULL, NULL, 'NO', '0'),
        ('video_chunk_info', 'context_before_compact', 'longtext', NULL, NULL, 'NO', '0'),
        ('video_chunk_info', 'context_after_compact', 'longtext', NULL, NULL, 'NO', '0'),
        ('video_chunk_info', 'window_summary', 'longtext', NULL, NULL, 'NO', '0'),
        ('video_chunk_info', 'provider', 'varchar', '64', NULL, 'YES', '0'),
        ('video_chunk_info', 'model', 'varchar', '255', NULL, 'YES', '0'),
        ('video_chunk_info', 'pipeline_version', 'varchar', '128', NULL, 'NO', '0'),
        ('video_chunk_info', 'is_fallback', 'tinyint', NULL, NULL, 'NO', '0'),
        ('video_chunk_info', 'parse_provenance', 'json', NULL, NULL, 'YES', '0'),
        ('video_chunk_info', 'parse_time', 'bigint', NULL, NULL, 'NO', '0'),
        ('video_chunk_info', 'create_time', 'bigint', NULL, NULL, 'YES', '0'),
        ('video_chunk_info', 'create_date', 'datetime', NULL, '3', 'YES', '0'),
        ('video_chunk_info', 'update_time', 'bigint', NULL, NULL, 'YES', '0'),
        ('video_chunk_info', 'update_date', 'datetime', NULL, '3', 'YES', '0'),
        ('video_parse_usage_attempt', 'id', 'varchar', '36', NULL, 'NO', '0'),
        ('video_parse_usage_attempt', 'video_task_id', 'varchar', '36', NULL, 'NO', '0'),
        ('video_parse_usage_attempt', 'execution_kind', 'varchar', '16', NULL, 'NO', '0'),
        ('video_parse_usage_attempt', 'execution_task_id', 'varchar', '36', NULL, 'NO', '0'),
        ('video_parse_usage_attempt', 'attempt_count', 'int', NULL, NULL, 'NO', '0'),
        ('video_parse_usage_attempt', 'lease_version', 'int', NULL, NULL, 'NO', '0'),
        ('video_parse_usage_attempt', 'start_time', 'timestamp', NULL, '3', 'NO', '0'),
        ('video_parse_usage_attempt', 'recording_complete', 'tinyint', NULL, NULL, 'NO', '0'),
        ('video_parse_usage_attempt', 'create_time', 'bigint', NULL, NULL, 'YES', '0'),
        ('video_parse_usage_attempt', 'create_date', 'datetime', NULL, '3', 'YES', '0'),
        ('video_parse_usage_attempt', 'update_time', 'bigint', NULL, NULL, 'YES', '0'),
        ('video_parse_usage_attempt', 'update_date', 'datetime', NULL, '3', 'YES', '0'),
        ('chunk_parse_content', 'id', 'varchar', '36', NULL, 'NO', '0'),
        ('chunk_parse_content', 'chunk_task_id', 'varchar', '36', NULL, 'NO', '0'),
        ('chunk_parse_content', 'content', 'json', NULL, NULL, 'YES', '0'),
        ('chunk_parse_content', 'version', 'int', NULL, NULL, 'NO', '0'),
        ('chunk_parse_content', 'detail', 'json', NULL, NULL, 'YES', '0'),
        ('chunk_parse_content', 'create_time', 'bigint', NULL, NULL, 'YES', '0'),
        ('chunk_parse_content', 'create_date', 'datetime', NULL, '3', 'YES', '0'),
        ('chunk_parse_content', 'update_time', 'bigint', NULL, NULL, 'YES', '0'),
        ('chunk_parse_content', 'update_date', 'datetime', NULL, '3', 'YES', '0'),
        ('interval_parse_task', 'id', 'varchar', '36', NULL, 'NO', '0'),
        ('interval_parse_task', 'chunk_task_id', 'varchar', '36', NULL, 'NO', '0'),
        ('interval_parse_task', 'video_task_id', 'varchar', '36', NULL, 'NO', '0'),
        ('interval_parse_task', 'chunk_task_ids', 'json', NULL, NULL, 'NO', '0'),
        ('interval_parse_task', 'start_sequence_no', 'int', NULL, NULL, 'NO', '0'),
        ('interval_parse_task', 'chunk_count', 'int', NULL, NULL, 'NO', '0'),
        ('interval_parse_task', 'ready_mask', 'bigint', NULL, NULL, 'NO', '0'),
        ('interval_parse_task', 'status', 'varchar', '16', NULL, 'NO', '0'),
        ('interval_parse_task', 'next_execute_time', 'timestamp', NULL, '3', 'NO', '0'),
        ('interval_parse_task', 'retry_count', 'int', NULL, NULL, 'NO', '0'),
        ('interval_parse_task', 'lease_owner', 'varchar', '128', NULL, 'YES', '0'),
        ('interval_parse_task', 'lease_version', 'int', NULL, NULL, 'NO', '0'),
        ('interval_parse_task', 'lease_expire_time', 'timestamp', NULL, '3', 'YES', '0'),
        ('interval_parse_task', 'start_time', 'timestamp', NULL, '3', 'YES', '0'),
        ('interval_parse_task', 'finish_time', 'timestamp', NULL, '3', 'YES', '0'),
        ('interval_parse_task', 'last_error', 'json', NULL, NULL, 'YES', '0'),
        ('interval_parse_task', 'create_time', 'bigint', NULL, NULL, 'YES', '0'),
        ('interval_parse_task', 'create_date', 'datetime', NULL, '3', 'YES', '0'),
        ('interval_parse_task', 'update_time', 'bigint', NULL, NULL, 'YES', '0'),
        ('interval_parse_task', 'update_date', 'datetime', NULL, '3', 'YES', '0'),
        ('upload_segment', 'session_id', 'varchar', '36', NULL, 'NO', '0'),
        ('upload_segment', 'segment_number', 'int', NULL, NULL, 'NO', '0'),
        ('upload_segment', 'start_byte', 'bigint', NULL, NULL, 'NO', '0'),
        ('upload_segment', 'end_byte_exclusive', 'bigint', NULL, NULL, 'NO', '0'),
        ('upload_segment', 'size_bytes', 'int', NULL, NULL, 'NO', '0'),
        ('upload_segment', 'sha256', 'varchar', '64', NULL, 'NO', '0'),
        ('upload_segment', 'artifact_uri', 'longtext', NULL, NULL, 'NO', '0'),
        ('upload_segment', 'create_time', 'bigint', NULL, NULL, 'YES', '0'),
        ('upload_segment', 'create_date', 'datetime', NULL, '3', 'YES', '0'),
        ('upload_segment', 'update_time', 'bigint', NULL, NULL, 'YES', '0'),
        ('upload_segment', 'update_date', 'datetime', NULL, '3', 'YES', '0'),
        ('interval_parse_content', 'interval_task_id', 'varchar', '36', NULL, 'NO', '0'),
        ('interval_parse_content', 'content', 'json', NULL, NULL, 'NO', '0'),
        ('interval_parse_content', 'detail', 'json', NULL, NULL, 'YES', '0'),
        ('interval_parse_content', 'create_time', 'bigint', NULL, NULL, 'YES', '0'),
        ('interval_parse_content', 'create_date', 'datetime', NULL, '3', 'YES', '0'),
        ('interval_parse_content', 'update_time', 'bigint', NULL, NULL, 'YES', '0'),
        ('interval_parse_content', 'update_date', 'datetime', NULL, '3', 'YES', '0');
    INSERT INTO _bm_source_fks VALUES
        ('agent_message', 'conversation_id', 'agent_conversation', 'conversation_id', 'CASCADE', 'RESTRICT'),
        ('tenant', 'owner_user_id', 'user', 'user_id', 'RESTRICT', 'RESTRICT'),
        ('video', 'series_id', 'series', 'series_id', 'CASCADE', 'RESTRICT'),
        ('series_parse_job', 'series_id', 'series', 'series_id', 'CASCADE', 'RESTRICT'),
        ('series_parse_job', 'video_id', 'video', 'video_id', 'CASCADE', 'RESTRICT'),
        ('video_chunk', 'video_id', 'video', 'video_id', 'CASCADE', 'RESTRICT'),
        ('video_parse_task', 'video_id', 'video', 'video_id', 'CASCADE', 'RESTRICT'),
        ('chunk_parse_task', 'video_parse_task_id', 'video_parse_task', 'id', 'CASCADE', 'RESTRICT'),
        ('chunk_parse_task', 'video_chunk_id', 'video_chunk', 'video_chunk_id', 'CASCADE', 'RESTRICT'),
        ('context_snapshot', 'series_id', 'series', 'series_id', 'CASCADE', 'RESTRICT'),
        ('context_snapshot', 'created_from_video_chunk_id', 'video_chunk', 'video_chunk_id', 'CASCADE', 'RESTRICT'),
        ('context_snapshot', 'video_id', 'video', 'video_id', 'CASCADE', 'RESTRICT'),
        ('index_sync_intent', 'video_chunk_id', 'video_chunk', 'video_chunk_id', 'CASCADE', 'RESTRICT'),
        ('parse_model_call', 'video_parse_task_id', 'video_parse_task', 'id', 'CASCADE', 'RESTRICT'),
        ('parse_model_call', 'job_id', 'series_parse_job', 'job_id', 'RESTRICT', 'RESTRICT'),
        ('parse_usage_attempt', 'job_id', 'series_parse_job', 'job_id', 'RESTRICT', 'RESTRICT'),
        ('parse_usage_attempt', 'video_parse_task_id', 'video_parse_task', 'id', 'SET NULL', 'RESTRICT'),
        ('upload_session', 'job_id', 'series_parse_job', 'job_id', 'SET NULL', 'RESTRICT'),
        ('upload_session', 'series_id', 'series', 'series_id', 'CASCADE', 'RESTRICT'),
        ('upload_session', 'initial_video_parse_task_id', 'video_parse_task', 'id', 'SET NULL', 'RESTRICT'),
        ('upload_session', 'video_id', 'video', 'video_id', 'SET NULL', 'RESTRICT'),
        ('video_chunk_checkpoint', 'job_id', 'series_parse_job', 'job_id', 'CASCADE', 'RESTRICT'),
        ('video_chunk_embedding', 'video_chunk_id', 'video_chunk', 'video_chunk_id', 'CASCADE', 'RESTRICT'),
        ('video_chunk_info', 'video_chunk_id', 'video_chunk', 'video_chunk_id', 'CASCADE', 'RESTRICT'),
        ('video_parse_usage_attempt', 'video_parse_task_id', 'video_parse_task', 'id', 'CASCADE', 'RESTRICT'),
        ('chunk_parse_content', 'chunk_parse_task_id', 'chunk_parse_task', 'id', 'CASCADE', 'RESTRICT'),
        ('interval_parse_task', 'chunk_parse_task_id,video_parse_task_id', 'chunk_parse_task', 'id,video_parse_task_id', 'CASCADE', 'RESTRICT'),
        ('interval_parse_task', 'video_parse_task_id', 'video_parse_task', 'id', 'CASCADE', 'RESTRICT'),
        ('upload_segment', 'upload_session_id', 'upload_session', 'upload_session_id', 'CASCADE', 'RESTRICT'),
        ('interval_parse_content', 'interval_parse_task_id', 'interval_parse_task', 'id', 'CASCADE', 'RESTRICT');
    INSERT INTO _bm_target_fks VALUES
        ('agent_message', 'conversation_id', 'agent_conversation', 'id', 'CASCADE', 'RESTRICT'),
        ('tenant', 'owner_id', 'user', 'id', 'RESTRICT', 'RESTRICT'),
        ('video', 'series_id', 'series', 'id', 'CASCADE', 'RESTRICT'),
        ('series_parse_job', 'video_id', 'video', 'id', 'CASCADE', 'RESTRICT'),
        ('series_parse_job', 'series_id', 'series', 'id', 'CASCADE', 'RESTRICT'),
        ('video_chunk', 'video_id', 'video', 'id', 'CASCADE', 'RESTRICT'),
        ('video_parse_task', 'video_id', 'video', 'id', 'CASCADE', 'RESTRICT'),
        ('chunk_parse_task', 'chunk_id', 'video_chunk', 'id', 'CASCADE', 'RESTRICT'),
        ('chunk_parse_task', 'video_task_id', 'video_parse_task', 'id', 'CASCADE', 'RESTRICT'),
        ('context_snapshot', 'source_chunk_id', 'video_chunk', 'id', 'CASCADE', 'RESTRICT'),
        ('context_snapshot', 'video_id', 'video', 'id', 'CASCADE', 'RESTRICT'),
        ('context_snapshot', 'series_id', 'series', 'id', 'CASCADE', 'RESTRICT'),
        ('index_sync_intent', 'chunk_id', 'video_chunk', 'id', 'CASCADE', 'RESTRICT'),
        ('parse_model_call', 'job_id', 'series_parse_job', 'id', 'RESTRICT', 'RESTRICT'),
        ('parse_model_call', 'video_task_id', 'video_parse_task', 'id', 'CASCADE', 'RESTRICT'),
        ('parse_usage_attempt', 'video_task_id', 'video_parse_task', 'id', 'SET NULL', 'RESTRICT'),
        ('parse_usage_attempt', 'job_id', 'series_parse_job', 'id', 'RESTRICT', 'RESTRICT'),
        ('upload_session', 'initial_task_id', 'video_parse_task', 'id', 'SET NULL', 'RESTRICT'),
        ('upload_session', 'job_id', 'series_parse_job', 'id', 'SET NULL', 'RESTRICT'),
        ('upload_session', 'video_id', 'video', 'id', 'SET NULL', 'RESTRICT'),
        ('upload_session', 'series_id', 'series', 'id', 'CASCADE', 'RESTRICT'),
        ('video_chunk_checkpoint', 'job_id', 'series_parse_job', 'id', 'CASCADE', 'RESTRICT'),
        ('video_chunk_embedding', 'chunk_id', 'video_chunk', 'id', 'CASCADE', 'RESTRICT'),
        ('video_chunk_info', 'chunk_id', 'video_chunk', 'id', 'CASCADE', 'RESTRICT'),
        ('video_parse_usage_attempt', 'video_task_id', 'video_parse_task', 'id', 'CASCADE', 'RESTRICT'),
        ('chunk_parse_content', 'chunk_task_id', 'chunk_parse_task', 'id', 'CASCADE', 'RESTRICT'),
        ('interval_parse_task', 'video_task_id', 'video_parse_task', 'id', 'CASCADE', 'RESTRICT'),
        ('interval_parse_task', 'chunk_task_id,video_task_id', 'chunk_parse_task', 'id,video_task_id', 'CASCADE', 'RESTRICT'),
        ('upload_segment', 'session_id', 'upload_session', 'id', 'CASCADE', 'RESTRICT'),
        ('interval_parse_content', 'interval_task_id', 'interval_parse_task', 'id', 'CASCADE', 'RESTRICT');
    INSERT INTO _bm_source_checks VALUES
        ('agent_conversation', 'ck_agent_conversation_next_message_sequence', 'next_message_sequence >= 0'),
        ('model_usage', 'ck_model_usage_task_type', 'task_type IN (''video_parse'', ''chat_retrieval'')'),
        ('user', 'ck_user_auth_version', 'auth_version >= 1'),
        ('user', 'ck_guest_email', 'is_guest = 0 OR email IS NULL'),
        ('user', 'ck_guest_role', 'is_guest = 0 OR role = ''user'''),
        ('user', 'ck_user_status', 'status IN (''ACTIVE'', ''DISABLED'')'),
        ('user', 'ck_user_role', 'role IN (''user'', ''admin'')'),
        ('agent_message', 'ck_agent_message_version', 'version > 0'),
        ('agent_message', 'ck_agent_message_status', 'status IN (''PROCESSING'', ''COMPLETED'', ''FAILED'')'),
        ('agent_message', 'ck_agent_message_sequence', 'sequence_no >= 0'),
        ('agent_message', 'ck_agent_message_phase', 'phase IS NULL OR phase IN (''QUEUED'', ''RETRIEVING'', ''EVALUATING'', ''RESPONDING'')'),
        ('agent_message', 'ck_agent_message_requested_top_k', 'requested_top_k IS NULL OR requested_top_k BETWEEN 1 AND 100'),
        ('agent_message', 'ck_agent_message_role', 'role IN (''USER'', ''ASSISTANT'')'),
        ('tenant', 'ck_tenant_type', 'type IN (''local'', ''public'', ''personal'')'),
        ('tenant', 'ck_owned_tenant_owner', 'type = ''public'' OR owner_user_id IS NOT NULL'),
        ('tenant', 'ck_public_tenant_owner', 'type <> ''public'' OR owner_user_id IS NULL'),
        ('tenant', 'ck_tenant_status', 'status IN (''ACTIVE'', ''DISABLED'')'),
        ('video', 'ck_video_duration', 'duration_seconds > 0'),
        ('video', 'ck_video_episode_order', 'episode_order > 0'),
        ('video', 'ck_video_total_bytes', 'total_bytes >= 0'),
        ('video', 'ck_video_parse_progress', 'parse_progress BETWEEN 0 AND 100'),
        ('video', 'ck_video_upload_progress', 'upload_progress BETWEEN 0 AND 100'),
        ('series_parse_job', 'ck_series_parse_job_progress', 'progress BETWEEN 0 AND 100'),
        ('series_parse_job', 'ck_series_parse_job_lease_version', 'lease_version >= 0'),
        ('series_parse_job', 'ck_series_parse_job_stage', 'stage IN (''QUEUED'', ''PREPARING'', ''SPLIT_READY'', ''PARSING'', ''TEXT_EMBEDDING'', ''VISION_EMBEDDING'', ''PERSISTING'', ''INDEXING'', ''COMPLETED'')'),
        ('series_parse_job', 'ck_series_parse_job_processing_duration', 'processing_duration_ms >= 0'),
        ('series_parse_job', 'ck_series_parse_job_status', 'status IN (''QUEUED'', ''RUNNING'', ''RETRY_WAIT'', ''SUCCEEDED'', ''FAILED'')'),
        ('series_parse_job', 'ck_series_parse_job_attempt_count', 'attempt_count >= 0'),
        ('series_parse_job', 'ck_series_parse_job_version', 'version >= 1'),
        ('series_parse_job', 'ck_series_parse_job_max_attempts', 'max_attempts > 0'),
        ('video_chunk', 'ck_video_chunk_sequence', 'sequence_no >= 0'),
        ('video_chunk', 'ck_video_chunk_range', 'start_seconds >= 0 AND end_seconds > start_seconds'),
        ('video_chunk', 'ck_video_chunk_analysis_range', 'analysis_window_start_seconds <= start_seconds AND analysis_window_end_seconds >= end_seconds'),
        ('video_parse_task', 'ck_video_parse_task_attempt_count', 'attempt_count >= 0'),
        ('video_parse_task', 'ck_video_parse_task_lease_state', '(status IN (''PREPARING'', ''PARSING'', ''INDEXING'') AND lease_owner IS NOT NULL AND lease_expire_time IS NOT NULL AND heartbeat_time IS NOT NULL) OR (lease_owner IS NULL AND lease_expire_time IS NULL AND heartbeat_time IS NULL)'),
        ('video_parse_task', 'ck_video_parse_task_chunk_count', 'chunk_count > 0'),
        ('video_parse_task', 'ck_video_parse_task_max_attempts', 'max_attempts > 0'),
        ('video_parse_task', 'ck_video_parse_task_lease_version', 'lease_version >= 0'),
        ('video_parse_task', 'ck_video_parse_task_version', 'version > 0'),
        ('video_parse_task', 'ck_video_parse_task_status', 'status IN (''PENDING'', ''PREPARING'', ''PARSING'', ''INDEXING'', ''COMPLETE'', ''PARSING_FAILED'', ''INDEXING_FAILED'')'),
        ('video_parse_task', 'ck_video_parse_task_processing_duration', 'processing_duration_ms >= 0'),
        ('chunk_parse_task', 'ck_chunk_parse_task_lease_version', 'lease_version >= 0'),
        ('chunk_parse_task', 'ck_chunk_parse_task_status', 'status IN (''QUEUED'', ''UNDERSTANDING'', ''COMPLETE'', ''FAILED'')'),
        ('chunk_parse_task', 'ck_chunk_parse_task_lease_state', '(status = ''UNDERSTANDING'' AND lease_owner IS NOT NULL AND lease_expire_time IS NOT NULL) OR (status <> ''UNDERSTANDING'' AND lease_owner IS NULL AND lease_expire_time IS NULL)'),
        ('chunk_parse_task', 'ck_chunk_parse_task_retry_count', 'retry_count >= 0'),
        ('context_snapshot', 'ck_context_snapshot_sequence', 'after_chunk_sequence_no >= 0'),
        ('index_sync_intent', 'ck_index_sync_intent_generation', 'generation > 0'),
        ('index_sync_intent', 'ck_index_sync_intent_operation', 'operation IN (''UPSERT'', ''DELETE'')'),
        ('index_sync_intent', 'ck_index_sync_intent_status', 'status IN (''PENDING'', ''RETRY_WAIT'', ''SYNCED'')'),
        ('index_sync_intent', 'ck_index_sync_intent_attempt_count', 'attempt_count >= 0'),
        ('parse_model_call', 'ck_parse_model_call_exactly_one_task', '(job_id IS NOT NULL) <> (video_parse_task_id IS NOT NULL)'),
        ('parse_model_call', 'ck_parse_model_call_category', 'category IN (''parse'', ''embedding'')'),
        ('parse_model_call', 'ck_parse_model_call_execution_identity', '(video_parse_task_id IS NULL AND execution_kind IS NULL AND execution_task_id IS NULL) OR (video_parse_task_id IS NOT NULL AND execution_kind IS NOT NULL AND execution_task_id IS NOT NULL)'),
        ('parse_model_call', 'ck_parse_model_call_status', 'status IN (''completed'', ''failed'')'),
        ('parse_model_call', 'ck_parse_model_call_execution_kind', 'execution_kind IS NULL OR execution_kind IN (''video'', ''chunk'', ''interval'', ''prefix'')'),
        ('upload_session', 'ck_upload_session_completion_stage', 'completion_stage IS NULL OR completion_stage IN (''VERIFYING'', ''ASSEMBLING'', ''PROBING'', ''CREATING_JOB'')'),
        ('upload_session', 'ck_upload_session_upload_sequence', 'upload_sequence > 0'),
        ('upload_session', 'ck_upload_session_segment_size', 'preferred_segment_size_bytes > 0'),
        ('upload_session', 'ck_upload_session_status', 'status IN (''UPLOADING'', ''COMPLETING'', ''COMPLETED'', ''EXPIRED'')'),
        ('upload_session', 'ck_upload_session_total_segments', 'total_segments > 0'),
        ('upload_session', 'ck_upload_session_episode_order', 'episode_order > 0'),
        ('upload_session', 'ck_upload_session_version', 'version > 0'),
        ('upload_session', 'ck_upload_session_total_size', 'total_size_bytes > 0'),
        ('video_chunk_checkpoint', 'ck_video_chunk_checkpoint_range', '(chunk_start_seconds IS NULL AND chunk_end_seconds IS NULL) OR (chunk_start_seconds IS NOT NULL AND chunk_end_seconds IS NOT NULL AND chunk_start_seconds >= 0 AND chunk_end_seconds > chunk_start_seconds)'),
        ('video_chunk_checkpoint', 'ck_video_chunk_checkpoint_sequence', 'sequence_no >= 0'),
        ('video_chunk_checkpoint', 'ck_video_chunk_checkpoint_stage', 'stage IN (''PREPARED'', ''SPLIT_READY'', ''PARSED'', ''TEXT_EMBEDDED'', ''VISION_EMBEDDED'', ''INDEXED'')'),
        ('video_parse_usage_attempt', 'ck_video_parse_usage_attempt_execution_kind', 'execution_kind IN (''video'', ''chunk'', ''interval'', ''prefix'')'),
        ('chunk_parse_content', 'ck_chunk_parse_content_version', 'version > 0'),
        ('interval_parse_task', 'ck_interval_parse_task_lease_version', 'lease_version >= 0'),
        ('interval_parse_task', 'ck_interval_parse_task_ready_mask', 'ready_mask >= 0 AND (ready_mask >> chunk_count) = 0'),
        ('interval_parse_task', 'ck_interval_parse_task_lease_state', '(status IN (''REFINING'', ''INDEXING'') AND lease_owner IS NOT NULL AND lease_expire_time IS NOT NULL) OR (status IN (''WAITING'', ''INDEXING'', ''COMPLETE'', ''PARSING_FAILED'', ''INDEXING_FAILED'') AND lease_owner IS NULL AND lease_expire_time IS NULL)'),
        ('interval_parse_task', 'ck_interval_parse_task_status', 'status IN (''WAITING'', ''REFINING'', ''INDEXING'', ''COMPLETE'', ''PARSING_FAILED'', ''INDEXING_FAILED'')'),
        ('interval_parse_task', 'ck_interval_parse_task_retry_count', 'retry_count >= 0'),
        ('interval_parse_task', 'ck_interval_parse_task_start_sequence', 'start_sequence_no >= 0'),
        ('interval_parse_task', 'ck_interval_parse_task_chunk_count', 'chunk_count BETWEEN 1 AND 63'),
        ('upload_segment', 'ck_upload_segment_range', 'start_byte >= 0 AND end_byte_exclusive > start_byte'),
        ('upload_segment', 'ck_upload_segment_number', 'segment_number >= 0'),
        ('upload_segment', 'ck_upload_segment_size', 'actual_size_bytes = end_byte_exclusive - start_byte');
    INSERT INTO _bm_target_checks VALUES
        ('agent_conversation', 'ck_agent_conversation_next_message_sequence', 'next_message_sequence >= 0'),
        ('model_usage', 'ck_model_usage_task_type', 'task_type IN (''video_parse'', ''chat_retrieval'')'),
        ('user', 'ck_user_status', 'status IN (''ACTIVE'', ''DISABLED'')'),
        ('user', 'ck_user_auth_version', 'auth_version >= 1'),
        ('user', 'ck_guest_email', 'is_anonymous = 0 OR email IS NULL'),
        ('user', 'ck_user_superuser', 'is_superuser IN (0, 1)'),
        ('user', 'ck_guest_role', 'is_anonymous = 0 OR is_superuser = 0'),
        ('agent_message', 'ck_agent_message_version', 'version > 0'),
        ('agent_message', 'ck_agent_message_status', 'status IN (''PROCESSING'', ''COMPLETED'', ''FAILED'')'),
        ('agent_message', 'ck_agent_message_sequence', 'sequence_no >= 0'),
        ('agent_message', 'ck_agent_message_requested_top_k', 'requested_top_k IS NULL OR requested_top_k BETWEEN 1 AND 100'),
        ('agent_message', 'ck_agent_message_phase', 'phase IS NULL OR phase IN (''QUEUED'', ''RETRIEVING'', ''EVALUATING'', ''RESPONDING'')'),
        ('agent_message', 'ck_agent_message_role', 'role IN (''USER'', ''ASSISTANT'')'),
        ('tenant', 'ck_owned_tenant_owner', 'type = ''public'' OR owner_id IS NOT NULL'),
        ('tenant', 'ck_public_tenant_owner', 'type <> ''public'' OR owner_id IS NULL'),
        ('tenant', 'ck_tenant_type', 'type IN (''local'', ''public'', ''personal'')'),
        ('tenant', 'ck_tenant_status', 'status IN (''ACTIVE'', ''DISABLED'')'),
        ('video', 'ck_video_duration', 'duration_ms > 0'),
        ('video', 'ck_video_total_bytes', 'size_bytes >= 0'),
        ('video', 'ck_video_upload_progress', 'upload_progress BETWEEN 0 AND 100'),
        ('video', 'ck_video_episode_order', 'episode_order > 0'),
        ('video', 'ck_video_parse_progress', 'parse_progress BETWEEN 0 AND 100'),
        ('series_parse_job', 'ck_series_parse_job_status', 'status IN (''QUEUED'', ''RUNNING'', ''RETRY_WAIT'', ''SUCCEEDED'', ''FAILED'')'),
        ('series_parse_job', 'ck_series_parse_job_lease_version', 'lease_version >= 0'),
        ('series_parse_job', 'ck_series_parse_job_progress', 'progress BETWEEN 0 AND 100'),
        ('series_parse_job', 'ck_series_parse_job_processing_duration', 'processing_duration_ms >= 0'),
        ('series_parse_job', 'ck_series_parse_job_attempt_count', 'attempt_count >= 0'),
        ('series_parse_job', 'ck_series_parse_job_version', 'version >= 1'),
        ('series_parse_job', 'ck_series_parse_job_max_attempts', 'max_attempts > 0'),
        ('series_parse_job', 'ck_series_parse_job_stage', 'stage IN (''QUEUED'', ''PREPARING'', ''SPLIT_READY'', ''PARSING'', ''TEXT_EMBEDDING'', ''VISION_EMBEDDING'', ''PERSISTING'', ''INDEXING'', ''COMPLETED'')'),
        ('video_chunk', 'ck_video_chunk_range', 'start_ms >= 0 AND end_ms > start_ms'),
        ('video_chunk', 'ck_video_chunk_sequence', 'sequence_no >= 0'),
        ('video_chunk', 'ck_video_chunk_analysis_range', 'window_start_ms <= start_ms AND window_end_ms >= end_ms'),
        ('video_parse_task', 'ck_video_parse_task_max_attempts', 'max_attempts > 0'),
        ('video_parse_task', 'ck_video_parse_task_lease_version', 'lease_version >= 0'),
        ('video_parse_task', 'ck_video_parse_task_version', 'version > 0'),
        ('video_parse_task', 'ck_video_parse_task_processing_duration', 'processing_duration_ms >= 0'),
        ('video_parse_task', 'ck_video_parse_task_status', 'status IN (''PENDING'', ''PREPARING'', ''PARSING'', ''INDEXING'', ''COMPLETE'', ''PARSING_FAILED'', ''INDEXING_FAILED'')'),
        ('video_parse_task', 'ck_video_parse_task_chunk_count', 'chunk_count > 0'),
        ('video_parse_task', 'ck_video_parse_task_attempt_count', 'attempt_count >= 0'),
        ('video_parse_task', 'ck_video_parse_task_lease_state', '(status IN (''PREPARING'', ''PARSING'', ''INDEXING'') AND lease_owner IS NOT NULL AND lease_expire_time IS NOT NULL AND heartbeat_time IS NOT NULL) OR (lease_owner IS NULL AND lease_expire_time IS NULL AND heartbeat_time IS NULL)'),
        ('chunk_parse_task', 'ck_chunk_parse_task_retry_count', 'retry_count >= 0'),
        ('chunk_parse_task', 'ck_chunk_parse_task_lease_version', 'lease_version >= 0'),
        ('chunk_parse_task', 'ck_chunk_parse_task_lease_state', '(status = ''UNDERSTANDING'' AND lease_owner IS NOT NULL AND lease_expire_time IS NOT NULL) OR (status <> ''UNDERSTANDING'' AND lease_owner IS NULL AND lease_expire_time IS NULL)'),
        ('chunk_parse_task', 'ck_chunk_parse_task_status', 'status IN (''QUEUED'', ''UNDERSTANDING'', ''COMPLETE'', ''FAILED'')'),
        ('context_snapshot', 'ck_context_snapshot_sequence', 'after_chunk_sequence_no >= 0'),
        ('index_sync_intent', 'ck_index_sync_intent_attempt_count', 'attempt_count >= 0'),
        ('index_sync_intent', 'ck_index_sync_intent_generation', 'generation > 0'),
        ('index_sync_intent', 'ck_index_sync_intent_operation', 'operation IN (''UPSERT'', ''DELETE'')'),
        ('index_sync_intent', 'ck_index_sync_intent_status', 'status IN (''PENDING'', ''RETRY_WAIT'', ''SYNCED'')'),
        ('parse_model_call', 'ck_parse_model_call_exactly_one_task', '(job_id IS NOT NULL) <> (video_task_id IS NOT NULL)'),
        ('parse_model_call', 'ck_parse_model_call_execution_identity', '(video_task_id IS NULL AND execution_kind IS NULL AND execution_task_id IS NULL) OR (video_task_id IS NOT NULL AND execution_kind IS NOT NULL AND execution_task_id IS NOT NULL)'),
        ('parse_model_call', 'ck_parse_model_call_status', 'status IN (''completed'', ''failed'')'),
        ('parse_model_call', 'ck_parse_model_call_category', 'category IN (''parse'', ''embedding'')'),
        ('parse_model_call', 'ck_parse_model_call_execution_kind', 'execution_kind IS NULL OR execution_kind IN (''video'', ''chunk'', ''interval'', ''prefix'')'),
        ('upload_session', 'ck_upload_session_episode_order', 'episode_order > 0'),
        ('upload_session', 'ck_upload_session_status', 'status IN (''UPLOADING'', ''COMPLETING'', ''COMPLETED'', ''EXPIRED'')'),
        ('upload_session', 'ck_upload_session_total_segments', 'segment_count > 0'),
        ('upload_session', 'ck_upload_session_upload_sequence', 'upload_sequence > 0'),
        ('upload_session', 'ck_upload_session_version', 'version > 0'),
        ('upload_session', 'ck_upload_session_total_size', 'size_bytes > 0'),
        ('upload_session', 'ck_upload_session_segment_size', 'segment_size_bytes > 0'),
        ('upload_session', 'ck_upload_session_completion_stage', 'completion_stage IS NULL OR completion_stage IN (''VERIFYING'', ''ASSEMBLING'', ''PROBING'', ''CREATING_JOB'')'),
        ('video_chunk_checkpoint', 'ck_video_chunk_checkpoint_range', '(start_ms IS NULL AND end_ms IS NULL) OR (start_ms IS NOT NULL AND end_ms IS NOT NULL AND start_ms >= 0 AND end_ms > start_ms)'),
        ('video_chunk_checkpoint', 'ck_video_chunk_checkpoint_stage', 'stage IN (''PREPARED'', ''SPLIT_READY'', ''PARSED'', ''TEXT_EMBEDDED'', ''VISION_EMBEDDED'', ''INDEXED'')'),
        ('video_chunk_checkpoint', 'ck_video_chunk_checkpoint_sequence', 'sequence_no >= 0'),
        ('video_parse_usage_attempt', 'ck_video_parse_usage_attempt_execution_kind', 'execution_kind IN (''video'', ''chunk'', ''interval'', ''prefix'')'),
        ('chunk_parse_content', 'ck_chunk_parse_content_version', 'version > 0'),
        ('interval_parse_task', 'ck_interval_parse_task_lease_version', 'lease_version >= 0'),
        ('interval_parse_task', 'ck_interval_parse_task_ready_mask', 'ready_mask >= 0 AND (ready_mask >> chunk_count) = 0'),
        ('interval_parse_task', 'ck_interval_parse_task_lease_state', '(status IN (''REFINING'', ''INDEXING'') AND lease_owner IS NOT NULL AND lease_expire_time IS NOT NULL) OR (status IN (''WAITING'', ''INDEXING'', ''COMPLETE'', ''PARSING_FAILED'', ''INDEXING_FAILED'') AND lease_owner IS NULL AND lease_expire_time IS NULL)'),
        ('interval_parse_task', 'ck_interval_parse_task_retry_count', 'retry_count >= 0'),
        ('interval_parse_task', 'ck_interval_parse_task_status', 'status IN (''WAITING'', ''REFINING'', ''INDEXING'', ''COMPLETE'', ''PARSING_FAILED'', ''INDEXING_FAILED'')'),
        ('interval_parse_task', 'ck_interval_parse_task_start_sequence', 'start_sequence_no >= 0'),
        ('interval_parse_task', 'ck_interval_parse_task_chunk_count', 'chunk_count BETWEEN 1 AND 63'),
        ('upload_segment', 'ck_upload_segment_range', 'start_byte >= 0 AND end_byte_exclusive > start_byte'),
        ('upload_segment', 'ck_upload_segment_number', 'segment_number >= 0'),
        ('upload_segment', 'ck_upload_segment_size', 'size_bytes = end_byte_exclusive - start_byte');
    INSERT INTO _bm_source_keys VALUES
        ('agent_conversation', 'PRIMARY', '1', 'conversation_id'),
        ('agent_conversation', 'uq_agent_conversation_tenant_idempotency', '1', 'tenant_id,owner_user_id,idempotency_key'),
        ('agent_conversation', 'ix_agent_conversation_tenant_id', '0', 'tenant_id'),
        ('model_usage', 'PRIMARY', '1', 'call_id'),
        ('model_usage', 'ix_model_usage_tenant_actor_started', '0', 'tenant_id,actor_user_id,started_at'),
        ('model_usage', 'ix_model_usage_tenant_started', '0', 'tenant_id,started_at,call_id'),
        ('model_usage', 'ix_model_usage_tenant_run_started', '0', 'tenant_id,run_id,started_at'),
        ('series', 'PRIMARY', '1', 'series_id'),
        ('series', 'uq_series_tenant_name', '1', 'tenant_id,name'),
        ('user', 'PRIMARY', '1', 'user_id'),
        ('user', NULL, '1', 'email'),
        ('agent_message', 'PRIMARY', '1', 'message_id'),
        ('agent_message', 'uq_agent_message_conversation_sequence', '1', 'conversation_id,sequence_no'),
        ('agent_message', 'ix_agent_message_conversation_status', '0', 'conversation_id,status'),
        ('agent_message', 'uq_agent_message_conversation_idempotency', '1', 'conversation_id,#expression'),
        ('agent_message', 'ix_agent_message_conversation_id', '0', 'conversation_id'),
        ('tenant', 'PRIMARY', '1', 'tenant_id'),
        ('tenant', 'ix_tenant_owner_user_id', '0', 'owner_user_id'),
        ('video', 'PRIMARY', '1', 'video_id'),
        ('video', 'uq_video_series_order', '1', 'series_id,episode_order'),
        ('video', 'uq_video_series_hash', '1', 'series_id,content_hash'),
        ('video', 'ix_video_series_id', '0', 'series_id'),
        ('series_parse_job', 'PRIMARY', '1', 'job_id'),
        ('series_parse_job', 'uq_series_parse_job_video_pipeline', '1', 'video_id,pipeline_version'),
        ('series_parse_job', 'ix_series_parse_job_due', '0', 'status,available_at'),
        ('series_parse_job', 'ix_series_parse_job_video_id', '0', 'video_id'),
        ('series_parse_job', 'ix_series_parse_job_series_id', '0', 'series_id'),
        ('video_chunk', 'PRIMARY', '1', 'video_chunk_id'),
        ('video_chunk', 'uq_video_chunk_video_sequence', '1', 'video_id,sequence_no'),
        ('video_chunk', 'ix_video_chunk_video_id', '0', 'video_id'),
        ('video_parse_task', 'PRIMARY', '1', 'id'),
        ('video_parse_task', 'uq_video_parse_task_video_version', '1', 'video_id,version'),
        ('video_parse_task', 'ix_video_parse_task_due', '0', 'status,next_execute_time'),
        ('video_parse_task', 'ix_video_parse_task_video_status', '0', 'video_id,status,version'),
        ('video_parse_task', 'ix_video_parse_task_lease', '0', 'status,lease_expire_time'),
        ('chunk_parse_task', 'PRIMARY', '1', 'id'),
        ('chunk_parse_task', 'uq_chunk_parse_task_parent_chunk', '1', 'video_parse_task_id,video_chunk_id'),
        ('chunk_parse_task', 'uq_chunk_parse_task_id_parent', '1', 'id,video_parse_task_id'),
        ('chunk_parse_task', 'ix_chunk_parse_task_parent_status', '0', 'video_parse_task_id,status,video_chunk_id'),
        ('chunk_parse_task', 'ix_chunk_parse_task_due', '0', 'status,next_execute_time'),
        ('chunk_parse_task', 'ix_chunk_parse_task_lease', '0', 'status,lease_expire_time'),
        ('context_snapshot', 'PRIMARY', '1', 'snapshot_id'),
        ('context_snapshot', 'uq_context_snapshot_video_sequence', '1', 'video_id,after_chunk_sequence_no'),
        ('context_snapshot', NULL, '1', 'created_from_video_chunk_id'),
        ('context_snapshot', 'ix_context_snapshot_series_id', '0', 'series_id'),
        ('context_snapshot', 'ix_context_snapshot_video_id', '0', 'video_id'),
        ('index_sync_intent', 'PRIMARY', '1', 'video_chunk_id'),
        ('index_sync_intent', 'ix_index_sync_intent_due', '0', 'status,available_at'),
        ('parse_model_call', 'PRIMARY', '1', 'call_id'),
        ('parse_model_call', 'ix_parse_model_call_video_parse_task_started', '0', 'video_parse_task_id,started_at'),
        ('parse_model_call', 'ix_parse_model_call_job_started', '0', 'job_id,started_at'),
        ('parse_model_call', 'ix_parse_model_call_v2_execution', '0', 'video_parse_task_id,execution_kind,execution_task_id,started_at'),
        ('parse_usage_attempt', 'PRIMARY', '1', 'job_id,attempt_count,lease_version'),
        ('parse_usage_attempt', 'ix_parse_usage_attempt_video_parse_task_id', '0', 'video_parse_task_id'),
        ('upload_session', 'PRIMARY', '1', 'upload_session_id'),
        ('upload_session', 'uq_upload_session_tenant_idempotency', '1', 'tenant_id,created_by_user_id,idempotency_key'),
        ('upload_session', 'uq_upload_session_active_slot', '1', 'active_slot_key'),
        ('upload_session', 'ix_upload_session_series_id', '0', 'series_id'),
        ('upload_session', 'ix_upload_session_video_id', '0', 'video_id'),
        ('upload_session', 'ix_upload_session_job_id', '0', 'job_id'),
        ('upload_session', 'ix_upload_session_initial_video_parse_task_id', '0', 'initial_video_parse_task_id'),
        ('video_chunk_checkpoint', 'PRIMARY', '1', 'job_id,sequence_no'),
        ('video_chunk_checkpoint', 'ix_video_chunk_checkpoint_job_stage', '0', 'job_id,stage'),
        ('video_chunk_embedding', 'PRIMARY', '1', 'video_chunk_id'),
        ('video_chunk_info', 'PRIMARY', '1', 'video_chunk_id'),
        ('video_parse_usage_attempt', 'PRIMARY', '1', 'attempt_id'),
        ('video_parse_usage_attempt', 'uq_video_parse_usage_attempt_lease', '1', 'video_parse_task_id,execution_kind,execution_task_id,attempt_count,lease_version'),
        ('video_parse_usage_attempt', 'ix_video_parse_usage_attempt_video_parse_task_id', '0', 'video_parse_task_id'),
        ('chunk_parse_content', 'PRIMARY', '1', 'id'),
        ('chunk_parse_content', 'uq_chunk_parse_content_task', '1', 'chunk_parse_task_id'),
        ('interval_parse_task', 'PRIMARY', '1', 'id'),
        ('interval_parse_task', 'uq_interval_parse_task_parent_start', '1', 'video_parse_task_id,start_sequence_no'),
        ('interval_parse_task', 'ix_interval_parse_task_due', '0', 'status,next_execute_time'),
        ('interval_parse_task', 'ix_interval_parse_task_parent_status', '0', 'video_parse_task_id,status,start_sequence_no'),
        ('interval_parse_task', 'ix_interval_parse_task_lease', '0', 'status,lease_expire_time'),
        ('upload_segment', 'PRIMARY', '1', 'upload_session_id,segment_number'),
        ('interval_parse_content', 'PRIMARY', '1', 'interval_parse_task_id');
    INSERT INTO _bm_target_keys VALUES
        ('agent_conversation', 'PRIMARY', '1', 'id'),
        ('agent_conversation', 'uq_agent_conversation_tenant_idempotency', '1', 'tenant_id,owner_id,idempotency_key'),
        ('agent_conversation', 'ix_agent_conversation_update_date', '0', 'update_date'),
        ('agent_conversation', 'ix_agent_conversation_create_time', '0', 'create_time'),
        ('agent_conversation', 'ix_agent_conversation_create_date', '0', 'create_date'),
        ('agent_conversation', 'ix_agent_conversation_tenant_id', '0', 'tenant_id'),
        ('agent_conversation', 'ix_agent_conversation_update_time', '0', 'update_time'),
        ('model_usage', 'PRIMARY', '1', 'id'),
        ('model_usage', 'ix_model_usage_tenant_run_started', '0', 'tenant_id,run_id,start_time'),
        ('model_usage', 'ix_model_usage_update_time', '0', 'update_time'),
        ('model_usage', 'ix_model_usage_update_date', '0', 'update_date'),
        ('model_usage', 'ix_model_usage_create_time', '0', 'create_time'),
        ('model_usage', 'ix_model_usage_tenant_started', '0', 'tenant_id,start_time,id'),
        ('model_usage', 'ix_model_usage_create_date', '0', 'create_date'),
        ('model_usage', 'ix_model_usage_tenant_actor_started', '0', 'tenant_id,user_id,start_time'),
        ('series', 'PRIMARY', '1', 'id'),
        ('series', 'uq_series_tenant_name', '1', 'tenant_id,name'),
        ('series', 'ix_series_update_time', '0', 'update_time'),
        ('series', 'ix_series_update_date', '0', 'update_date'),
        ('series', 'ix_series_create_date', '0', 'create_date'),
        ('series', 'ix_series_create_time', '0', 'create_time'),
        ('user', 'PRIMARY', '1', 'id'),
        ('user', NULL, '1', 'email'),
        ('user', 'ix_user_update_time', '0', 'update_time'),
        ('user', 'ix_user_create_date', '0', 'create_date'),
        ('user', 'ix_user_create_time', '0', 'create_time'),
        ('user', 'ix_user_last_login_time', '0', 'last_login_time'),
        ('user', 'ix_user_nickname', '0', 'nickname'),
        ('user', 'ix_user_update_date', '0', 'update_date'),
        ('agent_message', 'PRIMARY', '1', 'id'),
        ('agent_message', 'uq_agent_message_conversation_sequence', '1', 'conversation_id,sequence_no'),
        ('agent_message', 'ix_agent_message_conversation_id', '0', 'conversation_id'),
        ('agent_message', 'ix_agent_message_create_date', '0', 'create_date'),
        ('agent_message', 'ix_agent_message_update_time', '0', 'update_time'),
        ('agent_message', 'uq_agent_message_conversation_idempotency', '1', 'conversation_id,#expression'),
        ('agent_message', 'ix_agent_message_update_date', '0', 'update_date'),
        ('agent_message', 'ix_agent_message_conversation_status', '0', 'conversation_id,status'),
        ('agent_message', 'ix_agent_message_create_time', '0', 'create_time'),
        ('tenant', 'PRIMARY', '1', 'id'),
        ('tenant', 'ix_tenant_update_date', '0', 'update_date'),
        ('tenant', 'ix_tenant_create_time', '0', 'create_time'),
        ('tenant', 'ix_tenant_owner_id', '0', 'owner_id'),
        ('tenant', 'ix_tenant_create_date', '0', 'create_date'),
        ('tenant', 'ix_tenant_update_time', '0', 'update_time'),
        ('video', 'PRIMARY', '1', 'id'),
        ('video', 'uq_video_series_hash', '1', 'series_id,hash'),
        ('video', 'uq_video_series_order', '1', 'series_id,episode_order'),
        ('video', 'ix_video_update_time', '0', 'update_time'),
        ('video', 'ix_video_series_id', '0', 'series_id'),
        ('video', 'ix_video_update_date', '0', 'update_date'),
        ('video', 'ix_video_create_time', '0', 'create_time'),
        ('video', 'ix_video_create_date', '0', 'create_date'),
        ('series_parse_job', 'PRIMARY', '1', 'id'),
        ('series_parse_job', 'uq_series_parse_job_video_pipeline', '1', 'video_id,pipeline_version'),
        ('series_parse_job', 'ix_series_parse_job_update_date', '0', 'update_date'),
        ('series_parse_job', 'ix_series_parse_job_due', '0', 'status,next_execute_time'),
        ('series_parse_job', 'ix_series_parse_job_create_time', '0', 'create_time'),
        ('series_parse_job', 'ix_series_parse_job_create_date', '0', 'create_date'),
        ('series_parse_job', 'ix_series_parse_job_video_id', '0', 'video_id'),
        ('series_parse_job', 'ix_series_parse_job_series_id', '0', 'series_id'),
        ('series_parse_job', 'ix_series_parse_job_update_time', '0', 'update_time'),
        ('video_chunk', 'PRIMARY', '1', 'id'),
        ('video_chunk', 'uq_video_chunk_video_sequence', '1', 'video_id,sequence_no'),
        ('video_chunk', 'ix_video_chunk_update_date', '0', 'update_date'),
        ('video_chunk', 'ix_video_chunk_update_time', '0', 'update_time'),
        ('video_chunk', 'ix_video_chunk_video_id', '0', 'video_id'),
        ('video_chunk', 'ix_video_chunk_create_time', '0', 'create_time'),
        ('video_chunk', 'ix_video_chunk_create_date', '0', 'create_date'),
        ('video_parse_task', 'PRIMARY', '1', 'id'),
        ('video_parse_task', 'uq_video_parse_task_video_version', '1', 'video_id,version'),
        ('video_parse_task', 'ix_video_parse_task_due', '0', 'status,next_execute_time'),
        ('video_parse_task', 'ix_video_parse_task_update_date', '0', 'update_date'),
        ('video_parse_task', 'ix_video_parse_task_create_time', '0', 'create_time'),
        ('video_parse_task', 'ix_video_parse_task_create_date', '0', 'create_date'),
        ('video_parse_task', 'ix_video_parse_task_video_status', '0', 'video_id,status,version'),
        ('video_parse_task', 'ix_video_parse_task_update_time', '0', 'update_time'),
        ('video_parse_task', 'ix_video_parse_task_lease', '0', 'status,lease_expire_time'),
        ('chunk_parse_task', 'PRIMARY', '1', 'id'),
        ('chunk_parse_task', 'uq_chunk_parse_task_parent_chunk', '1', 'video_task_id,chunk_id'),
        ('chunk_parse_task', 'uq_chunk_parse_task_id_parent', '1', 'id,video_task_id'),
        ('chunk_parse_task', 'ix_chunk_parse_task_create_date', '0', 'create_date'),
        ('chunk_parse_task', 'ix_chunk_parse_task_update_time', '0', 'update_time'),
        ('chunk_parse_task', 'ix_chunk_parse_task_parent_status', '0', 'video_task_id,status,chunk_id'),
        ('chunk_parse_task', 'ix_chunk_parse_task_update_date', '0', 'update_date'),
        ('chunk_parse_task', 'ix_chunk_parse_task_lease', '0', 'status,lease_expire_time'),
        ('chunk_parse_task', 'ix_chunk_parse_task_create_time', '0', 'create_time'),
        ('chunk_parse_task', 'ix_chunk_parse_task_due', '0', 'status,next_execute_time'),
        ('context_snapshot', 'PRIMARY', '1', 'id'),
        ('context_snapshot', NULL, '1', 'source_chunk_id'),
        ('context_snapshot', 'uq_context_snapshot_video_sequence', '1', 'video_id,after_chunk_sequence_no'),
        ('context_snapshot', 'ix_context_snapshot_update_time', '0', 'update_time'),
        ('context_snapshot', 'ix_context_snapshot_create_date', '0', 'create_date'),
        ('context_snapshot', 'ix_context_snapshot_series_id', '0', 'series_id'),
        ('context_snapshot', 'ix_context_snapshot_create_time', '0', 'create_time'),
        ('context_snapshot', 'ix_context_snapshot_video_id', '0', 'video_id'),
        ('context_snapshot', 'ix_context_snapshot_update_date', '0', 'update_date'),
        ('index_sync_intent', 'PRIMARY', '1', 'chunk_id'),
        ('index_sync_intent', 'ix_index_sync_intent_update_time', '0', 'update_time'),
        ('index_sync_intent', 'ix_index_sync_intent_create_time', '0', 'create_time'),
        ('index_sync_intent', 'ix_index_sync_intent_update_date', '0', 'update_date'),
        ('index_sync_intent', 'ix_index_sync_intent_due', '0', 'status,next_execute_time'),
        ('index_sync_intent', 'ix_index_sync_intent_create_date', '0', 'create_date'),
        ('parse_model_call', 'PRIMARY', '1', 'id'),
        ('parse_model_call', 'ix_parse_model_call_create_time', '0', 'create_time'),
        ('parse_model_call', 'ix_parse_model_call_video_parse_task_started', '0', 'video_task_id,start_time'),
        ('parse_model_call', 'ix_parse_model_call_create_date', '0', 'create_date'),
        ('parse_model_call', 'ix_parse_model_call_job_started', '0', 'job_id,start_time'),
        ('parse_model_call', 'ix_parse_model_call_v2_execution', '0', 'video_task_id,execution_kind,execution_task_id,start_time'),
        ('parse_model_call', 'ix_parse_model_call_update_time', '0', 'update_time'),
        ('parse_model_call', 'ix_parse_model_call_update_date', '0', 'update_date'),
        ('parse_usage_attempt', 'PRIMARY', '1', 'job_id,attempt_count,lease_version'),
        ('parse_usage_attempt', 'ix_parse_usage_attempt_create_time', '0', 'create_time'),
        ('parse_usage_attempt', 'ix_parse_usage_attempt_update_date', '0', 'update_date'),
        ('parse_usage_attempt', 'ix_parse_usage_attempt_create_date', '0', 'create_date'),
        ('parse_usage_attempt', 'ix_parse_usage_attempt_video_task_id', '0', 'video_task_id'),
        ('parse_usage_attempt', 'ix_parse_usage_attempt_update_time', '0', 'update_time'),
        ('upload_session', 'PRIMARY', '1', 'id'),
        ('upload_session', 'uq_upload_session_active_slot', '1', 'active_slot_key'),
        ('upload_session', 'uq_upload_session_tenant_idempotency', '1', 'tenant_id,creator_id,idempotency_key'),
        ('upload_session', 'ix_upload_session_series_id', '0', 'series_id'),
        ('upload_session', 'ix_upload_session_create_date', '0', 'create_date'),
        ('upload_session', 'ix_upload_session_initial_task_id', '0', 'initial_task_id'),
        ('upload_session', 'ix_upload_session_update_time', '0', 'update_time'),
        ('upload_session', 'ix_upload_session_update_date', '0', 'update_date'),
        ('upload_session', 'ix_upload_session_job_id', '0', 'job_id'),
        ('upload_session', 'ix_upload_session_video_id', '0', 'video_id'),
        ('upload_session', 'ix_upload_session_create_time', '0', 'create_time'),
        ('video_chunk_checkpoint', 'PRIMARY', '1', 'job_id,sequence_no'),
        ('video_chunk_checkpoint', 'ix_video_chunk_checkpoint_job_stage', '0', 'job_id,stage'),
        ('video_chunk_checkpoint', 'ix_video_chunk_checkpoint_create_time', '0', 'create_time'),
        ('video_chunk_checkpoint', 'ix_video_chunk_checkpoint_update_time', '0', 'update_time'),
        ('video_chunk_checkpoint', 'ix_video_chunk_checkpoint_update_date', '0', 'update_date'),
        ('video_chunk_checkpoint', 'ix_video_chunk_checkpoint_create_date', '0', 'create_date'),
        ('video_chunk_embedding', 'PRIMARY', '1', 'chunk_id'),
        ('video_chunk_embedding', 'ix_video_chunk_embedding_update_time', '0', 'update_time'),
        ('video_chunk_embedding', 'ix_video_chunk_embedding_create_time', '0', 'create_time'),
        ('video_chunk_embedding', 'ix_video_chunk_embedding_create_date', '0', 'create_date'),
        ('video_chunk_embedding', 'ix_video_chunk_embedding_update_date', '0', 'update_date'),
        ('video_chunk_info', 'PRIMARY', '1', 'chunk_id'),
        ('video_chunk_info', 'ix_video_chunk_info_update_time', '0', 'update_time'),
        ('video_chunk_info', 'ix_video_chunk_info_create_date', '0', 'create_date'),
        ('video_chunk_info', 'ix_video_chunk_info_create_time', '0', 'create_time'),
        ('video_chunk_info', 'ix_video_chunk_info_update_date', '0', 'update_date'),
        ('video_parse_usage_attempt', 'PRIMARY', '1', 'id'),
        ('video_parse_usage_attempt', 'uq_video_parse_usage_attempt_lease', '1', 'video_task_id,execution_kind,execution_task_id,attempt_count,lease_version'),
        ('video_parse_usage_attempt', 'ix_video_parse_usage_attempt_update_time', '0', 'update_time'),
        ('video_parse_usage_attempt', 'ix_video_parse_usage_attempt_video_task_id', '0', 'video_task_id'),
        ('video_parse_usage_attempt', 'ix_video_parse_usage_attempt_update_date', '0', 'update_date'),
        ('video_parse_usage_attempt', 'ix_video_parse_usage_attempt_create_time', '0', 'create_time'),
        ('video_parse_usage_attempt', 'ix_video_parse_usage_attempt_create_date', '0', 'create_date'),
        ('chunk_parse_content', 'PRIMARY', '1', 'id'),
        ('chunk_parse_content', 'uq_chunk_parse_content_task', '1', 'chunk_task_id'),
        ('chunk_parse_content', 'ix_chunk_parse_content_create_time', '0', 'create_time'),
        ('chunk_parse_content', 'ix_chunk_parse_content_update_time', '0', 'update_time'),
        ('chunk_parse_content', 'ix_chunk_parse_content_create_date', '0', 'create_date'),
        ('chunk_parse_content', 'ix_chunk_parse_content_update_date', '0', 'update_date'),
        ('interval_parse_task', 'PRIMARY', '1', 'id'),
        ('interval_parse_task', 'uq_interval_parse_task_parent_start', '1', 'video_task_id,start_sequence_no'),
        ('interval_parse_task', 'ix_interval_parse_task_parent_status', '0', 'video_task_id,status,start_sequence_no'),
        ('interval_parse_task', 'ix_interval_parse_task_update_date', '0', 'update_date'),
        ('interval_parse_task', 'ix_interval_parse_task_create_date', '0', 'create_date'),
        ('interval_parse_task', 'ix_interval_parse_task_lease', '0', 'status,lease_expire_time'),
        ('interval_parse_task', 'ix_interval_parse_task_due', '0', 'status,next_execute_time'),
        ('interval_parse_task', 'ix_interval_parse_task_create_time', '0', 'create_time'),
        ('interval_parse_task', 'ix_interval_parse_task_update_time', '0', 'update_time'),
        ('upload_segment', 'PRIMARY', '1', 'session_id,segment_number'),
        ('upload_segment', 'ix_upload_segment_create_time', '0', 'create_time'),
        ('upload_segment', 'ix_upload_segment_update_date', '0', 'update_date'),
        ('upload_segment', 'ix_upload_segment_create_date', '0', 'create_date'),
        ('upload_segment', 'ix_upload_segment_update_time', '0', 'update_time'),
        ('interval_parse_content', 'PRIMARY', '1', 'interval_task_id'),
        ('interval_parse_content', 'ix_interval_parse_content_create_date', '0', 'create_date'),
        ('interval_parse_content', 'ix_interval_parse_content_update_date', '0', 'update_date'),
        ('interval_parse_content', 'ix_interval_parse_content_update_time', '0', 'update_time'),
        ('interval_parse_content', 'ix_interval_parse_content_create_time', '0', 'create_time');

    -- P1: complete old table/column set. New target columns or an id PK trigger
    -- this check on rerun; no business DDL has executed yet.
    IF (SELECT COUNT(*) FROM information_schema.tables t JOIN _bm_tables e
            ON e.table_name = t.table_name WHERE t.table_schema = DATABASE()
            AND t.table_type = 'BASE TABLE' AND t.engine = 'InnoDB') <> 24 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'expected all 24 source business tables using InnoDB';
    END IF;
    IF EXISTS (SELECT 1 FROM _bm_source_cols e LEFT JOIN information_schema.columns c
            ON c.table_schema = DATABASE() AND c.table_name = e.table_name AND c.column_name = e.column_name
            WHERE c.column_name IS NULL OR c.data_type <> e.data_type
                OR (e.char_length IS NOT NULL AND NOT(c.character_maximum_length <=> e.char_length))
                OR (e.date_precision IS NOT NULL AND NOT(c.datetime_precision <=> e.date_precision))
                OR c.is_nullable <> e.nullable OR (LOCATE('unsigned', c.column_type) > 0) <> e.is_unsigned) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source columns/types/nullability differ; partial or completed migration is rejected';
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.columns c JOIN _bm_tables t ON t.table_name = c.table_name
            LEFT JOIN _bm_source_cols e ON e.table_name = c.table_name AND e.column_name = c.column_name
            WHERE c.table_schema = DATABASE() AND e.column_name IS NULL) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source columns/types/nullability differ; partial or completed migration is rejected';
    END IF;
    -- Physical FK names may be generated by InnoDB or by an earlier migration.
    CREATE TEMPORARY TABLE _bm_actual_fks AS
        SELECT k.table_name, k.constraint_name, GROUP_CONCAT(k.column_name ORDER BY k.ordinal_position SEPARATOR ',') columns_csv,
               MAX(k.referenced_table_name) ref_table,
               GROUP_CONCAT(k.referenced_column_name ORDER BY k.ordinal_position SEPARATOR ',') ref_columns_csv,
               REPLACE(MAX(r.delete_rule), 'NO ACTION', 'RESTRICT') delete_rule,
               REPLACE(MAX(r.update_rule), 'NO ACTION', 'RESTRICT') update_rule
        FROM information_schema.key_column_usage k
        JOIN information_schema.referential_constraints r
            ON r.constraint_schema = k.constraint_schema AND r.table_name = k.table_name AND r.constraint_name = k.constraint_name
        JOIN _bm_tables t ON t.table_name = k.table_name
        WHERE k.constraint_schema = DATABASE() AND k.referenced_table_name IS NOT NULL
        GROUP BY k.table_name, k.constraint_name;
    IF (SELECT COUNT(*) FROM _bm_actual_fks) <> 30 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'expected all 30 source FK constraints including the two-column interval FK';
    END IF;
    IF EXISTS (SELECT 1 FROM _bm_source_fks e LEFT JOIN _bm_actual_fks a
            ON a.table_name=e.table_name AND a.columns_csv=e.columns_csv AND a.ref_table=e.ref_table
                AND a.ref_columns_csv=e.ref_columns_csv AND a.delete_rule=e.delete_rule AND a.update_rule=e.update_rule
            WHERE a.constraint_name IS NULL) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'expected all 30 source FK constraints including the two-column interval FK';
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.key_column_usage k LEFT JOIN _bm_tables t ON t.table_name=k.table_name
            WHERE k.referenced_table_schema=DATABASE() AND k.referenced_table_name IN ('agent_conversation', 'agent_message', 'chunk_parse_content', 'chunk_parse_task', 'context_snapshot', 'index_sync_intent', 'interval_parse_content', 'interval_parse_task', 'model_usage', 'parse_model_call', 'parse_usage_attempt', 'series', 'series_parse_job', 'tenant', 'upload_segment', 'upload_session', 'user', 'video', 'video_chunk', 'video_chunk_checkpoint', 'video_chunk_embedding', 'video_chunk_info', 'video_parse_task', 'video_parse_usage_attempt')
                AND (t.table_name IS NULL OR k.table_schema <> DATABASE())) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'unexpected cross-scope foreign key requires a separately reviewed migration';
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.key_column_usage k
            JOIN _bm_tables t ON t.table_name=k.table_name
            JOIN information_schema.columns src ON src.table_schema=k.table_schema AND src.table_name=k.table_name AND src.column_name=k.column_name
            JOIN information_schema.columns dst ON dst.table_schema=k.referenced_table_schema AND dst.table_name=k.referenced_table_name AND dst.column_name=k.referenced_column_name
            WHERE k.table_schema=DATABASE() AND k.referenced_table_name IS NOT NULL
                AND (k.referenced_table_schema <> DATABASE() OR src.column_type <> dst.column_type
                    OR NOT(src.character_set_name <=> dst.character_set_name) OR NOT(src.collation_name <=> dst.collation_name))) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'FK source/target types or text collations are incompatible';
    END IF;
    -- P2: PKs, UNIQUEs, declared indexes, and enforced CHECKs match the old model.
    CREATE TEMPORARY TABLE _bm_actual_keys AS
        SELECT s.table_name, s.index_name key_name, MAX(1-s.non_unique) is_unique,
            GROUP_CONCAT(COALESCE(s.column_name, '#expression') ORDER BY s.seq_in_index SEPARATOR ',') columns_csv
        FROM information_schema.statistics s JOIN _bm_tables t ON t.table_name=s.table_name
        WHERE s.table_schema=DATABASE() GROUP BY s.table_name,s.index_name;
    IF EXISTS (SELECT 1 FROM _bm_source_keys e LEFT JOIN _bm_actual_keys a
            ON a.table_name=e.table_name AND (e.key_name IS NULL OR a.key_name=e.key_name)
                AND a.is_unique=e.is_unique AND a.columns_csv=e.columns_csv
            WHERE a.key_name IS NULL) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source PK/UNIQUE/index definitions differ from the reviewed schema';
    END IF;
    IF (SELECT COUNT(*) FROM _bm_actual_keys WHERE is_unique=1) <> (SELECT COUNT(*) FROM _bm_source_keys WHERE is_unique=1) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source PK/UNIQUE/index definitions differ from the reviewed schema';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.statistics
            WHERE table_schema=DATABASE() AND table_name='agent_message'
                AND index_name='uq_agent_message_conversation_idempotency' AND seq_in_index=2
                AND expression LIKE '%idempotencyKey%' AND expression LIKE '%128%') THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'the message JSON idempotency functional index must retain its request key and width';
    END IF;
    IF (SELECT COUNT(*) FROM information_schema.table_constraints c JOIN _bm_tables t ON t.table_name=c.table_name
            WHERE c.constraint_schema=DATABASE() AND c.constraint_type='CHECK') <> (SELECT COUNT(*) FROM _bm_source_checks) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source CHECK names/enforcement/expressions differ from the reviewed schema';
    END IF;
    IF EXISTS (SELECT 1 FROM _bm_source_checks e LEFT JOIN information_schema.table_constraints tc
            ON tc.constraint_schema=DATABASE() AND tc.table_name=e.table_name AND tc.constraint_name=e.check_name AND tc.constraint_type='CHECK'
            LEFT JOIN information_schema.check_constraints cc ON cc.constraint_schema=tc.constraint_schema AND cc.constraint_name=tc.constraint_name
            WHERE tc.constraint_name IS NULL OR tc.enforced <> 'YES'
                OR LOWER(REGEXP_REPLACE(REGEXP_REPLACE(REPLACE(cc.check_clause, CONCAT(CHAR(92), CHAR(39)), CHAR(39)), '(?<![A-Za-z0-9_''])_[A-Za-z0-9]+(?='')', ''), '[[:space:]`()]+', ''))
                 <> LOWER(REGEXP_REPLACE(e.clause, '[[:space:]`()]+', ''))) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source CHECK names/enforcement/expressions differ from the reviewed schema';
    END IF;
    IF EXISTS (SELECT 1 FROM _bm_target_keys e JOIN information_schema.statistics s
            ON s.table_schema=DATABASE() AND s.table_name=e.table_name AND s.index_name=e.key_name
            LEFT JOIN _bm_source_keys old_key ON old_key.table_name=e.table_name AND old_key.key_name=e.key_name
            WHERE old_key.key_name IS NULL) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'a new audit/profile index name conflicts with the source schema';
    END IF;
    -- P3: the media data already uses milliseconds. Only names change here.
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
            WHERE table_schema=DATABASE() AND table_name='video' AND column_name='duration_seconds'
                AND column_comment='media_time_milliseconds') THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'apply the media-millisecond prerequisite first; values will not be multiplied here';
    END IF;
    IF EXISTS (SELECT 1 FROM `user` WHERE BINARY role NOT IN (BINARY 'user', BINARY 'admin') OR role IS NULL
            OR is_guest NOT IN (0,1) OR (is_guest=1 AND (email IS NOT NULL OR BINARY role <> BINARY 'user'))) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source User role/guest values cannot be converted losslessly';
    END IF;
    IF EXISTS (SELECT 1 FROM series_parse_job WHERE lease_owner IS NOT NULL) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'stop writers and release parse leases before changing field names';
    END IF;
    IF EXISTS (SELECT 1 FROM video_parse_task WHERE lease_owner IS NOT NULL) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'stop writers and release parse leases before changing field names';
    END IF;
    IF EXISTS (SELECT 1 FROM chunk_parse_task WHERE lease_owner IS NOT NULL) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'stop writers and release parse leases before changing field names';
    END IF;
    IF EXISTS (SELECT 1 FROM interval_parse_task WHERE lease_owner IS NOT NULL) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'stop writers and release parse leases before changing field names';
    END IF;
    IF EXISTS (SELECT 1 FROM `series` WHERE `created_at` < 0 OR `created_at` > 253402300799999) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'series.created_at cannot be represented as UTC DATETIME(3) and Unix milliseconds';
    END IF;
    IF EXISTS (SELECT 1 FROM `series` WHERE `updated_at` < 0 OR `updated_at` > 253402300799999) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'series.updated_at cannot be represented as UTC DATETIME(3) and Unix milliseconds';
    END IF;
    IF EXISTS (SELECT 1 FROM `interval_parse_task` WHERE `create_time` < '1970-01-01 00:00:00.000') THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'interval_parse_task.create_time cannot be represented as UTC DATETIME(3) and Unix milliseconds';
    END IF;
    IF EXISTS (SELECT 1 FROM `interval_parse_task` WHERE `update_time` < '1970-01-01 00:00:00.000') THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'interval_parse_task.update_time cannot be represented as UTC DATETIME(3) and Unix milliseconds';
    END IF;
    IF EXISTS (SELECT 1 FROM `video_parse_task` WHERE `create_time` < '1970-01-01 00:00:00.000') THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'video_parse_task.create_time cannot be represented as UTC DATETIME(3) and Unix milliseconds';
    END IF;
    IF EXISTS (SELECT 1 FROM `video_parse_task` WHERE `update_time` < '1970-01-01 00:00:00.000') THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'video_parse_task.update_time cannot be represented as UTC DATETIME(3) and Unix milliseconds';
    END IF;
    IF EXISTS (SELECT 1 FROM `parse_model_call` WHERE `created_at` < '1970-01-01 00:00:00.000') THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'parse_model_call.created_at cannot be represented as UTC DATETIME(3) and Unix milliseconds';
    END IF;
    IF EXISTS (SELECT 1 FROM `parse_model_call` WHERE `updated_at` < '1970-01-01 00:00:00.000') THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'parse_model_call.updated_at cannot be represented as UTC DATETIME(3) and Unix milliseconds';
    END IF;
    IF EXISTS (SELECT 1 FROM `agent_conversation` WHERE `created_at` < 0 OR `created_at` > 253402300799999) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'agent_conversation.created_at cannot be represented as UTC DATETIME(3) and Unix milliseconds';
    END IF;
    IF EXISTS (SELECT 1 FROM `agent_conversation` WHERE `updated_at` < 0 OR `updated_at` > 253402300799999) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'agent_conversation.updated_at cannot be represented as UTC DATETIME(3) and Unix milliseconds';
    END IF;
    IF EXISTS (SELECT 1 FROM `user` WHERE `created_at` < 0 OR `created_at` > 253402300799999) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'user.created_at cannot be represented as UTC DATETIME(3) and Unix milliseconds';
    END IF;
    IF EXISTS (SELECT 1 FROM `user` WHERE `updated_at` < 0 OR `updated_at` > 253402300799999) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'user.updated_at cannot be represented as UTC DATETIME(3) and Unix milliseconds';
    END IF;
    IF EXISTS (SELECT 1 FROM `agent_message` WHERE `created_at` < 0 OR `created_at` > 253402300799999) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'agent_message.created_at cannot be represented as UTC DATETIME(3) and Unix milliseconds';
    END IF;
    IF EXISTS (SELECT 1 FROM `agent_message` WHERE `updated_at` < 0 OR `updated_at` > 253402300799999) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'agent_message.updated_at cannot be represented as UTC DATETIME(3) and Unix milliseconds';
    END IF;
    IF EXISTS (SELECT 1 FROM `video_chunk_checkpoint` WHERE `updated_at` < 0 OR `updated_at` > 253402300799999) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'video_chunk_checkpoint.updated_at cannot be represented as UTC DATETIME(3) and Unix milliseconds';
    END IF;
    IF EXISTS (SELECT 1 FROM `index_sync_intent` WHERE `created_at` < 0 OR `created_at` > 253402300799999) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'index_sync_intent.created_at cannot be represented as UTC DATETIME(3) and Unix milliseconds';
    END IF;
    IF EXISTS (SELECT 1 FROM `index_sync_intent` WHERE `updated_at` < 0 OR `updated_at` > 253402300799999) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'index_sync_intent.updated_at cannot be represented as UTC DATETIME(3) and Unix milliseconds';
    END IF;
    IF EXISTS (SELECT 1 FROM `context_snapshot` WHERE `created_at` < 0 OR `created_at` > 253402300799999) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'context_snapshot.created_at cannot be represented as UTC DATETIME(3) and Unix milliseconds';
    END IF;
    IF EXISTS (SELECT 1 FROM `chunk_parse_task` WHERE `create_time` < '1970-01-01 00:00:00.000') THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'chunk_parse_task.create_time cannot be represented as UTC DATETIME(3) and Unix milliseconds';
    END IF;
    IF EXISTS (SELECT 1 FROM `chunk_parse_task` WHERE `update_time` < '1970-01-01 00:00:00.000') THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'chunk_parse_task.update_time cannot be represented as UTC DATETIME(3) and Unix milliseconds';
    END IF;
    IF EXISTS (SELECT 1 FROM `interval_parse_content` WHERE `create_time` < '1970-01-01 00:00:00.000') THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'interval_parse_content.create_time cannot be represented as UTC DATETIME(3) and Unix milliseconds';
    END IF;
    IF EXISTS (SELECT 1 FROM `interval_parse_content` WHERE `update_time` < '1970-01-01 00:00:00.000') THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'interval_parse_content.update_time cannot be represented as UTC DATETIME(3) and Unix milliseconds';
    END IF;
    IF EXISTS (SELECT 1 FROM `model_usage` WHERE `created_at` < '1970-01-01 00:00:00.000') THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'model_usage.created_at cannot be represented as UTC DATETIME(3) and Unix milliseconds';
    END IF;
    IF EXISTS (SELECT 1 FROM `chunk_parse_content` WHERE `create_time` < '1970-01-01 00:00:00.000') THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'chunk_parse_content.create_time cannot be represented as UTC DATETIME(3) and Unix milliseconds';
    END IF;
    IF EXISTS (SELECT 1 FROM `chunk_parse_content` WHERE `update_time` < '1970-01-01 00:00:00.000') THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'chunk_parse_content.update_time cannot be represented as UTC DATETIME(3) and Unix milliseconds';
    END IF;
    IF EXISTS (SELECT 1 FROM `video` WHERE `created_at` < '1970-01-01 00:00:00.000') THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'video.created_at cannot be represented as UTC DATETIME(3) and Unix milliseconds';
    END IF;
    IF EXISTS (SELECT 1 FROM `video` WHERE `updated_at` < '1970-01-01 00:00:00.000') THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'video.updated_at cannot be represented as UTC DATETIME(3) and Unix milliseconds';
    END IF;
    IF EXISTS (SELECT 1 FROM `series_parse_job` WHERE `created_at` < 0 OR `created_at` > 253402300799999) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'series_parse_job.created_at cannot be represented as UTC DATETIME(3) and Unix milliseconds';
    END IF;
    IF EXISTS (SELECT 1 FROM `series_parse_job` WHERE `updated_at` < 0 OR `updated_at` > 253402300799999) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'series_parse_job.updated_at cannot be represented as UTC DATETIME(3) and Unix milliseconds';
    END IF;
    IF EXISTS (SELECT 1 FROM `tenant` WHERE `created_at` < 0 OR `created_at` > 253402300799999) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'tenant.created_at cannot be represented as UTC DATETIME(3) and Unix milliseconds';
    END IF;
    IF EXISTS (SELECT 1 FROM `tenant` WHERE `updated_at` < 0 OR `updated_at` > 253402300799999) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'tenant.updated_at cannot be represented as UTC DATETIME(3) and Unix milliseconds';
    END IF;
    IF EXISTS (SELECT 1 FROM `upload_session` WHERE `created_at` < '1970-01-01 00:00:00.000') THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'upload_session.created_at cannot be represented as UTC DATETIME(3) and Unix milliseconds';
    END IF;
    IF EXISTS (SELECT 1 FROM `upload_session` WHERE `updated_at` < '1970-01-01 00:00:00.000') THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'upload_session.updated_at cannot be represented as UTC DATETIME(3) and Unix milliseconds';
    END IF;
    IF EXISTS (SELECT 1 FROM `upload_segment` WHERE `created_at` < '1970-01-01 00:00:00.000') THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'upload_segment.created_at cannot be represented as UTC DATETIME(3) and Unix milliseconds';
    END IF;
    IF EXISTS (SELECT 1 FROM `video_chunk` WHERE `created_at` < '1970-01-01 00:00:00.000') THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'video_chunk.created_at cannot be represented as UTC DATETIME(3) and Unix milliseconds';
    END IF;
    IF EXISTS (SELECT 1 FROM `video_chunk` WHERE `updated_at` < '1970-01-01 00:00:00.000') THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'video_chunk.updated_at cannot be represented as UTC DATETIME(3) and Unix milliseconds';
    END IF;
    IF EXISTS (SELECT 1 FROM `video_chunk_embedding` WHERE `created_at` < 0 OR `created_at` > 253402300799999) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'video_chunk_embedding.created_at cannot be represented as UTC DATETIME(3) and Unix milliseconds';
    END IF;
    IF EXISTS (SELECT 1 FROM `video_chunk_embedding` WHERE `updated_at` < 0 OR `updated_at` > 253402300799999) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'video_chunk_embedding.updated_at cannot be represented as UTC DATETIME(3) and Unix milliseconds';
    END IF;
    -- Re-enabling CHECK/FK checks does not validate rows inserted while checks
    -- were disabled. Reject those rows before dropping any business constraint.
    IF EXISTS (SELECT 1 FROM `interval_parse_task` WHERE NOT(lease_version >= 0)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_interval_parse_task_lease_version';
    END IF;
    IF EXISTS (SELECT 1 FROM `interval_parse_task` WHERE NOT(ready_mask >= 0 AND (ready_mask >> chunk_count) = 0)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_interval_parse_task_ready_mask';
    END IF;
    IF EXISTS (SELECT 1 FROM `interval_parse_task` WHERE NOT((status IN ('REFINING', 'INDEXING') AND lease_owner IS NOT NULL AND lease_expire_time IS NOT NULL) OR (status IN ('WAITING', 'INDEXING', 'COMPLETE', 'PARSING_FAILED', 'INDEXING_FAILED') AND lease_owner IS NULL AND lease_expire_time IS NULL))) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_interval_parse_task_lease_state';
    END IF;
    IF EXISTS (SELECT 1 FROM `interval_parse_task` WHERE NOT(status IN ('WAITING', 'REFINING', 'INDEXING', 'COMPLETE', 'PARSING_FAILED', 'INDEXING_FAILED'))) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_interval_parse_task_status';
    END IF;
    IF EXISTS (SELECT 1 FROM `interval_parse_task` WHERE NOT(retry_count >= 0)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_interval_parse_task_retry_count';
    END IF;
    IF EXISTS (SELECT 1 FROM `interval_parse_task` WHERE NOT(start_sequence_no >= 0)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_interval_parse_task_start_sequence';
    END IF;
    IF EXISTS (SELECT 1 FROM `interval_parse_task` WHERE NOT(chunk_count BETWEEN 1 AND 63)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_interval_parse_task_chunk_count';
    END IF;
    IF EXISTS (SELECT 1 FROM `video_parse_task` WHERE NOT(attempt_count >= 0)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_video_parse_task_attempt_count';
    END IF;
    IF EXISTS (SELECT 1 FROM `video_parse_task` WHERE NOT((status IN ('PREPARING', 'PARSING', 'INDEXING') AND lease_owner IS NOT NULL AND lease_expire_time IS NOT NULL AND heartbeat_time IS NOT NULL) OR (lease_owner IS NULL AND lease_expire_time IS NULL AND heartbeat_time IS NULL))) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_video_parse_task_lease_state';
    END IF;
    IF EXISTS (SELECT 1 FROM `video_parse_task` WHERE NOT(chunk_count > 0)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_video_parse_task_chunk_count';
    END IF;
    IF EXISTS (SELECT 1 FROM `video_parse_task` WHERE NOT(max_attempts > 0)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_video_parse_task_max_attempts';
    END IF;
    IF EXISTS (SELECT 1 FROM `video_parse_task` WHERE NOT(lease_version >= 0)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_video_parse_task_lease_version';
    END IF;
    IF EXISTS (SELECT 1 FROM `video_parse_task` WHERE NOT(version > 0)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_video_parse_task_version';
    END IF;
    IF EXISTS (SELECT 1 FROM `video_parse_task` WHERE NOT(status IN ('PENDING', 'PREPARING', 'PARSING', 'INDEXING', 'COMPLETE', 'PARSING_FAILED', 'INDEXING_FAILED'))) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_video_parse_task_status';
    END IF;
    IF EXISTS (SELECT 1 FROM `video_parse_task` WHERE NOT(processing_duration_ms >= 0)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_video_parse_task_processing_duration';
    END IF;
    IF EXISTS (SELECT 1 FROM `parse_model_call` WHERE NOT((job_id IS NOT NULL) <> (video_parse_task_id IS NOT NULL))) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_parse_model_call_exactly_one_task';
    END IF;
    IF EXISTS (SELECT 1 FROM `parse_model_call` WHERE NOT(category IN ('parse', 'embedding'))) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_parse_model_call_category';
    END IF;
    IF EXISTS (SELECT 1 FROM `parse_model_call` WHERE NOT((video_parse_task_id IS NULL AND execution_kind IS NULL AND execution_task_id IS NULL) OR (video_parse_task_id IS NOT NULL AND execution_kind IS NOT NULL AND execution_task_id IS NOT NULL))) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_parse_model_call_execution_identity';
    END IF;
    IF EXISTS (SELECT 1 FROM `parse_model_call` WHERE NOT(status IN ('completed', 'failed'))) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_parse_model_call_status';
    END IF;
    IF EXISTS (SELECT 1 FROM `parse_model_call` WHERE NOT(execution_kind IS NULL OR execution_kind IN ('video', 'chunk', 'interval', 'prefix'))) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_parse_model_call_execution_kind';
    END IF;
    IF EXISTS (SELECT 1 FROM `agent_conversation` WHERE NOT(next_message_sequence >= 0)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_agent_conversation_next_message_sequence';
    END IF;
    IF EXISTS (SELECT 1 FROM `user` WHERE NOT(auth_version >= 1)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_user_auth_version';
    END IF;
    IF EXISTS (SELECT 1 FROM `user` WHERE NOT(is_guest = 0 OR email IS NULL)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_guest_email';
    END IF;
    IF EXISTS (SELECT 1 FROM `user` WHERE NOT(is_guest = 0 OR role = 'user')) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_guest_role';
    END IF;
    IF EXISTS (SELECT 1 FROM `user` WHERE NOT(status IN ('ACTIVE', 'DISABLED'))) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_user_status';
    END IF;
    IF EXISTS (SELECT 1 FROM `user` WHERE NOT(role IN ('user', 'admin'))) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_user_role';
    END IF;
    IF EXISTS (SELECT 1 FROM `agent_message` WHERE NOT(version > 0)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_agent_message_version';
    END IF;
    IF EXISTS (SELECT 1 FROM `agent_message` WHERE NOT(status IN ('PROCESSING', 'COMPLETED', 'FAILED'))) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_agent_message_status';
    END IF;
    IF EXISTS (SELECT 1 FROM `agent_message` WHERE NOT(sequence_no >= 0)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_agent_message_sequence';
    END IF;
    IF EXISTS (SELECT 1 FROM `agent_message` WHERE NOT(phase IS NULL OR phase IN ('QUEUED', 'RETRIEVING', 'EVALUATING', 'RESPONDING'))) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_agent_message_phase';
    END IF;
    IF EXISTS (SELECT 1 FROM `agent_message` WHERE NOT(requested_top_k IS NULL OR requested_top_k BETWEEN 1 AND 100)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_agent_message_requested_top_k';
    END IF;
    IF EXISTS (SELECT 1 FROM `agent_message` WHERE NOT(role IN ('USER', 'ASSISTANT'))) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_agent_message_role';
    END IF;
    IF EXISTS (SELECT 1 FROM `video_chunk_checkpoint` WHERE NOT((chunk_start_seconds IS NULL AND chunk_end_seconds IS NULL) OR (chunk_start_seconds IS NOT NULL AND chunk_end_seconds IS NOT NULL AND chunk_start_seconds >= 0 AND chunk_end_seconds > chunk_start_seconds))) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_video_chunk_checkpoint_range';
    END IF;
    IF EXISTS (SELECT 1 FROM `video_chunk_checkpoint` WHERE NOT(sequence_no >= 0)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_video_chunk_checkpoint_sequence';
    END IF;
    IF EXISTS (SELECT 1 FROM `video_chunk_checkpoint` WHERE NOT(stage IN ('PREPARED', 'SPLIT_READY', 'PARSED', 'TEXT_EMBEDDED', 'VISION_EMBEDDED', 'INDEXED'))) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_video_chunk_checkpoint_stage';
    END IF;
    IF EXISTS (SELECT 1 FROM `index_sync_intent` WHERE NOT(generation > 0)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_index_sync_intent_generation';
    END IF;
    IF EXISTS (SELECT 1 FROM `index_sync_intent` WHERE NOT(operation IN ('UPSERT', 'DELETE'))) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_index_sync_intent_operation';
    END IF;
    IF EXISTS (SELECT 1 FROM `index_sync_intent` WHERE NOT(status IN ('PENDING', 'RETRY_WAIT', 'SYNCED'))) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_index_sync_intent_status';
    END IF;
    IF EXISTS (SELECT 1 FROM `index_sync_intent` WHERE NOT(attempt_count >= 0)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_index_sync_intent_attempt_count';
    END IF;
    IF EXISTS (SELECT 1 FROM `video_parse_usage_attempt` WHERE NOT(execution_kind IN ('video', 'chunk', 'interval', 'prefix'))) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_video_parse_usage_attempt_execution_kind';
    END IF;
    IF EXISTS (SELECT 1 FROM `context_snapshot` WHERE NOT(after_chunk_sequence_no >= 0)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_context_snapshot_sequence';
    END IF;
    IF EXISTS (SELECT 1 FROM `chunk_parse_task` WHERE NOT(lease_version >= 0)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_chunk_parse_task_lease_version';
    END IF;
    IF EXISTS (SELECT 1 FROM `chunk_parse_task` WHERE NOT(status IN ('QUEUED', 'UNDERSTANDING', 'COMPLETE', 'FAILED'))) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_chunk_parse_task_status';
    END IF;
    IF EXISTS (SELECT 1 FROM `chunk_parse_task` WHERE NOT((status = 'UNDERSTANDING' AND lease_owner IS NOT NULL AND lease_expire_time IS NOT NULL) OR (status <> 'UNDERSTANDING' AND lease_owner IS NULL AND lease_expire_time IS NULL))) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_chunk_parse_task_lease_state';
    END IF;
    IF EXISTS (SELECT 1 FROM `chunk_parse_task` WHERE NOT(retry_count >= 0)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_chunk_parse_task_retry_count';
    END IF;
    IF EXISTS (SELECT 1 FROM `model_usage` WHERE NOT(task_type IN ('video_parse', 'chat_retrieval'))) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_model_usage_task_type';
    END IF;
    IF EXISTS (SELECT 1 FROM `chunk_parse_content` WHERE NOT(version > 0)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_chunk_parse_content_version';
    END IF;
    IF EXISTS (SELECT 1 FROM `video` WHERE NOT(duration_seconds > 0)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_video_duration';
    END IF;
    IF EXISTS (SELECT 1 FROM `video` WHERE NOT(episode_order > 0)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_video_episode_order';
    END IF;
    IF EXISTS (SELECT 1 FROM `video` WHERE NOT(total_bytes >= 0)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_video_total_bytes';
    END IF;
    IF EXISTS (SELECT 1 FROM `video` WHERE NOT(parse_progress BETWEEN 0 AND 100)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_video_parse_progress';
    END IF;
    IF EXISTS (SELECT 1 FROM `video` WHERE NOT(upload_progress BETWEEN 0 AND 100)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_video_upload_progress';
    END IF;
    IF EXISTS (SELECT 1 FROM `series_parse_job` WHERE NOT(progress BETWEEN 0 AND 100)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_series_parse_job_progress';
    END IF;
    IF EXISTS (SELECT 1 FROM `series_parse_job` WHERE NOT(lease_version >= 0)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_series_parse_job_lease_version';
    END IF;
    IF EXISTS (SELECT 1 FROM `series_parse_job` WHERE NOT(stage IN ('QUEUED', 'PREPARING', 'SPLIT_READY', 'PARSING', 'TEXT_EMBEDDING', 'VISION_EMBEDDING', 'PERSISTING', 'INDEXING', 'COMPLETED'))) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_series_parse_job_stage';
    END IF;
    IF EXISTS (SELECT 1 FROM `series_parse_job` WHERE NOT(processing_duration_ms >= 0)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_series_parse_job_processing_duration';
    END IF;
    IF EXISTS (SELECT 1 FROM `series_parse_job` WHERE NOT(status IN ('QUEUED', 'RUNNING', 'RETRY_WAIT', 'SUCCEEDED', 'FAILED'))) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_series_parse_job_status';
    END IF;
    IF EXISTS (SELECT 1 FROM `series_parse_job` WHERE NOT(attempt_count >= 0)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_series_parse_job_attempt_count';
    END IF;
    IF EXISTS (SELECT 1 FROM `series_parse_job` WHERE NOT(version >= 1)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_series_parse_job_version';
    END IF;
    IF EXISTS (SELECT 1 FROM `series_parse_job` WHERE NOT(max_attempts > 0)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_series_parse_job_max_attempts';
    END IF;
    IF EXISTS (SELECT 1 FROM `tenant` WHERE NOT(type IN ('local', 'public', 'personal'))) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_tenant_type';
    END IF;
    IF EXISTS (SELECT 1 FROM `tenant` WHERE NOT(type = 'public' OR owner_user_id IS NOT NULL)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_owned_tenant_owner';
    END IF;
    IF EXISTS (SELECT 1 FROM `tenant` WHERE NOT(type <> 'public' OR owner_user_id IS NULL)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_public_tenant_owner';
    END IF;
    IF EXISTS (SELECT 1 FROM `tenant` WHERE NOT(status IN ('ACTIVE', 'DISABLED'))) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_tenant_status';
    END IF;
    IF EXISTS (SELECT 1 FROM `upload_session` WHERE NOT(completion_stage IS NULL OR completion_stage IN ('VERIFYING', 'ASSEMBLING', 'PROBING', 'CREATING_JOB'))) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_upload_session_completion_stage';
    END IF;
    IF EXISTS (SELECT 1 FROM `upload_session` WHERE NOT(upload_sequence > 0)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_upload_session_upload_sequence';
    END IF;
    IF EXISTS (SELECT 1 FROM `upload_session` WHERE NOT(preferred_segment_size_bytes > 0)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_upload_session_segment_size';
    END IF;
    IF EXISTS (SELECT 1 FROM `upload_session` WHERE NOT(status IN ('UPLOADING', 'COMPLETING', 'COMPLETED', 'EXPIRED'))) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_upload_session_status';
    END IF;
    IF EXISTS (SELECT 1 FROM `upload_session` WHERE NOT(total_segments > 0)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_upload_session_total_segments';
    END IF;
    IF EXISTS (SELECT 1 FROM `upload_session` WHERE NOT(episode_order > 0)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_upload_session_episode_order';
    END IF;
    IF EXISTS (SELECT 1 FROM `upload_session` WHERE NOT(version > 0)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_upload_session_version';
    END IF;
    IF EXISTS (SELECT 1 FROM `upload_session` WHERE NOT(total_size_bytes > 0)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_upload_session_total_size';
    END IF;
    IF EXISTS (SELECT 1 FROM `upload_segment` WHERE NOT(start_byte >= 0 AND end_byte_exclusive > start_byte)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_upload_segment_range';
    END IF;
    IF EXISTS (SELECT 1 FROM `upload_segment` WHERE NOT(segment_number >= 0)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_upload_segment_number';
    END IF;
    IF EXISTS (SELECT 1 FROM `upload_segment` WHERE NOT(actual_size_bytes = end_byte_exclusive - start_byte)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_upload_segment_size';
    END IF;
    IF EXISTS (SELECT 1 FROM `video_chunk` WHERE NOT(sequence_no >= 0)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_video_chunk_sequence';
    END IF;
    IF EXISTS (SELECT 1 FROM `video_chunk` WHERE NOT(start_seconds >= 0 AND end_seconds > start_seconds)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_video_chunk_range';
    END IF;
    IF EXISTS (SELECT 1 FROM `video_chunk` WHERE NOT(analysis_window_start_seconds <= start_seconds AND analysis_window_end_seconds >= end_seconds)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate ck_video_chunk_analysis_range';
    END IF;
    IF EXISTS (SELECT 1 FROM `interval_parse_task` child WHERE child.`chunk_parse_task_id` IS NOT NULL AND child.`video_parse_task_id` IS NOT NULL
            AND NOT EXISTS (SELECT 1 FROM `chunk_parse_task` parent WHERE parent.`id`=child.`chunk_parse_task_id` AND parent.`video_parse_task_id`=child.`video_parse_task_id`)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate interval_parse_task FK to chunk_parse_task';
    END IF;
    IF EXISTS (SELECT 1 FROM `interval_parse_task` child WHERE child.`video_parse_task_id` IS NOT NULL
            AND NOT EXISTS (SELECT 1 FROM `video_parse_task` parent WHERE parent.`id`=child.`video_parse_task_id`)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate interval_parse_task FK to video_parse_task';
    END IF;
    IF EXISTS (SELECT 1 FROM `video_parse_task` child WHERE child.`video_id` IS NOT NULL
            AND NOT EXISTS (SELECT 1 FROM `video` parent WHERE parent.`video_id`=child.`video_id`)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate video_parse_task FK to video';
    END IF;
    IF EXISTS (SELECT 1 FROM `parse_model_call` child WHERE child.`video_parse_task_id` IS NOT NULL
            AND NOT EXISTS (SELECT 1 FROM `video_parse_task` parent WHERE parent.`id`=child.`video_parse_task_id`)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate parse_model_call FK to video_parse_task';
    END IF;
    IF EXISTS (SELECT 1 FROM `parse_model_call` child WHERE child.`job_id` IS NOT NULL
            AND NOT EXISTS (SELECT 1 FROM `series_parse_job` parent WHERE parent.`job_id`=child.`job_id`)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate parse_model_call FK to series_parse_job';
    END IF;
    IF EXISTS (SELECT 1 FROM `agent_message` child WHERE child.`conversation_id` IS NOT NULL
            AND NOT EXISTS (SELECT 1 FROM `agent_conversation` parent WHERE parent.`conversation_id`=child.`conversation_id`)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate agent_message FK to agent_conversation';
    END IF;
    IF EXISTS (SELECT 1 FROM `video_chunk_checkpoint` child WHERE child.`job_id` IS NOT NULL
            AND NOT EXISTS (SELECT 1 FROM `series_parse_job` parent WHERE parent.`job_id`=child.`job_id`)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate video_chunk_checkpoint FK to series_parse_job';
    END IF;
    IF EXISTS (SELECT 1 FROM `video_chunk_info` child WHERE child.`video_chunk_id` IS NOT NULL
            AND NOT EXISTS (SELECT 1 FROM `video_chunk` parent WHERE parent.`video_chunk_id`=child.`video_chunk_id`)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate video_chunk_info FK to video_chunk';
    END IF;
    IF EXISTS (SELECT 1 FROM `index_sync_intent` child WHERE child.`video_chunk_id` IS NOT NULL
            AND NOT EXISTS (SELECT 1 FROM `video_chunk` parent WHERE parent.`video_chunk_id`=child.`video_chunk_id`)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate index_sync_intent FK to video_chunk';
    END IF;
    IF EXISTS (SELECT 1 FROM `video_parse_usage_attempt` child WHERE child.`video_parse_task_id` IS NOT NULL
            AND NOT EXISTS (SELECT 1 FROM `video_parse_task` parent WHERE parent.`id`=child.`video_parse_task_id`)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate video_parse_usage_attempt FK to video_parse_task';
    END IF;
    IF EXISTS (SELECT 1 FROM `context_snapshot` child WHERE child.`series_id` IS NOT NULL
            AND NOT EXISTS (SELECT 1 FROM `series` parent WHERE parent.`series_id`=child.`series_id`)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate context_snapshot FK to series';
    END IF;
    IF EXISTS (SELECT 1 FROM `context_snapshot` child WHERE child.`created_from_video_chunk_id` IS NOT NULL
            AND NOT EXISTS (SELECT 1 FROM `video_chunk` parent WHERE parent.`video_chunk_id`=child.`created_from_video_chunk_id`)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate context_snapshot FK to video_chunk';
    END IF;
    IF EXISTS (SELECT 1 FROM `context_snapshot` child WHERE child.`video_id` IS NOT NULL
            AND NOT EXISTS (SELECT 1 FROM `video` parent WHERE parent.`video_id`=child.`video_id`)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate context_snapshot FK to video';
    END IF;
    IF EXISTS (SELECT 1 FROM `chunk_parse_task` child WHERE child.`video_parse_task_id` IS NOT NULL
            AND NOT EXISTS (SELECT 1 FROM `video_parse_task` parent WHERE parent.`id`=child.`video_parse_task_id`)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate chunk_parse_task FK to video_parse_task';
    END IF;
    IF EXISTS (SELECT 1 FROM `chunk_parse_task` child WHERE child.`video_chunk_id` IS NOT NULL
            AND NOT EXISTS (SELECT 1 FROM `video_chunk` parent WHERE parent.`video_chunk_id`=child.`video_chunk_id`)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate chunk_parse_task FK to video_chunk';
    END IF;
    IF EXISTS (SELECT 1 FROM `interval_parse_content` child WHERE child.`interval_parse_task_id` IS NOT NULL
            AND NOT EXISTS (SELECT 1 FROM `interval_parse_task` parent WHERE parent.`id`=child.`interval_parse_task_id`)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate interval_parse_content FK to interval_parse_task';
    END IF;
    IF EXISTS (SELECT 1 FROM `chunk_parse_content` child WHERE child.`chunk_parse_task_id` IS NOT NULL
            AND NOT EXISTS (SELECT 1 FROM `chunk_parse_task` parent WHERE parent.`id`=child.`chunk_parse_task_id`)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate chunk_parse_content FK to chunk_parse_task';
    END IF;
    IF EXISTS (SELECT 1 FROM `video` child WHERE child.`series_id` IS NOT NULL
            AND NOT EXISTS (SELECT 1 FROM `series` parent WHERE parent.`series_id`=child.`series_id`)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate video FK to series';
    END IF;
    IF EXISTS (SELECT 1 FROM `series_parse_job` child WHERE child.`series_id` IS NOT NULL
            AND NOT EXISTS (SELECT 1 FROM `series` parent WHERE parent.`series_id`=child.`series_id`)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate series_parse_job FK to series';
    END IF;
    IF EXISTS (SELECT 1 FROM `series_parse_job` child WHERE child.`video_id` IS NOT NULL
            AND NOT EXISTS (SELECT 1 FROM `video` parent WHERE parent.`video_id`=child.`video_id`)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate series_parse_job FK to video';
    END IF;
    IF EXISTS (SELECT 1 FROM `tenant` child WHERE child.`owner_user_id` IS NOT NULL
            AND NOT EXISTS (SELECT 1 FROM `user` parent WHERE parent.`user_id`=child.`owner_user_id`)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate tenant FK to user';
    END IF;
    IF EXISTS (SELECT 1 FROM `upload_session` child WHERE child.`job_id` IS NOT NULL
            AND NOT EXISTS (SELECT 1 FROM `series_parse_job` parent WHERE parent.`job_id`=child.`job_id`)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate upload_session FK to series_parse_job';
    END IF;
    IF EXISTS (SELECT 1 FROM `upload_session` child WHERE child.`series_id` IS NOT NULL
            AND NOT EXISTS (SELECT 1 FROM `series` parent WHERE parent.`series_id`=child.`series_id`)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate upload_session FK to series';
    END IF;
    IF EXISTS (SELECT 1 FROM `upload_session` child WHERE child.`initial_video_parse_task_id` IS NOT NULL
            AND NOT EXISTS (SELECT 1 FROM `video_parse_task` parent WHERE parent.`id`=child.`initial_video_parse_task_id`)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate upload_session FK to video_parse_task';
    END IF;
    IF EXISTS (SELECT 1 FROM `upload_session` child WHERE child.`video_id` IS NOT NULL
            AND NOT EXISTS (SELECT 1 FROM `video` parent WHERE parent.`video_id`=child.`video_id`)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate upload_session FK to video';
    END IF;
    IF EXISTS (SELECT 1 FROM `upload_segment` child WHERE child.`upload_session_id` IS NOT NULL
            AND NOT EXISTS (SELECT 1 FROM `upload_session` parent WHERE parent.`upload_session_id`=child.`upload_session_id`)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate upload_segment FK to upload_session';
    END IF;
    IF EXISTS (SELECT 1 FROM `parse_usage_attempt` child WHERE child.`job_id` IS NOT NULL
            AND NOT EXISTS (SELECT 1 FROM `series_parse_job` parent WHERE parent.`job_id`=child.`job_id`)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate parse_usage_attempt FK to series_parse_job';
    END IF;
    IF EXISTS (SELECT 1 FROM `parse_usage_attempt` child WHERE child.`video_parse_task_id` IS NOT NULL
            AND NOT EXISTS (SELECT 1 FROM `video_parse_task` parent WHERE parent.`id`=child.`video_parse_task_id`)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate parse_usage_attempt FK to video_parse_task';
    END IF;
    IF EXISTS (SELECT 1 FROM `video_chunk` child WHERE child.`video_id` IS NOT NULL
            AND NOT EXISTS (SELECT 1 FROM `video` parent WHERE parent.`video_id`=child.`video_id`)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate video_chunk FK to video';
    END IF;
    IF EXISTS (SELECT 1 FROM `video_chunk_embedding` child WHERE child.`video_chunk_id` IS NOT NULL
            AND NOT EXISTS (SELECT 1 FROM `video_chunk` parent WHERE parent.`video_chunk_id`=child.`video_chunk_id`)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'source rows violate video_chunk_embedding FK to video_chunk';
    END IF;
    -- Capture original row counts only after the complete source preflight.
    UPDATE _bm_tables SET rows_before=(SELECT COUNT(*) FROM `agent_conversation`) WHERE table_name='agent_conversation';
    UPDATE _bm_tables SET rows_before=(SELECT COUNT(*) FROM `model_usage`) WHERE table_name='model_usage';
    UPDATE _bm_tables SET rows_before=(SELECT COUNT(*) FROM `series`) WHERE table_name='series';
    UPDATE _bm_tables SET rows_before=(SELECT COUNT(*) FROM `user`) WHERE table_name='user';
    UPDATE _bm_tables SET rows_before=(SELECT COUNT(*) FROM `agent_message`) WHERE table_name='agent_message';
    UPDATE _bm_tables SET rows_before=(SELECT COUNT(*) FROM `tenant`) WHERE table_name='tenant';
    UPDATE _bm_tables SET rows_before=(SELECT COUNT(*) FROM `video`) WHERE table_name='video';
    UPDATE _bm_tables SET rows_before=(SELECT COUNT(*) FROM `series_parse_job`) WHERE table_name='series_parse_job';
    UPDATE _bm_tables SET rows_before=(SELECT COUNT(*) FROM `video_chunk`) WHERE table_name='video_chunk';
    UPDATE _bm_tables SET rows_before=(SELECT COUNT(*) FROM `video_parse_task`) WHERE table_name='video_parse_task';
    UPDATE _bm_tables SET rows_before=(SELECT COUNT(*) FROM `chunk_parse_task`) WHERE table_name='chunk_parse_task';
    UPDATE _bm_tables SET rows_before=(SELECT COUNT(*) FROM `context_snapshot`) WHERE table_name='context_snapshot';
    UPDATE _bm_tables SET rows_before=(SELECT COUNT(*) FROM `index_sync_intent`) WHERE table_name='index_sync_intent';
    UPDATE _bm_tables SET rows_before=(SELECT COUNT(*) FROM `parse_model_call`) WHERE table_name='parse_model_call';
    UPDATE _bm_tables SET rows_before=(SELECT COUNT(*) FROM `parse_usage_attempt`) WHERE table_name='parse_usage_attempt';
    UPDATE _bm_tables SET rows_before=(SELECT COUNT(*) FROM `upload_session`) WHERE table_name='upload_session';
    UPDATE _bm_tables SET rows_before=(SELECT COUNT(*) FROM `video_chunk_checkpoint`) WHERE table_name='video_chunk_checkpoint';
    UPDATE _bm_tables SET rows_before=(SELECT COUNT(*) FROM `video_chunk_embedding`) WHERE table_name='video_chunk_embedding';
    UPDATE _bm_tables SET rows_before=(SELECT COUNT(*) FROM `video_chunk_info`) WHERE table_name='video_chunk_info';
    UPDATE _bm_tables SET rows_before=(SELECT COUNT(*) FROM `video_parse_usage_attempt`) WHERE table_name='video_parse_usage_attempt';
    UPDATE _bm_tables SET rows_before=(SELECT COUNT(*) FROM `chunk_parse_content`) WHERE table_name='chunk_parse_content';
    UPDATE _bm_tables SET rows_before=(SELECT COUNT(*) FROM `interval_parse_task`) WHERE table_name='interval_parse_task';
    UPDATE _bm_tables SET rows_before=(SELECT COUNT(*) FROM `upload_segment`) WHERE table_name='upload_segment';
    UPDATE _bm_tables SET rows_before=(SELECT COUNT(*) FROM `interval_parse_content`) WHERE table_name='interval_parse_content';

    -- First business DDL: drop the captured physical FKs with checks still ON.
    BEGIN
        DECLARE finished INT DEFAULT 0;
        DECLARE source_table VARCHAR(64);
        DECLARE physical_fk VARCHAR(64);
        DECLARE fk_cursor CURSOR FOR SELECT table_name,constraint_name FROM _bm_actual_fks ORDER BY table_name,constraint_name;
        DECLARE CONTINUE HANDLER FOR NOT FOUND SET finished=1;
        OPEN fk_cursor;
        drop_fks: LOOP
            FETCH fk_cursor INTO source_table,physical_fk;
            IF finished=1 THEN LEAVE drop_fks; END IF;
            SET @bm_sql=CONCAT('ALTER TABLE `',REPLACE(source_table,'`','``'),'` DROP FOREIGN KEY `',REPLACE(physical_fk,'`','``'),'`');
            PREPARE bm_stmt FROM @bm_sql; EXECUTE bm_stmt; DEALLOCATE PREPARE bm_stmt;
        END LOOP;
        CLOSE fk_cursor;
    END;
    -- MySQL may reject renaming a CHECK-referenced column. Rebuild every named
    -- CHECK from the target model, after values/columns reach their target form.
    ALTER TABLE `agent_conversation` DROP CHECK `ck_agent_conversation_next_message_sequence`;
    ALTER TABLE `model_usage` DROP CHECK `ck_model_usage_task_type`;
    ALTER TABLE `user` DROP CHECK `ck_user_auth_version`, DROP CHECK `ck_guest_email`, DROP CHECK `ck_guest_role`, DROP CHECK `ck_user_status`, DROP CHECK `ck_user_role`;
    ALTER TABLE `agent_message` DROP CHECK `ck_agent_message_version`, DROP CHECK `ck_agent_message_status`, DROP CHECK `ck_agent_message_sequence`, DROP CHECK `ck_agent_message_phase`, DROP CHECK `ck_agent_message_requested_top_k`, DROP CHECK `ck_agent_message_role`;
    ALTER TABLE `tenant` DROP CHECK `ck_tenant_type`, DROP CHECK `ck_owned_tenant_owner`, DROP CHECK `ck_public_tenant_owner`, DROP CHECK `ck_tenant_status`;
    ALTER TABLE `video` DROP CHECK `ck_video_duration`, DROP CHECK `ck_video_episode_order`, DROP CHECK `ck_video_total_bytes`, DROP CHECK `ck_video_parse_progress`, DROP CHECK `ck_video_upload_progress`;
    ALTER TABLE `series_parse_job` DROP CHECK `ck_series_parse_job_progress`, DROP CHECK `ck_series_parse_job_lease_version`, DROP CHECK `ck_series_parse_job_stage`, DROP CHECK `ck_series_parse_job_processing_duration`, DROP CHECK `ck_series_parse_job_status`, DROP CHECK `ck_series_parse_job_attempt_count`, DROP CHECK `ck_series_parse_job_version`, DROP CHECK `ck_series_parse_job_max_attempts`;
    ALTER TABLE `video_chunk` DROP CHECK `ck_video_chunk_sequence`, DROP CHECK `ck_video_chunk_range`, DROP CHECK `ck_video_chunk_analysis_range`;
    ALTER TABLE `video_parse_task` DROP CHECK `ck_video_parse_task_attempt_count`, DROP CHECK `ck_video_parse_task_lease_state`, DROP CHECK `ck_video_parse_task_chunk_count`, DROP CHECK `ck_video_parse_task_max_attempts`, DROP CHECK `ck_video_parse_task_lease_version`, DROP CHECK `ck_video_parse_task_version`, DROP CHECK `ck_video_parse_task_status`, DROP CHECK `ck_video_parse_task_processing_duration`;
    ALTER TABLE `chunk_parse_task` DROP CHECK `ck_chunk_parse_task_lease_version`, DROP CHECK `ck_chunk_parse_task_status`, DROP CHECK `ck_chunk_parse_task_lease_state`, DROP CHECK `ck_chunk_parse_task_retry_count`;
    ALTER TABLE `context_snapshot` DROP CHECK `ck_context_snapshot_sequence`;
    ALTER TABLE `index_sync_intent` DROP CHECK `ck_index_sync_intent_generation`, DROP CHECK `ck_index_sync_intent_operation`, DROP CHECK `ck_index_sync_intent_status`, DROP CHECK `ck_index_sync_intent_attempt_count`;
    ALTER TABLE `parse_model_call` DROP CHECK `ck_parse_model_call_exactly_one_task`, DROP CHECK `ck_parse_model_call_category`, DROP CHECK `ck_parse_model_call_execution_identity`, DROP CHECK `ck_parse_model_call_status`, DROP CHECK `ck_parse_model_call_execution_kind`;
    ALTER TABLE `upload_session` DROP CHECK `ck_upload_session_completion_stage`, DROP CHECK `ck_upload_session_upload_sequence`, DROP CHECK `ck_upload_session_segment_size`, DROP CHECK `ck_upload_session_status`, DROP CHECK `ck_upload_session_total_segments`, DROP CHECK `ck_upload_session_episode_order`, DROP CHECK `ck_upload_session_version`, DROP CHECK `ck_upload_session_total_size`;
    ALTER TABLE `video_chunk_checkpoint` DROP CHECK `ck_video_chunk_checkpoint_range`, DROP CHECK `ck_video_chunk_checkpoint_sequence`, DROP CHECK `ck_video_chunk_checkpoint_stage`;
    ALTER TABLE `video_parse_usage_attempt` DROP CHECK `ck_video_parse_usage_attempt_execution_kind`;
    ALTER TABLE `chunk_parse_content` DROP CHECK `ck_chunk_parse_content_version`;
    ALTER TABLE `interval_parse_task` DROP CHECK `ck_interval_parse_task_lease_version`, DROP CHECK `ck_interval_parse_task_ready_mask`, DROP CHECK `ck_interval_parse_task_lease_state`, DROP CHECK `ck_interval_parse_task_status`, DROP CHECK `ck_interval_parse_task_retry_count`, DROP CHECK `ck_interval_parse_task_start_sequence`, DROP CHECK `ck_interval_parse_task_chunk_count`;
    ALTER TABLE `upload_segment` DROP CHECK `ck_upload_segment_range`, DROP CHECK `ck_upload_segment_number`, DROP CHECK `ck_upload_segment_size`;

    -- Rename business fields, build nullable audit pairs, then backfill facts.
    -- agent_conversation
    ALTER TABLE `agent_conversation`
        RENAME COLUMN `owner_user_id` TO `owner_id`,
        RENAME COLUMN `last_message_at` TO `last_message_time`,
        RENAME COLUMN `conversation_id` TO `id`,
        CHANGE COLUMN `created_at` `create_time` BIGINT NULL,
        ADD COLUMN `create_date` DATETIME(3) NULL,
        CHANGE COLUMN `updated_at` `update_time` BIGINT NULL,
        ADD COLUMN `update_date` DATETIME(3) NULL;
    UPDATE `agent_conversation` SET
        `create_date`=DATE_ADD('1970-01-01 00:00:00.000', INTERVAL `create_time` * 1000 MICROSECOND),
        `update_date`=DATE_ADD('1970-01-01 00:00:00.000', INTERVAL `update_time` * 1000 MICROSECOND);
    CREATE INDEX ix_agent_conversation_create_date ON agent_conversation (create_date);
    CREATE INDEX ix_agent_conversation_create_time ON agent_conversation (create_time);
    CREATE INDEX ix_agent_conversation_update_date ON agent_conversation (update_date);
    CREATE INDEX ix_agent_conversation_update_time ON agent_conversation (update_time);
    -- model_usage
    ALTER TABLE `model_usage`
        RENAME COLUMN `actor_user_id` TO `user_id`,
        RENAME COLUMN `started_at` TO `start_time`,
        RENAME COLUMN `call_id` TO `id`,
        CHANGE COLUMN `created_at` `create_date` DATETIME(3) NULL,
        ADD COLUMN `create_time` BIGINT NULL,
        ADD COLUMN `update_time` BIGINT NULL,
        ADD COLUMN `update_date` DATETIME(3) NULL;
    UPDATE `model_usage` SET
        `create_time`=TIMESTAMPDIFF(MICROSECOND, '1970-01-01 00:00:00.000', `create_date`) DIV 1000;
    CREATE INDEX ix_model_usage_create_date ON model_usage (create_date);
    CREATE INDEX ix_model_usage_create_time ON model_usage (create_time);
    CREATE INDEX ix_model_usage_update_date ON model_usage (update_date);
    CREATE INDEX ix_model_usage_update_time ON model_usage (update_time);
    -- series
    ALTER TABLE `series`
        RENAME COLUMN `series_id` TO `id`,
        CHANGE COLUMN `created_at` `create_time` BIGINT NULL,
        ADD COLUMN `create_date` DATETIME(3) NULL,
        CHANGE COLUMN `updated_at` `update_time` BIGINT NULL,
        ADD COLUMN `update_date` DATETIME(3) NULL;
    UPDATE `series` SET
        `create_date`=DATE_ADD('1970-01-01 00:00:00.000', INTERVAL `create_time` * 1000 MICROSECOND),
        `update_date`=DATE_ADD('1970-01-01 00:00:00.000', INTERVAL `update_time` * 1000 MICROSECOND);
    CREATE INDEX ix_series_create_date ON series (create_date);
    CREATE INDEX ix_series_create_time ON series (create_time);
    CREATE INDEX ix_series_update_date ON series (update_date);
    CREATE INDEX ix_series_update_time ON series (update_time);
    -- user
    ALTER TABLE `user`
        RENAME COLUMN `user_id` TO `id`,
        RENAME COLUMN `is_guest` TO `is_anonymous`,
        CHANGE COLUMN `created_at` `create_time` BIGINT NULL,
        ADD COLUMN `create_date` DATETIME(3) NULL,
        CHANGE COLUMN `updated_at` `update_time` BIGINT NULL,
        ADD COLUMN `update_date` DATETIME(3) NULL,
        ADD COLUMN `is_superuser` BOOLEAN NULL,
        ADD COLUMN `nickname` VARCHAR(100) NULL,
        ADD COLUMN `avatar` TEXT NULL,
        ADD COLUMN `language` VARCHAR(32) NULL,
        ADD COLUMN `color_schema` VARCHAR(32) NULL,
        ADD COLUMN `timezone` VARCHAR(64) NULL,
        ADD COLUMN `last_login_time` DATETIME(3) NULL,
        ADD COLUMN `login_channel` VARCHAR(32) NULL;
    UPDATE `user` SET
        `create_date`=DATE_ADD('1970-01-01 00:00:00.000', INTERVAL `create_time` * 1000 MICROSECOND),
        `update_date`=DATE_ADD('1970-01-01 00:00:00.000', INTERVAL `update_time` * 1000 MICROSECOND),
        `is_superuser`=(BINARY `role`=BINARY 'admin'),
        `nickname`=CASE WHEN `id`='local-user' THEN '本地用户' WHEN `is_anonymous`=1 THEN '游客' ELSE LEFT(COALESCE(NULLIF(SUBSTRING_INDEX(`email`, '@', 1), ''), '用户'), 100) END;
    ALTER TABLE `user` DROP COLUMN `role`, MODIFY COLUMN `is_superuser` BOOLEAN NOT NULL, MODIFY COLUMN `nickname` VARCHAR(100) NOT NULL;
    CREATE INDEX ix_user_create_date ON user (create_date);
    CREATE INDEX ix_user_create_time ON user (create_time);
    CREATE INDEX ix_user_last_login_time ON user (last_login_time);
    CREATE INDEX ix_user_nickname ON user (nickname);
    CREATE INDEX ix_user_update_date ON user (update_date);
    CREATE INDEX ix_user_update_time ON user (update_time);
    -- agent_message
    ALTER TABLE `agent_message`
        RENAME COLUMN `model_name` TO `model`,
        RENAME COLUMN `message_id` TO `id`,
        CHANGE COLUMN `created_at` `create_time` BIGINT NULL,
        ADD COLUMN `create_date` DATETIME(3) NULL,
        CHANGE COLUMN `updated_at` `update_time` BIGINT NULL,
        ADD COLUMN `update_date` DATETIME(3) NULL;
    UPDATE `agent_message` SET
        `create_date`=DATE_ADD('1970-01-01 00:00:00.000', INTERVAL `create_time` * 1000 MICROSECOND),
        `update_date`=DATE_ADD('1970-01-01 00:00:00.000', INTERVAL `update_time` * 1000 MICROSECOND);
    CREATE INDEX ix_agent_message_create_date ON agent_message (create_date);
    CREATE INDEX ix_agent_message_create_time ON agent_message (create_time);
    CREATE INDEX ix_agent_message_update_date ON agent_message (update_date);
    CREATE INDEX ix_agent_message_update_time ON agent_message (update_time);
    -- tenant
    ALTER TABLE `tenant`
        RENAME COLUMN `owner_user_id` TO `owner_id`,
        RENAME COLUMN `tenant_id` TO `id`,
        CHANGE COLUMN `created_at` `create_time` BIGINT NULL,
        ADD COLUMN `create_date` DATETIME(3) NULL,
        CHANGE COLUMN `updated_at` `update_time` BIGINT NULL,
        ADD COLUMN `update_date` DATETIME(3) NULL,
        ADD COLUMN `description` TEXT NULL,
        ADD COLUMN `avatar` TEXT NULL,
        RENAME INDEX `ix_tenant_owner_user_id` TO `ix_tenant_owner_id`;
    UPDATE `tenant` SET
        `create_date`=DATE_ADD('1970-01-01 00:00:00.000', INTERVAL `create_time` * 1000 MICROSECOND),
        `update_date`=DATE_ADD('1970-01-01 00:00:00.000', INTERVAL `update_time` * 1000 MICROSECOND);
    CREATE INDEX ix_tenant_create_date ON tenant (create_date);
    CREATE INDEX ix_tenant_create_time ON tenant (create_time);
    CREATE INDEX ix_tenant_update_date ON tenant (update_date);
    CREATE INDEX ix_tenant_update_time ON tenant (update_time);
    -- video
    ALTER TABLE `video`
        RENAME COLUMN `original_filename` TO `filename`,
        RENAME COLUMN `original_source_uri` TO `source_uri`,
        RENAME COLUMN `inference_proxy_uri` TO `proxy_uri`,
        RENAME COLUMN `content_hash` TO `hash`,
        RENAME COLUMN `total_bytes` TO `size_bytes`,
        RENAME COLUMN `duration_seconds` TO `duration_ms`,
        RENAME COLUMN `video_id` TO `id`,
        CHANGE COLUMN `created_at` `create_date` DATETIME(3) NULL,
        ADD COLUMN `create_time` BIGINT NULL,
        CHANGE COLUMN `updated_at` `update_date` DATETIME(3) NULL,
        ADD COLUMN `update_time` BIGINT NULL;
    UPDATE `video` SET
        `create_time`=TIMESTAMPDIFF(MICROSECOND, '1970-01-01 00:00:00.000', `create_date`) DIV 1000,
        `update_time`=TIMESTAMPDIFF(MICROSECOND, '1970-01-01 00:00:00.000', `update_date`) DIV 1000;
    CREATE INDEX ix_video_create_date ON video (create_date);
    CREATE INDEX ix_video_create_time ON video (create_time);
    CREATE INDEX ix_video_update_date ON video (update_date);
    CREATE INDEX ix_video_update_time ON video (update_time);
    -- series_parse_job
    ALTER TABLE `series_parse_job`
        RENAME COLUMN `created_by_user_id` TO `creator_id`,
        RENAME COLUMN `available_at` TO `next_execute_time`,
        RENAME COLUMN `lease_expires_at` TO `lease_expire_time`,
        RENAME COLUMN `heartbeat_at` TO `heartbeat_time`,
        RENAME COLUMN `started_at` TO `start_time`,
        RENAME COLUMN `completed_at` TO `finish_time`,
        RENAME COLUMN `job_id` TO `id`,
        CHANGE COLUMN `created_at` `create_time` BIGINT NULL,
        ADD COLUMN `create_date` DATETIME(3) NULL,
        CHANGE COLUMN `updated_at` `update_time` BIGINT NULL,
        ADD COLUMN `update_date` DATETIME(3) NULL;
    UPDATE `series_parse_job` SET
        `create_date`=DATE_ADD('1970-01-01 00:00:00.000', INTERVAL `create_time` * 1000 MICROSECOND),
        `update_date`=DATE_ADD('1970-01-01 00:00:00.000', INTERVAL `update_time` * 1000 MICROSECOND);
    CREATE INDEX ix_series_parse_job_create_date ON series_parse_job (create_date);
    CREATE INDEX ix_series_parse_job_create_time ON series_parse_job (create_time);
    CREATE INDEX ix_series_parse_job_update_date ON series_parse_job (update_date);
    CREATE INDEX ix_series_parse_job_update_time ON series_parse_job (update_time);
    -- video_chunk
    ALTER TABLE `video_chunk`
        RENAME COLUMN `video_chunk_id` TO `id`,
        RENAME COLUMN `start_seconds` TO `start_ms`,
        RENAME COLUMN `end_seconds` TO `end_ms`,
        RENAME COLUMN `analysis_window_start_seconds` TO `window_start_ms`,
        RENAME COLUMN `analysis_window_end_seconds` TO `window_end_ms`,
        CHANGE COLUMN `created_at` `create_date` DATETIME(3) NULL,
        ADD COLUMN `create_time` BIGINT NULL,
        CHANGE COLUMN `updated_at` `update_date` DATETIME(3) NULL,
        ADD COLUMN `update_time` BIGINT NULL;
    UPDATE `video_chunk` SET
        `create_time`=TIMESTAMPDIFF(MICROSECOND, '1970-01-01 00:00:00.000', `create_date`) DIV 1000,
        `update_time`=TIMESTAMPDIFF(MICROSECOND, '1970-01-01 00:00:00.000', `update_date`) DIV 1000;
    CREATE INDEX ix_video_chunk_create_date ON video_chunk (create_date);
    CREATE INDEX ix_video_chunk_create_time ON video_chunk (create_time);
    CREATE INDEX ix_video_chunk_update_date ON video_chunk (update_date);
    CREATE INDEX ix_video_chunk_update_time ON video_chunk (update_time);
    -- video_parse_task
    ALTER TABLE `video_parse_task`
        RENAME COLUMN `created_by_user_id` TO `creator_id`,
        CHANGE COLUMN `create_time` `create_date` DATETIME(3) NULL,
        ADD COLUMN `create_time` BIGINT NULL,
        CHANGE COLUMN `update_time` `update_date` DATETIME(3) NULL,
        ADD COLUMN `update_time` BIGINT NULL;
    UPDATE `video_parse_task` SET
        `create_time`=TIMESTAMPDIFF(MICROSECOND, '1970-01-01 00:00:00.000', `create_date`) DIV 1000,
        `update_time`=TIMESTAMPDIFF(MICROSECOND, '1970-01-01 00:00:00.000', `update_date`) DIV 1000;
    CREATE INDEX ix_video_parse_task_create_date ON video_parse_task (create_date);
    CREATE INDEX ix_video_parse_task_create_time ON video_parse_task (create_time);
    CREATE INDEX ix_video_parse_task_update_date ON video_parse_task (update_date);
    CREATE INDEX ix_video_parse_task_update_time ON video_parse_task (update_time);
    -- chunk_parse_task
    ALTER TABLE `chunk_parse_task`
        RENAME COLUMN `video_parse_task_id` TO `video_task_id`,
        RENAME COLUMN `video_chunk_id` TO `chunk_id`,
        CHANGE COLUMN `create_time` `create_date` DATETIME(3) NULL,
        ADD COLUMN `create_time` BIGINT NULL,
        CHANGE COLUMN `update_time` `update_date` DATETIME(3) NULL,
        ADD COLUMN `update_time` BIGINT NULL;
    UPDATE `chunk_parse_task` SET
        `create_time`=TIMESTAMPDIFF(MICROSECOND, '1970-01-01 00:00:00.000', `create_date`) DIV 1000,
        `update_time`=TIMESTAMPDIFF(MICROSECOND, '1970-01-01 00:00:00.000', `update_date`) DIV 1000;
    CREATE INDEX ix_chunk_parse_task_create_date ON chunk_parse_task (create_date);
    CREATE INDEX ix_chunk_parse_task_create_time ON chunk_parse_task (create_time);
    CREATE INDEX ix_chunk_parse_task_update_date ON chunk_parse_task (update_date);
    CREATE INDEX ix_chunk_parse_task_update_time ON chunk_parse_task (update_time);
    -- context_snapshot
    ALTER TABLE `context_snapshot`
        RENAME COLUMN `created_from_video_chunk_id` TO `source_chunk_id`,
        RENAME COLUMN `snapshot_id` TO `id`,
        CHANGE COLUMN `created_at` `create_time` BIGINT NULL,
        ADD COLUMN `create_date` DATETIME(3) NULL,
        ADD COLUMN `update_time` BIGINT NULL,
        ADD COLUMN `update_date` DATETIME(3) NULL;
    UPDATE `context_snapshot` SET
        `create_date`=DATE_ADD('1970-01-01 00:00:00.000', INTERVAL `create_time` * 1000 MICROSECOND);
    CREATE INDEX ix_context_snapshot_create_date ON context_snapshot (create_date);
    CREATE INDEX ix_context_snapshot_create_time ON context_snapshot (create_time);
    CREATE INDEX ix_context_snapshot_update_date ON context_snapshot (update_date);
    CREATE INDEX ix_context_snapshot_update_time ON context_snapshot (update_time);
    -- index_sync_intent
    ALTER TABLE `index_sync_intent`
        RENAME COLUMN `video_chunk_id` TO `chunk_id`,
        RENAME COLUMN `available_at` TO `next_execute_time`,
        RENAME COLUMN `synced_at` TO `sync_time`,
        CHANGE COLUMN `created_at` `create_time` BIGINT NULL,
        ADD COLUMN `create_date` DATETIME(3) NULL,
        CHANGE COLUMN `updated_at` `update_time` BIGINT NULL,
        ADD COLUMN `update_date` DATETIME(3) NULL;
    UPDATE `index_sync_intent` SET
        `create_date`=DATE_ADD('1970-01-01 00:00:00.000', INTERVAL `create_time` * 1000 MICROSECOND),
        `update_date`=DATE_ADD('1970-01-01 00:00:00.000', INTERVAL `update_time` * 1000 MICROSECOND);
    CREATE INDEX ix_index_sync_intent_create_date ON index_sync_intent (create_date);
    CREATE INDEX ix_index_sync_intent_create_time ON index_sync_intent (create_time);
    CREATE INDEX ix_index_sync_intent_update_date ON index_sync_intent (update_date);
    CREATE INDEX ix_index_sync_intent_update_time ON index_sync_intent (update_time);
    -- parse_model_call
    ALTER TABLE `parse_model_call`
        RENAME COLUMN `video_parse_task_id` TO `video_task_id`,
        RENAME COLUMN `video_chunk_id` TO `chunk_id`,
        RENAME COLUMN `started_at` TO `start_time`,
        RENAME COLUMN `call_id` TO `id`,
        CHANGE COLUMN `created_at` `create_date` DATETIME(3) NULL,
        ADD COLUMN `create_time` BIGINT NULL,
        CHANGE COLUMN `updated_at` `update_date` DATETIME(3) NULL,
        ADD COLUMN `update_time` BIGINT NULL;
    UPDATE `parse_model_call` SET
        `create_time`=TIMESTAMPDIFF(MICROSECOND, '1970-01-01 00:00:00.000', `create_date`) DIV 1000,
        `update_time`=TIMESTAMPDIFF(MICROSECOND, '1970-01-01 00:00:00.000', `update_date`) DIV 1000;
    CREATE INDEX ix_parse_model_call_create_date ON parse_model_call (create_date);
    CREATE INDEX ix_parse_model_call_create_time ON parse_model_call (create_time);
    CREATE INDEX ix_parse_model_call_update_date ON parse_model_call (update_date);
    CREATE INDEX ix_parse_model_call_update_time ON parse_model_call (update_time);
    -- parse_usage_attempt
    ALTER TABLE `parse_usage_attempt`
        RENAME COLUMN `video_parse_task_id` TO `video_task_id`,
        RENAME COLUMN `started_at` TO `start_time`,
        ADD COLUMN `create_time` BIGINT NULL,
        ADD COLUMN `create_date` DATETIME(3) NULL,
        ADD COLUMN `update_time` BIGINT NULL,
        ADD COLUMN `update_date` DATETIME(3) NULL,
        RENAME INDEX `ix_parse_usage_attempt_video_parse_task_id` TO `ix_parse_usage_attempt_video_task_id`;
    CREATE INDEX ix_parse_usage_attempt_create_date ON parse_usage_attempt (create_date);
    CREATE INDEX ix_parse_usage_attempt_create_time ON parse_usage_attempt (create_time);
    CREATE INDEX ix_parse_usage_attempt_update_date ON parse_usage_attempt (update_date);
    CREATE INDEX ix_parse_usage_attempt_update_time ON parse_usage_attempt (update_time);
    -- upload_session
    ALTER TABLE `upload_session`
        RENAME COLUMN `upload_session_id` TO `id`,
        RENAME COLUMN `created_by_user_id` TO `creator_id`,
        RENAME COLUMN `original_filename` TO `filename`,
        RENAME COLUMN `total_size_bytes` TO `size_bytes`,
        RENAME COLUMN `preferred_segment_size_bytes` TO `segment_size_bytes`,
        RENAME COLUMN `total_segments` TO `segment_count`,
        RENAME COLUMN `content_sha256` TO `sha256`,
        RENAME COLUMN `completion_started_at` TO `completion_start_time`,
        RENAME COLUMN `expires_at` TO `expire_time`,
        RENAME COLUMN `completed_at` TO `finish_time`,
        RENAME COLUMN `initial_video_parse_task_id` TO `initial_task_id`,
        CHANGE COLUMN `created_at` `create_date` DATETIME(3) NULL,
        ADD COLUMN `create_time` BIGINT NULL,
        CHANGE COLUMN `updated_at` `update_date` DATETIME(3) NULL,
        ADD COLUMN `update_time` BIGINT NULL,
        RENAME INDEX `ix_upload_session_initial_video_parse_task_id` TO `ix_upload_session_initial_task_id`;
    UPDATE `upload_session` SET
        `create_time`=TIMESTAMPDIFF(MICROSECOND, '1970-01-01 00:00:00.000', `create_date`) DIV 1000,
        `update_time`=TIMESTAMPDIFF(MICROSECOND, '1970-01-01 00:00:00.000', `update_date`) DIV 1000;
    CREATE INDEX ix_upload_session_create_date ON upload_session (create_date);
    CREATE INDEX ix_upload_session_create_time ON upload_session (create_time);
    CREATE INDEX ix_upload_session_update_date ON upload_session (update_date);
    CREATE INDEX ix_upload_session_update_time ON upload_session (update_time);
    -- video_chunk_checkpoint
    ALTER TABLE `video_chunk_checkpoint`
        RENAME COLUMN `video_chunk_id` TO `chunk_id`,
        RENAME COLUMN `chunk_start_seconds` TO `start_ms`,
        RENAME COLUMN `chunk_end_seconds` TO `end_ms`,
        ADD COLUMN `create_time` BIGINT NULL,
        ADD COLUMN `create_date` DATETIME(3) NULL,
        CHANGE COLUMN `updated_at` `update_time` BIGINT NULL,
        ADD COLUMN `update_date` DATETIME(3) NULL;
    UPDATE `video_chunk_checkpoint` SET
        `update_date`=DATE_ADD('1970-01-01 00:00:00.000', INTERVAL `update_time` * 1000 MICROSECOND);
    CREATE INDEX ix_video_chunk_checkpoint_create_date ON video_chunk_checkpoint (create_date);
    CREATE INDEX ix_video_chunk_checkpoint_create_time ON video_chunk_checkpoint (create_time);
    CREATE INDEX ix_video_chunk_checkpoint_update_date ON video_chunk_checkpoint (update_date);
    CREATE INDEX ix_video_chunk_checkpoint_update_time ON video_chunk_checkpoint (update_time);
    -- video_chunk_embedding
    ALTER TABLE `video_chunk_embedding`
        RENAME COLUMN `video_chunk_id` TO `chunk_id`,
        CHANGE COLUMN `created_at` `create_time` BIGINT NULL,
        ADD COLUMN `create_date` DATETIME(3) NULL,
        CHANGE COLUMN `updated_at` `update_time` BIGINT NULL,
        ADD COLUMN `update_date` DATETIME(3) NULL;
    UPDATE `video_chunk_embedding` SET
        `create_date`=DATE_ADD('1970-01-01 00:00:00.000', INTERVAL `create_time` * 1000 MICROSECOND),
        `update_date`=DATE_ADD('1970-01-01 00:00:00.000', INTERVAL `update_time` * 1000 MICROSECOND);
    CREATE INDEX ix_video_chunk_embedding_create_date ON video_chunk_embedding (create_date);
    CREATE INDEX ix_video_chunk_embedding_create_time ON video_chunk_embedding (create_time);
    CREATE INDEX ix_video_chunk_embedding_update_date ON video_chunk_embedding (update_date);
    CREATE INDEX ix_video_chunk_embedding_update_time ON video_chunk_embedding (update_time);
    -- video_chunk_info
    ALTER TABLE `video_chunk_info`
        RENAME COLUMN `video_chunk_id` TO `chunk_id`,
        RENAME COLUMN `model_provider` TO `provider`,
        RENAME COLUMN `model_name` TO `model`,
        RENAME COLUMN `parsed_at` TO `parse_time`,
        ADD COLUMN `create_time` BIGINT NULL,
        ADD COLUMN `create_date` DATETIME(3) NULL,
        ADD COLUMN `update_time` BIGINT NULL,
        ADD COLUMN `update_date` DATETIME(3) NULL;
    CREATE INDEX ix_video_chunk_info_create_date ON video_chunk_info (create_date);
    CREATE INDEX ix_video_chunk_info_create_time ON video_chunk_info (create_time);
    CREATE INDEX ix_video_chunk_info_update_date ON video_chunk_info (update_date);
    CREATE INDEX ix_video_chunk_info_update_time ON video_chunk_info (update_time);
    -- video_parse_usage_attempt
    ALTER TABLE `video_parse_usage_attempt`
        RENAME COLUMN `video_parse_task_id` TO `video_task_id`,
        RENAME COLUMN `started_at` TO `start_time`,
        RENAME COLUMN `attempt_id` TO `id`,
        ADD COLUMN `create_time` BIGINT NULL,
        ADD COLUMN `create_date` DATETIME(3) NULL,
        ADD COLUMN `update_time` BIGINT NULL,
        ADD COLUMN `update_date` DATETIME(3) NULL,
        RENAME INDEX `ix_video_parse_usage_attempt_video_parse_task_id` TO `ix_video_parse_usage_attempt_video_task_id`;
    CREATE INDEX ix_video_parse_usage_attempt_create_date ON video_parse_usage_attempt (create_date);
    CREATE INDEX ix_video_parse_usage_attempt_create_time ON video_parse_usage_attempt (create_time);
    CREATE INDEX ix_video_parse_usage_attempt_update_date ON video_parse_usage_attempt (update_date);
    CREATE INDEX ix_video_parse_usage_attempt_update_time ON video_parse_usage_attempt (update_time);
    -- chunk_parse_content
    ALTER TABLE `chunk_parse_content`
        RENAME COLUMN `chunk_parse_task_id` TO `chunk_task_id`,
        CHANGE COLUMN `create_time` `create_date` DATETIME(3) NULL,
        ADD COLUMN `create_time` BIGINT NULL,
        CHANGE COLUMN `update_time` `update_date` DATETIME(3) NULL,
        ADD COLUMN `update_time` BIGINT NULL;
    UPDATE `chunk_parse_content` SET
        `create_time`=TIMESTAMPDIFF(MICROSECOND, '1970-01-01 00:00:00.000', `create_date`) DIV 1000,
        `update_time`=TIMESTAMPDIFF(MICROSECOND, '1970-01-01 00:00:00.000', `update_date`) DIV 1000;
    CREATE INDEX ix_chunk_parse_content_create_date ON chunk_parse_content (create_date);
    CREATE INDEX ix_chunk_parse_content_create_time ON chunk_parse_content (create_time);
    CREATE INDEX ix_chunk_parse_content_update_date ON chunk_parse_content (update_date);
    CREATE INDEX ix_chunk_parse_content_update_time ON chunk_parse_content (update_time);
    -- interval_parse_task
    ALTER TABLE `interval_parse_task`
        RENAME COLUMN `chunk_parse_task_id` TO `chunk_task_id`,
        RENAME COLUMN `video_parse_task_id` TO `video_task_id`,
        RENAME COLUMN `chunk_parse_task_ids` TO `chunk_task_ids`,
        CHANGE COLUMN `create_time` `create_date` DATETIME(3) NULL,
        ADD COLUMN `create_time` BIGINT NULL,
        CHANGE COLUMN `update_time` `update_date` DATETIME(3) NULL,
        ADD COLUMN `update_time` BIGINT NULL;
    UPDATE `interval_parse_task` SET
        `create_time`=TIMESTAMPDIFF(MICROSECOND, '1970-01-01 00:00:00.000', `create_date`) DIV 1000,
        `update_time`=TIMESTAMPDIFF(MICROSECOND, '1970-01-01 00:00:00.000', `update_date`) DIV 1000;
    CREATE INDEX ix_interval_parse_task_create_date ON interval_parse_task (create_date);
    CREATE INDEX ix_interval_parse_task_create_time ON interval_parse_task (create_time);
    CREATE INDEX ix_interval_parse_task_update_date ON interval_parse_task (update_date);
    CREATE INDEX ix_interval_parse_task_update_time ON interval_parse_task (update_time);
    -- upload_segment
    ALTER TABLE `upload_segment`
        RENAME COLUMN `upload_session_id` TO `session_id`,
        RENAME COLUMN `actual_size_bytes` TO `size_bytes`,
        RENAME COLUMN `checksum_sha256` TO `sha256`,
        CHANGE COLUMN `created_at` `create_date` DATETIME(3) NULL,
        ADD COLUMN `create_time` BIGINT NULL,
        ADD COLUMN `update_time` BIGINT NULL,
        ADD COLUMN `update_date` DATETIME(3) NULL;
    UPDATE `upload_segment` SET
        `create_time`=TIMESTAMPDIFF(MICROSECOND, '1970-01-01 00:00:00.000', `create_date`) DIV 1000;
    CREATE INDEX ix_upload_segment_create_date ON upload_segment (create_date);
    CREATE INDEX ix_upload_segment_create_time ON upload_segment (create_time);
    CREATE INDEX ix_upload_segment_update_date ON upload_segment (update_date);
    CREATE INDEX ix_upload_segment_update_time ON upload_segment (update_time);
    -- interval_parse_content
    ALTER TABLE `interval_parse_content`
        RENAME COLUMN `interval_parse_task_id` TO `interval_task_id`,
        CHANGE COLUMN `create_time` `create_date` DATETIME(3) NULL,
        ADD COLUMN `create_time` BIGINT NULL,
        CHANGE COLUMN `update_time` `update_date` DATETIME(3) NULL,
        ADD COLUMN `update_time` BIGINT NULL;
    UPDATE `interval_parse_content` SET
        `create_time`=TIMESTAMPDIFF(MICROSECOND, '1970-01-01 00:00:00.000', `create_date`) DIV 1000,
        `update_time`=TIMESTAMPDIFF(MICROSECOND, '1970-01-01 00:00:00.000', `update_date`) DIV 1000;
    CREATE INDEX ix_interval_parse_content_create_date ON interval_parse_content (create_date);
    CREATE INDEX ix_interval_parse_content_create_time ON interval_parse_content (create_time);
    CREATE INDEX ix_interval_parse_content_update_date ON interval_parse_content (update_date);
    CREATE INDEX ix_interval_parse_content_update_time ON interval_parse_content (update_time);
    -- Recreate every target CHECK, then all 30 FKs; MySQL validates existing rows.
    ALTER TABLE agent_conversation ADD CONSTRAINT ck_agent_conversation_next_message_sequence CHECK (next_message_sequence >= 0);
    ALTER TABLE model_usage ADD CONSTRAINT ck_model_usage_task_type CHECK (task_type IN ('video_parse', 'chat_retrieval'));
    ALTER TABLE user ADD CONSTRAINT ck_guest_email CHECK (is_anonymous = 0 OR email IS NULL);
    ALTER TABLE user ADD CONSTRAINT ck_guest_role CHECK (is_anonymous = 0 OR is_superuser = 0);
    ALTER TABLE user ADD CONSTRAINT ck_user_auth_version CHECK (auth_version >= 1);
    ALTER TABLE user ADD CONSTRAINT ck_user_status CHECK (status IN ('ACTIVE', 'DISABLED'));
    ALTER TABLE user ADD CONSTRAINT ck_user_superuser CHECK (is_superuser IN (0, 1));
    ALTER TABLE agent_message ADD CONSTRAINT ck_agent_message_phase CHECK (phase IS NULL OR phase IN ('QUEUED', 'RETRIEVING', 'EVALUATING', 'RESPONDING'));
    ALTER TABLE agent_message ADD CONSTRAINT ck_agent_message_requested_top_k CHECK (requested_top_k IS NULL OR requested_top_k BETWEEN 1 AND 100);
    ALTER TABLE agent_message ADD CONSTRAINT ck_agent_message_role CHECK (role IN ('USER', 'ASSISTANT'));
    ALTER TABLE agent_message ADD CONSTRAINT ck_agent_message_sequence CHECK (sequence_no >= 0);
    ALTER TABLE agent_message ADD CONSTRAINT ck_agent_message_status CHECK (status IN ('PROCESSING', 'COMPLETED', 'FAILED'));
    ALTER TABLE agent_message ADD CONSTRAINT ck_agent_message_version CHECK (version > 0);
    ALTER TABLE tenant ADD CONSTRAINT ck_owned_tenant_owner CHECK (type = 'public' OR owner_id IS NOT NULL);
    ALTER TABLE tenant ADD CONSTRAINT ck_public_tenant_owner CHECK (type <> 'public' OR owner_id IS NULL);
    ALTER TABLE tenant ADD CONSTRAINT ck_tenant_status CHECK (status IN ('ACTIVE', 'DISABLED'));
    ALTER TABLE tenant ADD CONSTRAINT ck_tenant_type CHECK (type IN ('local', 'public', 'personal'));
    ALTER TABLE video ADD CONSTRAINT ck_video_duration CHECK (duration_ms > 0);
    ALTER TABLE video ADD CONSTRAINT ck_video_episode_order CHECK (episode_order > 0);
    ALTER TABLE video ADD CONSTRAINT ck_video_parse_progress CHECK (parse_progress BETWEEN 0 AND 100);
    ALTER TABLE video ADD CONSTRAINT ck_video_total_bytes CHECK (size_bytes >= 0);
    ALTER TABLE video ADD CONSTRAINT ck_video_upload_progress CHECK (upload_progress BETWEEN 0 AND 100);
    ALTER TABLE series_parse_job ADD CONSTRAINT ck_series_parse_job_attempt_count CHECK (attempt_count >= 0);
    ALTER TABLE series_parse_job ADD CONSTRAINT ck_series_parse_job_lease_version CHECK (lease_version >= 0);
    ALTER TABLE series_parse_job ADD CONSTRAINT ck_series_parse_job_max_attempts CHECK (max_attempts > 0);
    ALTER TABLE series_parse_job ADD CONSTRAINT ck_series_parse_job_processing_duration CHECK (processing_duration_ms >= 0);
    ALTER TABLE series_parse_job ADD CONSTRAINT ck_series_parse_job_progress CHECK (progress BETWEEN 0 AND 100);
    ALTER TABLE series_parse_job ADD CONSTRAINT ck_series_parse_job_stage CHECK (stage IN ('QUEUED', 'PREPARING', 'SPLIT_READY', 'PARSING', 'TEXT_EMBEDDING', 'VISION_EMBEDDING', 'PERSISTING', 'INDEXING', 'COMPLETED'));
    ALTER TABLE series_parse_job ADD CONSTRAINT ck_series_parse_job_status CHECK (status IN ('QUEUED', 'RUNNING', 'RETRY_WAIT', 'SUCCEEDED', 'FAILED'));
    ALTER TABLE series_parse_job ADD CONSTRAINT ck_series_parse_job_version CHECK (version >= 1);
    ALTER TABLE video_chunk ADD CONSTRAINT ck_video_chunk_analysis_range CHECK (window_start_ms <= start_ms AND window_end_ms >= end_ms);
    ALTER TABLE video_chunk ADD CONSTRAINT ck_video_chunk_range CHECK (start_ms >= 0 AND end_ms > start_ms);
    ALTER TABLE video_chunk ADD CONSTRAINT ck_video_chunk_sequence CHECK (sequence_no >= 0);
    ALTER TABLE video_parse_task ADD CONSTRAINT ck_video_parse_task_attempt_count CHECK (attempt_count >= 0);
    ALTER TABLE video_parse_task ADD CONSTRAINT ck_video_parse_task_chunk_count CHECK (chunk_count > 0);
    ALTER TABLE video_parse_task ADD CONSTRAINT ck_video_parse_task_lease_state CHECK ((status IN ('PREPARING', 'PARSING', 'INDEXING') AND lease_owner IS NOT NULL AND lease_expire_time IS NOT NULL AND heartbeat_time IS NOT NULL) OR (lease_owner IS NULL AND lease_expire_time IS NULL AND heartbeat_time IS NULL));
    ALTER TABLE video_parse_task ADD CONSTRAINT ck_video_parse_task_lease_version CHECK (lease_version >= 0);
    ALTER TABLE video_parse_task ADD CONSTRAINT ck_video_parse_task_max_attempts CHECK (max_attempts > 0);
    ALTER TABLE video_parse_task ADD CONSTRAINT ck_video_parse_task_processing_duration CHECK (processing_duration_ms >= 0);
    ALTER TABLE video_parse_task ADD CONSTRAINT ck_video_parse_task_status CHECK (status IN ('PENDING', 'PREPARING', 'PARSING', 'INDEXING', 'COMPLETE', 'PARSING_FAILED', 'INDEXING_FAILED'));
    ALTER TABLE video_parse_task ADD CONSTRAINT ck_video_parse_task_version CHECK (version > 0);
    ALTER TABLE chunk_parse_task ADD CONSTRAINT ck_chunk_parse_task_lease_state CHECK ((status = 'UNDERSTANDING' AND lease_owner IS NOT NULL AND lease_expire_time IS NOT NULL) OR (status <> 'UNDERSTANDING' AND lease_owner IS NULL AND lease_expire_time IS NULL));
    ALTER TABLE chunk_parse_task ADD CONSTRAINT ck_chunk_parse_task_lease_version CHECK (lease_version >= 0);
    ALTER TABLE chunk_parse_task ADD CONSTRAINT ck_chunk_parse_task_retry_count CHECK (retry_count >= 0);
    ALTER TABLE chunk_parse_task ADD CONSTRAINT ck_chunk_parse_task_status CHECK (status IN ('QUEUED', 'UNDERSTANDING', 'COMPLETE', 'FAILED'));
    ALTER TABLE context_snapshot ADD CONSTRAINT ck_context_snapshot_sequence CHECK (after_chunk_sequence_no >= 0);
    ALTER TABLE index_sync_intent ADD CONSTRAINT ck_index_sync_intent_attempt_count CHECK (attempt_count >= 0);
    ALTER TABLE index_sync_intent ADD CONSTRAINT ck_index_sync_intent_generation CHECK (generation > 0);
    ALTER TABLE index_sync_intent ADD CONSTRAINT ck_index_sync_intent_operation CHECK (operation IN ('UPSERT', 'DELETE'));
    ALTER TABLE index_sync_intent ADD CONSTRAINT ck_index_sync_intent_status CHECK (status IN ('PENDING', 'RETRY_WAIT', 'SYNCED'));
    ALTER TABLE parse_model_call ADD CONSTRAINT ck_parse_model_call_category CHECK (category IN ('parse', 'embedding'));
    ALTER TABLE parse_model_call ADD CONSTRAINT ck_parse_model_call_exactly_one_task CHECK ((job_id IS NOT NULL) <> (video_task_id IS NOT NULL));
    ALTER TABLE parse_model_call ADD CONSTRAINT ck_parse_model_call_execution_identity CHECK ((video_task_id IS NULL AND execution_kind IS NULL AND execution_task_id IS NULL) OR (video_task_id IS NOT NULL AND execution_kind IS NOT NULL AND execution_task_id IS NOT NULL));
    ALTER TABLE parse_model_call ADD CONSTRAINT ck_parse_model_call_execution_kind CHECK (execution_kind IS NULL OR execution_kind IN ('video', 'chunk', 'interval', 'prefix'));
    ALTER TABLE parse_model_call ADD CONSTRAINT ck_parse_model_call_status CHECK (status IN ('completed', 'failed'));
    ALTER TABLE upload_session ADD CONSTRAINT ck_upload_session_completion_stage CHECK (completion_stage IS NULL OR completion_stage IN ('VERIFYING', 'ASSEMBLING', 'PROBING', 'CREATING_JOB'));
    ALTER TABLE upload_session ADD CONSTRAINT ck_upload_session_episode_order CHECK (episode_order > 0);
    ALTER TABLE upload_session ADD CONSTRAINT ck_upload_session_segment_size CHECK (segment_size_bytes > 0);
    ALTER TABLE upload_session ADD CONSTRAINT ck_upload_session_status CHECK (status IN ('UPLOADING', 'COMPLETING', 'COMPLETED', 'EXPIRED'));
    ALTER TABLE upload_session ADD CONSTRAINT ck_upload_session_total_segments CHECK (segment_count > 0);
    ALTER TABLE upload_session ADD CONSTRAINT ck_upload_session_total_size CHECK (size_bytes > 0);
    ALTER TABLE upload_session ADD CONSTRAINT ck_upload_session_upload_sequence CHECK (upload_sequence > 0);
    ALTER TABLE upload_session ADD CONSTRAINT ck_upload_session_version CHECK (version > 0);
    ALTER TABLE video_chunk_checkpoint ADD CONSTRAINT ck_video_chunk_checkpoint_range CHECK ((start_ms IS NULL AND end_ms IS NULL) OR (start_ms IS NOT NULL AND end_ms IS NOT NULL AND start_ms >= 0 AND end_ms > start_ms));
    ALTER TABLE video_chunk_checkpoint ADD CONSTRAINT ck_video_chunk_checkpoint_sequence CHECK (sequence_no >= 0);
    ALTER TABLE video_chunk_checkpoint ADD CONSTRAINT ck_video_chunk_checkpoint_stage CHECK (stage IN ('PREPARED', 'SPLIT_READY', 'PARSED', 'TEXT_EMBEDDED', 'VISION_EMBEDDED', 'INDEXED'));
    ALTER TABLE video_parse_usage_attempt ADD CONSTRAINT ck_video_parse_usage_attempt_execution_kind CHECK (execution_kind IN ('video', 'chunk', 'interval', 'prefix'));
    ALTER TABLE chunk_parse_content ADD CONSTRAINT ck_chunk_parse_content_version CHECK (version > 0);
    ALTER TABLE interval_parse_task ADD CONSTRAINT ck_interval_parse_task_chunk_count CHECK (chunk_count BETWEEN 1 AND 63);
    ALTER TABLE interval_parse_task ADD CONSTRAINT ck_interval_parse_task_lease_state CHECK ((status IN ('REFINING', 'INDEXING') AND lease_owner IS NOT NULL AND lease_expire_time IS NOT NULL) OR (status IN ('WAITING', 'INDEXING', 'COMPLETE', 'PARSING_FAILED', 'INDEXING_FAILED') AND lease_owner IS NULL AND lease_expire_time IS NULL));
    ALTER TABLE interval_parse_task ADD CONSTRAINT ck_interval_parse_task_lease_version CHECK (lease_version >= 0);
    ALTER TABLE interval_parse_task ADD CONSTRAINT ck_interval_parse_task_ready_mask CHECK (ready_mask >= 0 AND (ready_mask >> chunk_count) = 0);
    ALTER TABLE interval_parse_task ADD CONSTRAINT ck_interval_parse_task_retry_count CHECK (retry_count >= 0);
    ALTER TABLE interval_parse_task ADD CONSTRAINT ck_interval_parse_task_start_sequence CHECK (start_sequence_no >= 0);
    ALTER TABLE interval_parse_task ADD CONSTRAINT ck_interval_parse_task_status CHECK (status IN ('WAITING', 'REFINING', 'INDEXING', 'COMPLETE', 'PARSING_FAILED', 'INDEXING_FAILED'));
    ALTER TABLE upload_segment ADD CONSTRAINT ck_upload_segment_number CHECK (segment_number >= 0);
    ALTER TABLE upload_segment ADD CONSTRAINT ck_upload_segment_range CHECK (start_byte >= 0 AND end_byte_exclusive > start_byte);
    ALTER TABLE upload_segment ADD CONSTRAINT ck_upload_segment_size CHECK (size_bytes = end_byte_exclusive - start_byte);
    ALTER TABLE `agent_message` ADD CONSTRAINT `fk_bm_agent_message_1` FOREIGN KEY (`conversation_id`) REFERENCES `agent_conversation` (`id`) ON DELETE CASCADE;
    ALTER TABLE `tenant` ADD CONSTRAINT `fk_bm_tenant_1` FOREIGN KEY (`owner_id`) REFERENCES `user` (`id`);
    ALTER TABLE `video` ADD CONSTRAINT `fk_bm_video_1` FOREIGN KEY (`series_id`) REFERENCES `series` (`id`) ON DELETE CASCADE;
    ALTER TABLE `series_parse_job` ADD CONSTRAINT `fk_bm_series_parse_job_1` FOREIGN KEY (`series_id`) REFERENCES `series` (`id`) ON DELETE CASCADE;
    ALTER TABLE `series_parse_job` ADD CONSTRAINT `fk_bm_series_parse_job_2` FOREIGN KEY (`video_id`) REFERENCES `video` (`id`) ON DELETE CASCADE;
    ALTER TABLE `video_chunk` ADD CONSTRAINT `fk_bm_video_chunk_1` FOREIGN KEY (`video_id`) REFERENCES `video` (`id`) ON DELETE CASCADE;
    ALTER TABLE `video_parse_task` ADD CONSTRAINT `fk_bm_video_parse_task_1` FOREIGN KEY (`video_id`) REFERENCES `video` (`id`) ON DELETE CASCADE;
    ALTER TABLE `chunk_parse_task` ADD CONSTRAINT `fk_bm_chunk_parse_task_1` FOREIGN KEY (`chunk_id`) REFERENCES `video_chunk` (`id`) ON DELETE CASCADE;
    ALTER TABLE `chunk_parse_task` ADD CONSTRAINT `fk_bm_chunk_parse_task_2` FOREIGN KEY (`video_task_id`) REFERENCES `video_parse_task` (`id`) ON DELETE CASCADE;
    ALTER TABLE `context_snapshot` ADD CONSTRAINT `fk_bm_context_snapshot_1` FOREIGN KEY (`series_id`) REFERENCES `series` (`id`) ON DELETE CASCADE;
    ALTER TABLE `context_snapshot` ADD CONSTRAINT `fk_bm_context_snapshot_2` FOREIGN KEY (`source_chunk_id`) REFERENCES `video_chunk` (`id`) ON DELETE CASCADE;
    ALTER TABLE `context_snapshot` ADD CONSTRAINT `fk_bm_context_snapshot_3` FOREIGN KEY (`video_id`) REFERENCES `video` (`id`) ON DELETE CASCADE;
    ALTER TABLE `index_sync_intent` ADD CONSTRAINT `fk_bm_index_sync_intent_1` FOREIGN KEY (`chunk_id`) REFERENCES `video_chunk` (`id`) ON DELETE CASCADE;
    ALTER TABLE `parse_model_call` ADD CONSTRAINT `fk_bm_parse_model_call_1` FOREIGN KEY (`job_id`) REFERENCES `series_parse_job` (`id`);
    ALTER TABLE `parse_model_call` ADD CONSTRAINT `fk_bm_parse_model_call_2` FOREIGN KEY (`video_task_id`) REFERENCES `video_parse_task` (`id`) ON DELETE CASCADE;
    ALTER TABLE `parse_usage_attempt` ADD CONSTRAINT `fk_bm_parse_usage_attempt_1` FOREIGN KEY (`job_id`) REFERENCES `series_parse_job` (`id`);
    ALTER TABLE `parse_usage_attempt` ADD CONSTRAINT `fk_bm_parse_usage_attempt_2` FOREIGN KEY (`video_task_id`) REFERENCES `video_parse_task` (`id`) ON DELETE SET NULL;
    ALTER TABLE `upload_session` ADD CONSTRAINT `fk_bm_upload_session_1` FOREIGN KEY (`initial_task_id`) REFERENCES `video_parse_task` (`id`) ON DELETE SET NULL;
    ALTER TABLE `upload_session` ADD CONSTRAINT `fk_bm_upload_session_2` FOREIGN KEY (`job_id`) REFERENCES `series_parse_job` (`id`) ON DELETE SET NULL;
    ALTER TABLE `upload_session` ADD CONSTRAINT `fk_bm_upload_session_3` FOREIGN KEY (`series_id`) REFERENCES `series` (`id`) ON DELETE CASCADE;
    ALTER TABLE `upload_session` ADD CONSTRAINT `fk_bm_upload_session_4` FOREIGN KEY (`video_id`) REFERENCES `video` (`id`) ON DELETE SET NULL;
    ALTER TABLE `video_chunk_checkpoint` ADD CONSTRAINT `fk_bm_video_chunk_checkpoint_1` FOREIGN KEY (`job_id`) REFERENCES `series_parse_job` (`id`) ON DELETE CASCADE;
    ALTER TABLE `video_chunk_embedding` ADD CONSTRAINT `fk_bm_video_chunk_embedding_1` FOREIGN KEY (`chunk_id`) REFERENCES `video_chunk` (`id`) ON DELETE CASCADE;
    ALTER TABLE `video_chunk_info` ADD CONSTRAINT `fk_bm_video_chunk_info_1` FOREIGN KEY (`chunk_id`) REFERENCES `video_chunk` (`id`) ON DELETE CASCADE;
    ALTER TABLE `video_parse_usage_attempt` ADD CONSTRAINT `fk_bm_video_parse_usage_attempt_1` FOREIGN KEY (`video_task_id`) REFERENCES `video_parse_task` (`id`) ON DELETE CASCADE;
    ALTER TABLE `chunk_parse_content` ADD CONSTRAINT `fk_bm_chunk_parse_content_1` FOREIGN KEY (`chunk_task_id`) REFERENCES `chunk_parse_task` (`id`) ON DELETE CASCADE;
    ALTER TABLE `interval_parse_task` ADD CONSTRAINT `fk_interval_parse_task_anchor_parent` FOREIGN KEY (`chunk_task_id`,`video_task_id`) REFERENCES `chunk_parse_task` (`id`,`video_task_id`) ON DELETE CASCADE;
    ALTER TABLE `interval_parse_task` ADD CONSTRAINT `fk_bm_interval_parse_task_2` FOREIGN KEY (`video_task_id`) REFERENCES `video_parse_task` (`id`) ON DELETE CASCADE;
    ALTER TABLE `upload_segment` ADD CONSTRAINT `fk_bm_upload_segment_1` FOREIGN KEY (`session_id`) REFERENCES `upload_session` (`id`) ON DELETE CASCADE;
    ALTER TABLE `interval_parse_content` ADD CONSTRAINT `fk_bm_interval_parse_content_1` FOREIGN KEY (`interval_task_id`) REFERENCES `interval_parse_task` (`id`) ON DELETE CASCADE;

    -- Postflight: exact target columns/types/nullability, PK/UNIQUE/index keys,
    -- CHECK set, 30 FK constraints (31 column references), unchanged row counts.
    IF EXISTS (SELECT 1 FROM _bm_target_cols e LEFT JOIN information_schema.columns c
            ON c.table_schema=DATABASE() AND c.table_name=e.table_name AND c.column_name=e.column_name
            WHERE c.column_name IS NULL OR c.data_type<>e.data_type
                OR (e.char_length IS NOT NULL AND NOT(c.character_maximum_length <=> e.char_length))
                OR (e.date_precision IS NOT NULL AND NOT(c.datetime_precision <=> e.date_precision))
                OR c.is_nullable<>e.nullable OR (LOCATE('unsigned',c.column_type)>0)<>e.is_unsigned) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'postflight target columns/types/nullability do not match the 390-column model';
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.columns c JOIN _bm_tables t ON t.table_name=c.table_name
            LEFT JOIN _bm_target_cols e ON e.table_name=c.table_name AND e.column_name=c.column_name
            WHERE c.table_schema=DATABASE() AND e.column_name IS NULL) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'postflight target columns/types/nullability do not match the 390-column model';
    END IF;
    DELETE FROM _bm_actual_keys;
    INSERT INTO _bm_actual_keys
        SELECT s.table_name,s.index_name,MAX(1-s.non_unique),
            GROUP_CONCAT(COALESCE(s.column_name,'#expression') ORDER BY s.seq_in_index SEPARATOR ',')
        FROM information_schema.statistics s JOIN _bm_tables t ON t.table_name=s.table_name
        WHERE s.table_schema=DATABASE() GROUP BY s.table_name,s.index_name;
    IF EXISTS (SELECT 1 FROM _bm_target_keys e LEFT JOIN _bm_actual_keys a
            ON a.table_name=e.table_name AND (e.key_name IS NULL OR a.key_name=e.key_name)
                AND a.is_unique=e.is_unique AND a.columns_csv=e.columns_csv WHERE a.key_name IS NULL) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'postflight target PK/UNIQUE/index definitions differ';
    END IF;
    IF (SELECT COUNT(*) FROM _bm_actual_keys WHERE is_unique=1)<>(SELECT COUNT(*) FROM _bm_target_keys WHERE is_unique=1) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'postflight target PK/UNIQUE/index definitions differ';
    END IF;
    IF (SELECT COUNT(*) FROM information_schema.table_constraints c JOIN _bm_tables t ON t.table_name=c.table_name
            WHERE c.constraint_schema=DATABASE() AND c.constraint_type='CHECK')<>(SELECT COUNT(*) FROM _bm_target_checks) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'postflight target CHECK names/enforcement differ';
    END IF;
    IF EXISTS (SELECT 1 FROM _bm_target_checks e LEFT JOIN information_schema.table_constraints c
            ON c.constraint_schema=DATABASE() AND c.table_name=e.table_name AND c.constraint_name=e.check_name
                AND c.constraint_type='CHECK' AND c.enforced='YES' WHERE c.constraint_name IS NULL) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'postflight target CHECK names/enforcement differ';
    END IF;
    DELETE FROM _bm_actual_fks;
    INSERT INTO _bm_actual_fks
        SELECT k.table_name,k.constraint_name,GROUP_CONCAT(k.column_name ORDER BY k.ordinal_position SEPARATOR ','),
            MAX(k.referenced_table_name),GROUP_CONCAT(k.referenced_column_name ORDER BY k.ordinal_position SEPARATOR ','),
            REPLACE(MAX(r.delete_rule),'NO ACTION','RESTRICT'),REPLACE(MAX(r.update_rule),'NO ACTION','RESTRICT')
        FROM information_schema.key_column_usage k
        JOIN information_schema.referential_constraints r
            ON r.constraint_schema=k.constraint_schema AND r.table_name=k.table_name AND r.constraint_name=k.constraint_name
        JOIN _bm_tables t ON t.table_name=k.table_name
        WHERE k.constraint_schema=DATABASE() AND k.referenced_table_name IS NOT NULL GROUP BY k.table_name,k.constraint_name;
    IF (SELECT COUNT(*) FROM _bm_actual_fks)<>30 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'postflight FK reconstruction differs; keep writers stopped and restore/review backup';
    END IF;
    IF EXISTS (SELECT 1 FROM _bm_target_fks e LEFT JOIN _bm_actual_fks a
            ON a.table_name=e.table_name AND a.columns_csv=e.columns_csv AND a.ref_table=e.ref_table
                AND a.ref_columns_csv=e.ref_columns_csv AND a.delete_rule=e.delete_rule AND a.update_rule=e.update_rule
            WHERE a.constraint_name IS NULL) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'postflight FK reconstruction differs; keep writers stopped and restore/review backup';
    END IF;
    IF @@SESSION.foreign_key_checks <> 1 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'foreign_key_checks unexpectedly changed during migration';
    END IF;
    IF (SELECT COUNT(*) FROM `agent_conversation`)<>(SELECT rows_before FROM _bm_tables WHERE table_name='agent_conversation') THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='agent_conversation row count changed during migration; keep writers stopped';
    END IF;
    IF EXISTS (SELECT 1 FROM `agent_conversation` WHERE NOT(`create_date` <=> DATE_ADD('1970-01-01 00:00:00.000', INTERVAL `create_time` * 1000 MICROSECOND)) OR NOT(`update_date` <=> DATE_ADD('1970-01-01 00:00:00.000', INTERVAL `update_time` * 1000 MICROSECOND))) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='agent_conversation audit time/date pairs do not match exactly';
    END IF;
    IF (SELECT COUNT(*) FROM `model_usage`)<>(SELECT rows_before FROM _bm_tables WHERE table_name='model_usage') THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='model_usage row count changed during migration; keep writers stopped';
    END IF;
    IF EXISTS (SELECT 1 FROM `model_usage` WHERE NOT(`create_date` <=> DATE_ADD('1970-01-01 00:00:00.000', INTERVAL `create_time` * 1000 MICROSECOND)) OR NOT(`update_date` <=> DATE_ADD('1970-01-01 00:00:00.000', INTERVAL `update_time` * 1000 MICROSECOND))) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='model_usage audit time/date pairs do not match exactly';
    END IF;
    IF (SELECT COUNT(*) FROM `series`)<>(SELECT rows_before FROM _bm_tables WHERE table_name='series') THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='series row count changed during migration; keep writers stopped';
    END IF;
    IF EXISTS (SELECT 1 FROM `series` WHERE NOT(`create_date` <=> DATE_ADD('1970-01-01 00:00:00.000', INTERVAL `create_time` * 1000 MICROSECOND)) OR NOT(`update_date` <=> DATE_ADD('1970-01-01 00:00:00.000', INTERVAL `update_time` * 1000 MICROSECOND))) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='series audit time/date pairs do not match exactly';
    END IF;
    IF (SELECT COUNT(*) FROM `user`)<>(SELECT rows_before FROM _bm_tables WHERE table_name='user') THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='user row count changed during migration; keep writers stopped';
    END IF;
    IF EXISTS (SELECT 1 FROM `user` WHERE NOT(`create_date` <=> DATE_ADD('1970-01-01 00:00:00.000', INTERVAL `create_time` * 1000 MICROSECOND)) OR NOT(`update_date` <=> DATE_ADD('1970-01-01 00:00:00.000', INTERVAL `update_time` * 1000 MICROSECOND))) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='user audit time/date pairs do not match exactly';
    END IF;
    IF (SELECT COUNT(*) FROM `agent_message`)<>(SELECT rows_before FROM _bm_tables WHERE table_name='agent_message') THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='agent_message row count changed during migration; keep writers stopped';
    END IF;
    IF EXISTS (SELECT 1 FROM `agent_message` WHERE NOT(`create_date` <=> DATE_ADD('1970-01-01 00:00:00.000', INTERVAL `create_time` * 1000 MICROSECOND)) OR NOT(`update_date` <=> DATE_ADD('1970-01-01 00:00:00.000', INTERVAL `update_time` * 1000 MICROSECOND))) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='agent_message audit time/date pairs do not match exactly';
    END IF;
    IF (SELECT COUNT(*) FROM `tenant`)<>(SELECT rows_before FROM _bm_tables WHERE table_name='tenant') THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='tenant row count changed during migration; keep writers stopped';
    END IF;
    IF EXISTS (SELECT 1 FROM `tenant` WHERE NOT(`create_date` <=> DATE_ADD('1970-01-01 00:00:00.000', INTERVAL `create_time` * 1000 MICROSECOND)) OR NOT(`update_date` <=> DATE_ADD('1970-01-01 00:00:00.000', INTERVAL `update_time` * 1000 MICROSECOND))) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='tenant audit time/date pairs do not match exactly';
    END IF;
    IF (SELECT COUNT(*) FROM `video`)<>(SELECT rows_before FROM _bm_tables WHERE table_name='video') THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='video row count changed during migration; keep writers stopped';
    END IF;
    IF EXISTS (SELECT 1 FROM `video` WHERE NOT(`create_date` <=> DATE_ADD('1970-01-01 00:00:00.000', INTERVAL `create_time` * 1000 MICROSECOND)) OR NOT(`update_date` <=> DATE_ADD('1970-01-01 00:00:00.000', INTERVAL `update_time` * 1000 MICROSECOND))) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='video audit time/date pairs do not match exactly';
    END IF;
    IF (SELECT COUNT(*) FROM `series_parse_job`)<>(SELECT rows_before FROM _bm_tables WHERE table_name='series_parse_job') THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='series_parse_job row count changed during migration; keep writers stopped';
    END IF;
    IF EXISTS (SELECT 1 FROM `series_parse_job` WHERE NOT(`create_date` <=> DATE_ADD('1970-01-01 00:00:00.000', INTERVAL `create_time` * 1000 MICROSECOND)) OR NOT(`update_date` <=> DATE_ADD('1970-01-01 00:00:00.000', INTERVAL `update_time` * 1000 MICROSECOND))) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='series_parse_job audit time/date pairs do not match exactly';
    END IF;
    IF (SELECT COUNT(*) FROM `video_chunk`)<>(SELECT rows_before FROM _bm_tables WHERE table_name='video_chunk') THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='video_chunk row count changed during migration; keep writers stopped';
    END IF;
    IF EXISTS (SELECT 1 FROM `video_chunk` WHERE NOT(`create_date` <=> DATE_ADD('1970-01-01 00:00:00.000', INTERVAL `create_time` * 1000 MICROSECOND)) OR NOT(`update_date` <=> DATE_ADD('1970-01-01 00:00:00.000', INTERVAL `update_time` * 1000 MICROSECOND))) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='video_chunk audit time/date pairs do not match exactly';
    END IF;
    IF (SELECT COUNT(*) FROM `video_parse_task`)<>(SELECT rows_before FROM _bm_tables WHERE table_name='video_parse_task') THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='video_parse_task row count changed during migration; keep writers stopped';
    END IF;
    IF EXISTS (SELECT 1 FROM `video_parse_task` WHERE NOT(`create_date` <=> DATE_ADD('1970-01-01 00:00:00.000', INTERVAL `create_time` * 1000 MICROSECOND)) OR NOT(`update_date` <=> DATE_ADD('1970-01-01 00:00:00.000', INTERVAL `update_time` * 1000 MICROSECOND))) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='video_parse_task audit time/date pairs do not match exactly';
    END IF;
    IF (SELECT COUNT(*) FROM `chunk_parse_task`)<>(SELECT rows_before FROM _bm_tables WHERE table_name='chunk_parse_task') THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='chunk_parse_task row count changed during migration; keep writers stopped';
    END IF;
    IF EXISTS (SELECT 1 FROM `chunk_parse_task` WHERE NOT(`create_date` <=> DATE_ADD('1970-01-01 00:00:00.000', INTERVAL `create_time` * 1000 MICROSECOND)) OR NOT(`update_date` <=> DATE_ADD('1970-01-01 00:00:00.000', INTERVAL `update_time` * 1000 MICROSECOND))) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='chunk_parse_task audit time/date pairs do not match exactly';
    END IF;
    IF (SELECT COUNT(*) FROM `context_snapshot`)<>(SELECT rows_before FROM _bm_tables WHERE table_name='context_snapshot') THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='context_snapshot row count changed during migration; keep writers stopped';
    END IF;
    IF EXISTS (SELECT 1 FROM `context_snapshot` WHERE NOT(`create_date` <=> DATE_ADD('1970-01-01 00:00:00.000', INTERVAL `create_time` * 1000 MICROSECOND)) OR NOT(`update_date` <=> DATE_ADD('1970-01-01 00:00:00.000', INTERVAL `update_time` * 1000 MICROSECOND))) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='context_snapshot audit time/date pairs do not match exactly';
    END IF;
    IF (SELECT COUNT(*) FROM `index_sync_intent`)<>(SELECT rows_before FROM _bm_tables WHERE table_name='index_sync_intent') THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='index_sync_intent row count changed during migration; keep writers stopped';
    END IF;
    IF EXISTS (SELECT 1 FROM `index_sync_intent` WHERE NOT(`create_date` <=> DATE_ADD('1970-01-01 00:00:00.000', INTERVAL `create_time` * 1000 MICROSECOND)) OR NOT(`update_date` <=> DATE_ADD('1970-01-01 00:00:00.000', INTERVAL `update_time` * 1000 MICROSECOND))) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='index_sync_intent audit time/date pairs do not match exactly';
    END IF;
    IF (SELECT COUNT(*) FROM `parse_model_call`)<>(SELECT rows_before FROM _bm_tables WHERE table_name='parse_model_call') THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='parse_model_call row count changed during migration; keep writers stopped';
    END IF;
    IF EXISTS (SELECT 1 FROM `parse_model_call` WHERE NOT(`create_date` <=> DATE_ADD('1970-01-01 00:00:00.000', INTERVAL `create_time` * 1000 MICROSECOND)) OR NOT(`update_date` <=> DATE_ADD('1970-01-01 00:00:00.000', INTERVAL `update_time` * 1000 MICROSECOND))) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='parse_model_call audit time/date pairs do not match exactly';
    END IF;
    IF (SELECT COUNT(*) FROM `parse_usage_attempt`)<>(SELECT rows_before FROM _bm_tables WHERE table_name='parse_usage_attempt') THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='parse_usage_attempt row count changed during migration; keep writers stopped';
    END IF;
    IF EXISTS (SELECT 1 FROM `parse_usage_attempt` WHERE NOT(`create_date` <=> DATE_ADD('1970-01-01 00:00:00.000', INTERVAL `create_time` * 1000 MICROSECOND)) OR NOT(`update_date` <=> DATE_ADD('1970-01-01 00:00:00.000', INTERVAL `update_time` * 1000 MICROSECOND))) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='parse_usage_attempt audit time/date pairs do not match exactly';
    END IF;
    IF (SELECT COUNT(*) FROM `upload_session`)<>(SELECT rows_before FROM _bm_tables WHERE table_name='upload_session') THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='upload_session row count changed during migration; keep writers stopped';
    END IF;
    IF EXISTS (SELECT 1 FROM `upload_session` WHERE NOT(`create_date` <=> DATE_ADD('1970-01-01 00:00:00.000', INTERVAL `create_time` * 1000 MICROSECOND)) OR NOT(`update_date` <=> DATE_ADD('1970-01-01 00:00:00.000', INTERVAL `update_time` * 1000 MICROSECOND))) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='upload_session audit time/date pairs do not match exactly';
    END IF;
    IF (SELECT COUNT(*) FROM `video_chunk_checkpoint`)<>(SELECT rows_before FROM _bm_tables WHERE table_name='video_chunk_checkpoint') THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='video_chunk_checkpoint row count changed during migration; keep writers stopped';
    END IF;
    IF EXISTS (SELECT 1 FROM `video_chunk_checkpoint` WHERE NOT(`create_date` <=> DATE_ADD('1970-01-01 00:00:00.000', INTERVAL `create_time` * 1000 MICROSECOND)) OR NOT(`update_date` <=> DATE_ADD('1970-01-01 00:00:00.000', INTERVAL `update_time` * 1000 MICROSECOND))) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='video_chunk_checkpoint audit time/date pairs do not match exactly';
    END IF;
    IF (SELECT COUNT(*) FROM `video_chunk_embedding`)<>(SELECT rows_before FROM _bm_tables WHERE table_name='video_chunk_embedding') THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='video_chunk_embedding row count changed during migration; keep writers stopped';
    END IF;
    IF EXISTS (SELECT 1 FROM `video_chunk_embedding` WHERE NOT(`create_date` <=> DATE_ADD('1970-01-01 00:00:00.000', INTERVAL `create_time` * 1000 MICROSECOND)) OR NOT(`update_date` <=> DATE_ADD('1970-01-01 00:00:00.000', INTERVAL `update_time` * 1000 MICROSECOND))) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='video_chunk_embedding audit time/date pairs do not match exactly';
    END IF;
    IF (SELECT COUNT(*) FROM `video_chunk_info`)<>(SELECT rows_before FROM _bm_tables WHERE table_name='video_chunk_info') THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='video_chunk_info row count changed during migration; keep writers stopped';
    END IF;
    IF EXISTS (SELECT 1 FROM `video_chunk_info` WHERE NOT(`create_date` <=> DATE_ADD('1970-01-01 00:00:00.000', INTERVAL `create_time` * 1000 MICROSECOND)) OR NOT(`update_date` <=> DATE_ADD('1970-01-01 00:00:00.000', INTERVAL `update_time` * 1000 MICROSECOND))) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='video_chunk_info audit time/date pairs do not match exactly';
    END IF;
    IF (SELECT COUNT(*) FROM `video_parse_usage_attempt`)<>(SELECT rows_before FROM _bm_tables WHERE table_name='video_parse_usage_attempt') THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='video_parse_usage_attempt row count changed during migration; keep writers stopped';
    END IF;
    IF EXISTS (SELECT 1 FROM `video_parse_usage_attempt` WHERE NOT(`create_date` <=> DATE_ADD('1970-01-01 00:00:00.000', INTERVAL `create_time` * 1000 MICROSECOND)) OR NOT(`update_date` <=> DATE_ADD('1970-01-01 00:00:00.000', INTERVAL `update_time` * 1000 MICROSECOND))) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='video_parse_usage_attempt audit time/date pairs do not match exactly';
    END IF;
    IF (SELECT COUNT(*) FROM `chunk_parse_content`)<>(SELECT rows_before FROM _bm_tables WHERE table_name='chunk_parse_content') THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='chunk_parse_content row count changed during migration; keep writers stopped';
    END IF;
    IF EXISTS (SELECT 1 FROM `chunk_parse_content` WHERE NOT(`create_date` <=> DATE_ADD('1970-01-01 00:00:00.000', INTERVAL `create_time` * 1000 MICROSECOND)) OR NOT(`update_date` <=> DATE_ADD('1970-01-01 00:00:00.000', INTERVAL `update_time` * 1000 MICROSECOND))) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='chunk_parse_content audit time/date pairs do not match exactly';
    END IF;
    IF (SELECT COUNT(*) FROM `interval_parse_task`)<>(SELECT rows_before FROM _bm_tables WHERE table_name='interval_parse_task') THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='interval_parse_task row count changed during migration; keep writers stopped';
    END IF;
    IF EXISTS (SELECT 1 FROM `interval_parse_task` WHERE NOT(`create_date` <=> DATE_ADD('1970-01-01 00:00:00.000', INTERVAL `create_time` * 1000 MICROSECOND)) OR NOT(`update_date` <=> DATE_ADD('1970-01-01 00:00:00.000', INTERVAL `update_time` * 1000 MICROSECOND))) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='interval_parse_task audit time/date pairs do not match exactly';
    END IF;
    IF (SELECT COUNT(*) FROM `upload_segment`)<>(SELECT rows_before FROM _bm_tables WHERE table_name='upload_segment') THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='upload_segment row count changed during migration; keep writers stopped';
    END IF;
    IF EXISTS (SELECT 1 FROM `upload_segment` WHERE NOT(`create_date` <=> DATE_ADD('1970-01-01 00:00:00.000', INTERVAL `create_time` * 1000 MICROSECOND)) OR NOT(`update_date` <=> DATE_ADD('1970-01-01 00:00:00.000', INTERVAL `update_time` * 1000 MICROSECOND))) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='upload_segment audit time/date pairs do not match exactly';
    END IF;
    IF (SELECT COUNT(*) FROM `interval_parse_content`)<>(SELECT rows_before FROM _bm_tables WHERE table_name='interval_parse_content') THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='interval_parse_content row count changed during migration; keep writers stopped';
    END IF;
    IF EXISTS (SELECT 1 FROM `interval_parse_content` WHERE NOT(`create_date` <=> DATE_ADD('1970-01-01 00:00:00.000', INTERVAL `create_time` * 1000 MICROSECOND)) OR NOT(`update_date` <=> DATE_ADD('1970-01-01 00:00:00.000', INTERVAL `update_time` * 1000 MICROSECOND))) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='interval_parse_content audit time/date pairs do not match exactly';
    END IF;

    SELECT 'BaseModel migration verified' result, 24 business_tables, 390 columns,
        30 foreign_key_constraints, 31 foreign_key_column_references;
    DROP TEMPORARY TABLE _bm_actual_fks, _bm_actual_keys, _bm_source_cols, _bm_target_cols,
        _bm_source_fks, _bm_target_fks, _bm_source_checks, _bm_target_checks,
        _bm_source_keys, _bm_target_keys, _bm_tables;
    SET SESSION time_zone=previous_time_zone;
    SET SESSION sql_mode=previous_sql_mode;
    DO RELEASE_LOCK(migration_lock);
END$$
CALL migrate_base_model_field_names()$$
DROP PROCEDURE migrate_base_model_field_names$$
DELIMITER ;
