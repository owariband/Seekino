-- Seekino controlled migration: personal Series sharing.
-- Stop API/worker writes and verify a backup before applying to the explicitly
-- selected database. MySQL DDL commits implicitly: stop on the first error,
-- inspect partial state, and never use mysql --force or blindly rerun this file.
-- Existing true values become public, false values private. Ownership, resource
-- IDs, timestamps and search indexes remain unchanged. Apply frontend/API code
-- that consumes visibility together; older code still requires is_published.

DELIMITER $$
DROP PROCEDURE IF EXISTS assert_series_visibility_source_20261019$$
CREATE PROCEDURE assert_series_visibility_source_20261019()
BEGIN
    DECLARE source_columns INT DEFAULT 0;
    IF DATABASE() IS NULL THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'select the intended database explicitly';
    END IF;
    SELECT COUNT(*) INTO source_columns
    FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'series'
      AND COLUMN_NAME IN ('id', 'tenant_id', 'create_time', 'is_published');
    IF source_columns <> 4 THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'expected current Series schema with is_published; inspect migration state';
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.COLUMNS
               WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'series'
                 AND COLUMN_NAME = 'visibility')
       OR EXISTS (SELECT 1 FROM information_schema.STATISTICS
                  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'series'
                    AND INDEX_NAME = 'ix_series_visibility_created')
       OR EXISTS (SELECT 1 FROM information_schema.TABLE_CONSTRAINTS
                  WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'series'
                    AND CONSTRAINT_NAME = 'ck_series_visibility') THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'Series visibility migration already started; inspect state rather than rerun';
    END IF;
    IF EXISTS (SELECT 1 FROM series WHERE is_published IS NULL OR is_published NOT IN (0, 1)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'is_published contains values outside the boolean contract';
    END IF;
END$$
CALL assert_series_visibility_source_20261019()$$
DROP PROCEDURE assert_series_visibility_source_20261019$$
DELIMITER ;

ALTER TABLE series ADD COLUMN visibility VARCHAR(16) NOT NULL DEFAULT 'private' AFTER status;
UPDATE series SET visibility = CASE WHEN is_published = 1 THEN 'public' ELSE 'private' END;
ALTER TABLE series
    ADD CONSTRAINT ck_series_visibility CHECK (visibility IN ('private', 'public')),
    ADD INDEX ix_series_visibility_created (visibility, create_time, id),
    DROP COLUMN is_published;

SELECT visibility, COUNT(*) AS series_count FROM series GROUP BY visibility;
