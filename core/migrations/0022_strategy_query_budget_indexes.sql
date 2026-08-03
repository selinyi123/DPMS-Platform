-- Keep Strategy's bounded account/history reads on deterministic index ranges.
-- Canonical visible indexes are retry-safe when MySQL committed DDL before
-- schema_migrations was recorded. A same-named drifted or invisible index is
-- never overwritten: its prior definition cannot be reconstructed by the
-- static rollback, so migration stops for explicit operator resolution.

SET @dpms_strategy_task_account_index_signature = (
  SELECT GROUP_CONCAT(
           CONCAT(
             SEQ_IN_INDEX, ':', COLUMN_NAME, ':', NON_UNIQUE, ':',
             COALESCE(CAST(SUB_PART AS CHAR), 'FULL'), ':', IS_VISIBLE
           )
           ORDER BY SEQ_IN_INDEX SEPARATOR ','
         )
  FROM information_schema.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'task_runs'
    AND INDEX_NAME = 'idx_task_run_account_created_id'
);

SET @dpms_sql = CASE
  WHEN @dpms_strategy_task_account_index_signature =
       '1:account_id:1:FULL:YES,2:created_at:1:FULL:YES,3:id:1:FULL:YES'
    THEN 'SELECT 1'
  WHEN @dpms_strategy_task_account_index_signature IS NULL
    THEN 'ALTER TABLE task_runs
            ADD INDEX idx_task_run_account_created_id
              (account_id, created_at, id) VISIBLE'
  ELSE 'SELECT *
          FROM information_schema.dpms_0022_task_account_index_drift_requires_manual_resolution'
END;

PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

SET @dpms_strategy_account_candidate_index_signature = (
  SELECT GROUP_CONCAT(
           CONCAT(
             SEQ_IN_INDEX, ':', COLUMN_NAME, ':', NON_UNIQUE, ':',
             COALESCE(CAST(SUB_PART AS CHAR), 'FULL'), ':', IS_VISIBLE
           )
           ORDER BY SEQ_IN_INDEX SEPARATOR ','
         )
  FROM information_schema.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'accounts'
    AND INDEX_NAME = 'idx_account_strategy_candidate'
);

SET @dpms_sql = CASE
  WHEN @dpms_strategy_account_candidate_index_signature =
       '1:platform:1:FULL:YES,2:status:1:FULL:YES,3:deleted_at:1:FULL:YES,4:daily_task_count:1:FULL:YES,5:id:1:FULL:YES'
    THEN 'SELECT 1'
  WHEN @dpms_strategy_account_candidate_index_signature IS NULL
    THEN 'ALTER TABLE accounts
            ADD INDEX idx_account_strategy_candidate
              (platform, status, deleted_at, daily_task_count, id) VISIBLE'
  ELSE 'SELECT *
          FROM information_schema.dpms_0022_account_candidate_index_drift_requires_manual_resolution'
END;

PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

SET @dpms_strategy_calibration_latest_index_signature = (
  SELECT GROUP_CONCAT(
           CONCAT(
             SEQ_IN_INDEX, ':', COLUMN_NAME, ':', NON_UNIQUE, ':',
             COALESCE(CAST(SUB_PART AS CHAR), 'FULL'), ':', IS_VISIBLE
           )
           ORDER BY SEQ_IN_INDEX SEPARATOR ','
         )
  FROM information_schema.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'account_calibrations'
    AND INDEX_NAME = 'idx_account_calibration_account_platform_id'
);

SET @dpms_sql = CASE
  WHEN @dpms_strategy_calibration_latest_index_signature =
       '1:account_id:1:FULL:YES,2:platform:1:FULL:YES,3:id:1:FULL:YES'
    THEN 'SELECT 1'
  WHEN @dpms_strategy_calibration_latest_index_signature IS NULL
    THEN 'ALTER TABLE account_calibrations
            ADD INDEX idx_account_calibration_account_platform_id
              (account_id, platform, id) VISIBLE'
  ELSE 'SELECT *
          FROM information_schema.dpms_0022_calibration_latest_index_drift_requires_manual_resolution'
END;

PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

SET @dpms_strategy_calibration_rollback_fallback_exists = (
  SELECT COUNT(*)
  FROM information_schema.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'account_calibrations'
    AND INDEX_NAME = 'idx_account_calibrations_account_fk_rollback'
);

SET @dpms_sql = IF(
  @dpms_strategy_calibration_rollback_fallback_exists > 0,
  'ALTER TABLE account_calibrations
     DROP INDEX idx_account_calibrations_account_fk_rollback',
  'SELECT 1'
);

PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

SET @dpms_strategy_task_rollback_fallback_exists = (
  SELECT COUNT(*)
  FROM information_schema.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'task_runs'
    AND INDEX_NAME = 'idx_task_runs_account_fk_rollback'
);

SET @dpms_sql = IF(
  @dpms_strategy_task_rollback_fallback_exists > 0,
  'ALTER TABLE task_runs
     DROP INDEX idx_task_runs_account_fk_rollback',
  'SELECT 1'
);

PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

SET @dpms_strategy_task_created_index_signature = (
  SELECT GROUP_CONCAT(
           CONCAT(
             SEQ_IN_INDEX, ':', COLUMN_NAME, ':', NON_UNIQUE, ':',
             COALESCE(CAST(SUB_PART AS CHAR), 'FULL'), ':', IS_VISIBLE
           )
           ORDER BY SEQ_IN_INDEX SEPARATOR ','
         )
  FROM information_schema.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'task_runs'
    AND INDEX_NAME = 'idx_task_run_created_lottery_id'
);

SET @dpms_sql = CASE
  WHEN @dpms_strategy_task_created_index_signature =
       '1:created_at:1:FULL:YES,2:lottery_id:1:FULL:YES,3:id:1:FULL:YES'
    THEN 'SELECT 1'
  WHEN @dpms_strategy_task_created_index_signature IS NULL
    THEN 'ALTER TABLE task_runs
            ADD INDEX idx_task_run_created_lottery_id
              (created_at, lottery_id, id) VISIBLE'
  ELSE 'SELECT *
          FROM information_schema.dpms_0022_task_created_index_drift_requires_manual_resolution'
END;

PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

SET @dpms_strategy_risk_created_index_signature = (
  SELECT GROUP_CONCAT(
           CONCAT(
             SEQ_IN_INDEX, ':', COLUMN_NAME, ':', NON_UNIQUE, ':',
             COALESCE(CAST(SUB_PART AS CHAR), 'FULL'), ':', IS_VISIBLE
           )
           ORDER BY SEQ_IN_INDEX SEPARATOR ','
         )
  FROM information_schema.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'risk_events'
    AND INDEX_NAME = 'idx_risk_created_account_id'
);

SET @dpms_sql = CASE
  WHEN @dpms_strategy_risk_created_index_signature =
       '1:created_at:1:FULL:YES,2:account_id:1:FULL:YES,3:id:1:FULL:YES'
    THEN 'SELECT 1'
  WHEN @dpms_strategy_risk_created_index_signature IS NULL
    THEN 'ALTER TABLE risk_events
            ADD INDEX idx_risk_created_account_id
              (created_at, account_id, id) VISIBLE'
  ELSE 'SELECT *
          FROM information_schema.dpms_0022_risk_created_index_drift_requires_manual_resolution'
END;

PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

SET @dpms_strategy_lottery_extracted_index_signature = (
  SELECT GROUP_CONCAT(
           CONCAT(
             SEQ_IN_INDEX, ':', COLUMN_NAME, ':', NON_UNIQUE, ':',
             COALESCE(CAST(SUB_PART AS CHAR), 'FULL'), ':', IS_VISIBLE
           )
           ORDER BY SEQ_IN_INDEX SEPARATOR ','
         )
  FROM information_schema.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'lotteries'
    AND INDEX_NAME = 'idx_lottery_extracted_platform_id'
);

SET @dpms_sql = CASE
  WHEN @dpms_strategy_lottery_extracted_index_signature =
       '1:extracted_at:1:FULL:YES,2:platform:1:FULL:YES,3:id:1:FULL:YES'
    THEN 'SELECT 1'
  WHEN @dpms_strategy_lottery_extracted_index_signature IS NULL
    THEN 'ALTER TABLE lotteries
            ADD INDEX idx_lottery_extracted_platform_id
              (extracted_at, platform, id) VISIBLE'
  ELSE 'SELECT *
          FROM information_schema.dpms_0022_lottery_extracted_index_drift_requires_manual_resolution'
END;

PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;
