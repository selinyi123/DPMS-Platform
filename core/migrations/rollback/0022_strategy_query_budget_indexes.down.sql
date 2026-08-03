-- Optional retry-safe rollback for Strategy query-budget indexes.

SET @dpms_strategy_task_account_index_exists = (
  SELECT COUNT(*)
  FROM information_schema.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'task_runs'
    AND INDEX_NAME = 'idx_task_run_account_created_id'
);

SET @dpms_strategy_task_account_alternative_exists = (
  SELECT COUNT(*)
  FROM information_schema.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'task_runs'
    AND INDEX_NAME <> 'idx_task_run_account_created_id'
    AND SEQ_IN_INDEX = 1
    AND COLUMN_NAME = 'account_id'
);

SET @dpms_sql = CASE
  WHEN @dpms_strategy_task_account_index_exists = 0
    THEN 'SELECT 1'
  WHEN @dpms_strategy_task_account_alternative_exists > 0
    THEN 'ALTER TABLE task_runs
            DROP INDEX idx_task_run_account_created_id'
  ELSE 'ALTER TABLE task_runs
          ADD INDEX idx_task_runs_account_fk_rollback (account_id),
          DROP INDEX idx_task_run_account_created_id'
END;

PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

SET @dpms_strategy_account_candidate_index_exists = (
  SELECT COUNT(*)
  FROM information_schema.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'accounts'
    AND INDEX_NAME = 'idx_account_strategy_candidate'
);

SET @dpms_sql = IF(
  @dpms_strategy_account_candidate_index_exists > 0,
  'ALTER TABLE accounts DROP INDEX idx_account_strategy_candidate',
  'SELECT 1'
);

PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

SET @dpms_strategy_calibration_latest_index_exists = (
  SELECT COUNT(*)
  FROM information_schema.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'account_calibrations'
    AND INDEX_NAME = 'idx_account_calibration_account_platform_id'
);

SET @dpms_strategy_calibration_account_alternative_exists = (
  SELECT COUNT(*)
  FROM information_schema.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'account_calibrations'
    AND INDEX_NAME <> 'idx_account_calibration_account_platform_id'
    AND SEQ_IN_INDEX = 1
    AND COLUMN_NAME = 'account_id'
);

SET @dpms_sql = CASE
  WHEN @dpms_strategy_calibration_latest_index_exists = 0
    THEN 'SELECT 1'
  WHEN @dpms_strategy_calibration_account_alternative_exists > 0
    THEN 'ALTER TABLE account_calibrations
            DROP INDEX idx_account_calibration_account_platform_id'
  ELSE 'ALTER TABLE account_calibrations
          ADD INDEX idx_account_calibrations_account_fk_rollback
            (account_id),
          DROP INDEX idx_account_calibration_account_platform_id'
END;

PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

SET @dpms_strategy_task_created_index_exists = (
  SELECT COUNT(*)
  FROM information_schema.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'task_runs'
    AND INDEX_NAME = 'idx_task_run_created_lottery_id'
);

SET @dpms_sql = IF(
  @dpms_strategy_task_created_index_exists > 0,
  'ALTER TABLE task_runs DROP INDEX idx_task_run_created_lottery_id',
  'SELECT 1'
);

PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

SET @dpms_strategy_risk_created_index_exists = (
  SELECT COUNT(*)
  FROM information_schema.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'risk_events'
    AND INDEX_NAME = 'idx_risk_created_account_id'
);

SET @dpms_sql = IF(
  @dpms_strategy_risk_created_index_exists > 0,
  'ALTER TABLE risk_events DROP INDEX idx_risk_created_account_id',
  'SELECT 1'
);

PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

SET @dpms_strategy_lottery_extracted_index_exists = (
  SELECT COUNT(*)
  FROM information_schema.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'lotteries'
    AND INDEX_NAME = 'idx_lottery_extracted_platform_id'
);

SET @dpms_sql = IF(
  @dpms_strategy_lottery_extracted_index_exists > 0,
  'ALTER TABLE lotteries DROP INDEX idx_lottery_extracted_platform_id',
  'SELECT 1'
);

PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

DELETE FROM schema_migrations WHERE version = '0022';
