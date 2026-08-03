-- Manual, fail-closed rollback for 0024.
--
-- Run only with Core and every platform Worker quiesced. Removing the durable
-- queue while a profile is still pending/running would restore the credential
-- retention bug, so rollback refuses until every recorded cleanup succeeded.

DROP TEMPORARY TABLE IF EXISTS dpms_0024_rollback_guard;

CREATE TEMPORARY TABLE dpms_0024_rollback_guard (
  has_incomplete_cleanup TINYINT UNSIGNED NOT NULL,
  CONSTRAINT chk_0024_no_incomplete_profile_cleanup
    CHECK (has_incomplete_cleanup = 0) ENFORCED
);

SET @dpms_0024_cleanup_table_exists = (
  SELECT COUNT(*)
  FROM information_schema.TABLES
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'account_profile_cleanup_intents'
);

SET @dpms_sql = IF(
  @dpms_0024_cleanup_table_exists > 0,
  'INSERT INTO dpms_0024_rollback_guard (has_incomplete_cleanup)
   SELECT EXISTS (
     SELECT 1
     FROM account_profile_cleanup_intents
     WHERE status <> ''succeeded''
     LIMIT 1
   )',
  'INSERT INTO dpms_0024_rollback_guard (has_incomplete_cleanup) VALUES (0)'
);

PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

SET @dpms_0024_login_cleanup_table_exists = (
  SELECT COUNT(*)
  FROM information_schema.TABLES
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'login_profile_cleanup_intents'
);

SET @dpms_sql = IF(
  @dpms_0024_login_cleanup_table_exists > 0,
  'INSERT INTO dpms_0024_rollback_guard (has_incomplete_cleanup)
   SELECT EXISTS (
     SELECT 1
     FROM login_profile_cleanup_intents
     WHERE status <> ''succeeded''
     LIMIT 1
   )',
  'INSERT INTO dpms_0024_rollback_guard (has_incomplete_cleanup) VALUES (0)'
);

PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

DROP TEMPORARY TABLE dpms_0024_rollback_guard;

DROP TABLE IF EXISTS login_profile_cleanup_intents;

DROP TABLE IF EXISTS account_profile_cleanup_intents;

DELETE FROM schema_migrations WHERE version = '0024';
