-- Agent message request identity lives in detail.request.idempotencyKey.
--
-- Apply only to an isolated, empty V2 schema that still has the three old
-- columns, the original reply self-reference, and the legacy empty timing
-- table. MySQL DDL auto-commits; inspect the target schema and take a backup
-- before applying elsewhere.
-- The conversation-level idempotency_key is intentionally unchanged.

DELIMITER $$

DROP PROCEDURE IF EXISTS assert_empty_agent_message_request_source$$
CREATE PROCEDURE assert_empty_agent_message_request_source()
BEGIN
    DECLARE old_column_count INT DEFAULT 0;
    DECLARE old_index_count INT DEFAULT 0;
    DECLARE old_reply_fk_count INT DEFAULT 0;
    DECLARE detail_column_count INT DEFAULT 0;
    DECLARE timing_table_count INT DEFAULT 0;

    IF EXISTS (SELECT 1 FROM agent_message LIMIT 1) THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'Agent message request migration requires an empty agent_message table';
    END IF;

    SELECT COUNT(*) INTO timing_table_count
    FROM information_schema.TABLES
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'agent_message_timing';

    IF timing_table_count <> 1 THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'Agent message request migration requires the legacy timing table';
    END IF;

    IF EXISTS (SELECT 1 FROM agent_message_timing LIMIT 1) THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'Agent message request migration requires an empty agent_message_timing table';
    END IF;

    SELECT COUNT(*) INTO old_column_count
    FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'agent_message'
      AND COLUMN_NAME IN ('idempotency_key', 'reply_to_message_id', 'retrieval_query');

    SELECT COUNT(*) INTO detail_column_count
    FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'agent_message'
      AND COLUMN_NAME = 'detail';

    SELECT COUNT(DISTINCT INDEX_NAME) INTO old_index_count
    FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'agent_message'
      AND INDEX_NAME IN (
          'uq_agent_message_conversation_idempotency',
          'uq_agent_message_reply_to_message'
      );

    SELECT COUNT(*) INTO old_reply_fk_count
    FROM information_schema.KEY_COLUMN_USAGE
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'agent_message'
      AND COLUMN_NAME = 'reply_to_message_id'
      AND CONSTRAINT_NAME = 'agent_message_ibfk_2'
      AND REFERENCED_TABLE_NAME = 'agent_message';

    IF old_column_count <> 3 OR old_index_count <> 2
       OR old_reply_fk_count <> 1 OR detail_column_count <> 0 THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'Agent message request migration source schema does not match';
    END IF;
END$$

CALL assert_empty_agent_message_request_source()$$
DROP PROCEDURE assert_empty_agent_message_request_source$$

DELIMITER ;

ALTER TABLE agent_message
    ADD COLUMN detail JSON NULL AFTER extra_context,
    DROP FOREIGN KEY agent_message_ibfk_2,
    DROP INDEX uq_agent_message_reply_to_message,
    DROP INDEX uq_agent_message_conversation_idempotency,
    DROP COLUMN idempotency_key,
    DROP COLUMN reply_to_message_id,
    DROP COLUMN retrieval_query;

DROP TABLE agent_message_timing;

-- Keep a physical uniqueness guarantee for USER request replay while the
-- value itself stays in JSON. This expression matches the SQLAlchemy model.
CREATE UNIQUE INDEX uq_agent_message_conversation_idempotency
ON agent_message (
    conversation_id,
    (CAST(
        CASE JSON_EXTRACT(detail, '$."request"."idempotencyKey"')
            WHEN 'null' THEN NULL
            ELSE JSON_UNQUOTE(JSON_EXTRACT(detail, '$."request"."idempotencyKey"'))
        END AS CHAR(128)
    ))
);
