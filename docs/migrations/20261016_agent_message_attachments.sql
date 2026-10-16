-- Add the canonical message attachment references without rewriting history.
-- Run against the selected database before starting code that reads this column.
-- MySQL DDL commits implicitly. Keep a backup; rollback code can leave the new
-- nullable column in place. Do not drop it after attachment references are stored.

DELIMITER $$

DROP PROCEDURE IF EXISTS migrate_agent_message_attachments_20261016$$
CREATE PROCEDURE migrate_agent_message_attachments_20261016()
BEGIN
    DECLARE table_count INT DEFAULT 0;
    DECLARE column_count INT DEFAULT 0;
    DECLARE compatible_count INT DEFAULT 0;

    SELECT COUNT(*) INTO table_count
    FROM information_schema.TABLES
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'agent_message';
    IF table_count <> 1 THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'agent_message must exist before adding attachments';
    END IF;

    SELECT COUNT(*), COALESCE(SUM(DATA_TYPE = 'json' AND IS_NULLABLE = 'YES'), 0)
    INTO column_count, compatible_count
    FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'agent_message'
      AND COLUMN_NAME = 'attachments';

    IF column_count = 0 THEN
        ALTER TABLE agent_message ADD COLUMN attachments JSON NULL;
    ELSEIF compatible_count <> 1 THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'agent_message.attachments exists with incompatible type or nullability';
    END IF;
END$$

CALL migrate_agent_message_attachments_20261016()$$
DROP PROCEDURE migrate_agent_message_attachments_20261016$$

DELIMITER ;

SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE
FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'agent_message'
  AND COLUMN_NAME = 'attachments';
