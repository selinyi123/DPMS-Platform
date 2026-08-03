-- Optional rollback for the platform-specific recovery indexes. Keep the
-- repaired status indexes: they pre-date 0020 and remain required by durable
-- adapter-probe and account-calibration control reconciliation.
--
-- Drop both platform indexes in one ALTER so a large account_calibrations
-- table acquires one metadata lock. The dynamic action remains retry-safe.

SET @dpms_account_calibration_queued_index_exists = (
  SELECT COUNT(*)
  FROM information_schema.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'account_calibrations'
    AND INDEX_NAME = 'idx_account_calibration_platform_queued'
);

SET @dpms_account_calibration_running_index_exists = (
  SELECT COUNT(*)
  FROM information_schema.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'account_calibrations'
    AND INDEX_NAME = 'idx_account_calibration_platform_running'
);

SET @dpms_account_calibration_rollback_actions = CONCAT_WS(
  ', ',
  IF(
    @dpms_account_calibration_queued_index_exists > 0,
    'DROP INDEX idx_account_calibration_platform_queued',
    NULL
  ),
  IF(
    @dpms_account_calibration_running_index_exists > 0,
    'DROP INDEX idx_account_calibration_platform_running',
    NULL
  )
);

SET @dpms_sql = IF(
  @dpms_account_calibration_rollback_actions = '',
  'SELECT 1',
  CONCAT(
    'ALTER TABLE account_calibrations ',
    @dpms_account_calibration_rollback_actions
  )
);

PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

DELETE FROM schema_migrations WHERE version = '0020';
