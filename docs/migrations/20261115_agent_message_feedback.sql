-- Persist one user's latest reaction to one assistant message.
-- Apply once against the selected database before starting code that reads this table.
-- MySQL DDL commits implicitly. Keep a backup; this script intentionally refuses to
-- rerun or to accept an existing, differently shaped table.

SET time_zone = '+00:00';

DELIMITER $$

DROP PROCEDURE IF EXISTS migrate_agent_message_feedback_20261115$$
CREATE PROCEDURE migrate_agent_message_feedback_20261115()
BEGIN
    DECLARE prerequisite_tables INT DEFAULT 0;
    DECLARE existing_tables INT DEFAULT 0;

    SELECT COUNT(*) INTO prerequisite_tables
    FROM information_schema.TABLES
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME IN ('agent_message', 'agent_conversation');

    IF prerequisite_tables <> 2 THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'agent_message and agent_conversation must exist before adding message feedback';
    END IF;

    SELECT COUNT(*) INTO existing_tables
    FROM information_schema.TABLES
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'agent_message_feedback';

    IF existing_tables <> 0 THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'agent_message_feedback already exists; inspect state rather than rerun';
    END IF;

    CREATE TABLE agent_message_feedback (
        message_id VARCHAR(36) NOT NULL,
        user_id VARCHAR(64) NOT NULL,
        conversation_id VARCHAR(36) NOT NULL,
        tenant_id VARCHAR(64) NOT NULL,
        rating VARCHAR(8) NOT NULL,
        reasons JSON NULL,
        comment VARCHAR(2000) NULL,
        message_version INT NOT NULL,
        model VARCHAR(255) NULL,
        message_status VARCHAR(16) NOT NULL,
        phase VARCHAR(16) NULL,
        error_code VARCHAR(64) NULL,
        create_time BIGINT NULL,
        create_date DATETIME(3) NULL,
        update_time BIGINT NULL,
        update_date DATETIME(3) NULL,
        PRIMARY KEY (message_id, user_id),
        KEY ix_agent_message_feedback_conversation_time (conversation_id, create_time),
        KEY ix_agent_message_feedback_rating_time (rating, create_time),
        KEY ix_agent_message_feedback_user_time (user_id, create_time),
        KEY ix_agent_message_feedback_create_time (create_time),
        KEY ix_agent_message_feedback_create_date (create_date),
        KEY ix_agent_message_feedback_update_time (update_time),
        KEY ix_agent_message_feedback_update_date (update_date),
        CONSTRAINT fk_agent_message_feedback_message
            FOREIGN KEY (message_id) REFERENCES agent_message (id) ON DELETE CASCADE,
        CONSTRAINT ck_agent_message_feedback_rating
            CHECK (rating IN ('UP', 'DOWN')),
        CONSTRAINT ck_agent_message_feedback_message_status
            CHECK (message_status IN ('COMPLETED', 'FAILED')),
        CONSTRAINT ck_agent_message_feedback_message_version
            CHECK (message_version > 0),
        CONSTRAINT ck_agent_message_feedback_phase
            CHECK (phase IS NULL OR phase IN ('QUEUED', 'RETRIEVING', 'EVALUATING', 'RESPONDING'))
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
END$$

CALL migrate_agent_message_feedback_20261115()$$
DROP PROCEDURE migrate_agent_message_feedback_20261115$$

DELIMITER ;

SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE
FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA = DATABASE()
  AND TABLE_NAME = 'agent_message_feedback'
ORDER BY ORDINAL_POSITION;
