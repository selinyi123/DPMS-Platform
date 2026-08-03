-- Keep account-calibration recovery inside one platform's database range.
-- Also repair the two pre-existing status indexes used by durable control
-- reconciliation. Every dynamic ALTER is retry-safe when MySQL has applied
-- the DDL but schema_migrations has not yet recorded this migration.
--
-- The three account_calibrations index actions are deliberately combined into
-- one ALTER TABLE. This bounds metadata-lock acquisition and avoids rebuilding
-- a large calibration table once per missing/drifted index.

SET @dpms_adapter_probe_status_index_signature = (
  SELECT GROUP_CONCAT(
           CONCAT(
             SEQ_IN_INDEX, ':', COLUMN_NAME, ':', NON_UNIQUE, ':',
             COALESCE(CAST(SUB_PART AS CHAR), 'FULL'), ':',
             IS_VISIBLE
           )
           ORDER BY SEQ_IN_INDEX
           SEPARATOR ','
         )
  FROM information_schema.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'adapter_calibrations'
    AND INDEX_NAME = 'idx_adapter_probe_status'
);

SET @dpms_sql = CASE
  WHEN @dpms_adapter_probe_status_index_signature =
       '1:status:1:FULL:YES,2:created_at:1:FULL:YES'
    THEN 'SELECT 1'
  WHEN @dpms_adapter_probe_status_index_signature =
       '1:status:1:FULL:NO,2:created_at:1:FULL:NO'
    THEN 'ALTER TABLE adapter_calibrations
            ALTER INDEX idx_adapter_probe_status VISIBLE'
  WHEN @dpms_adapter_probe_status_index_signature IS NULL
    THEN 'ALTER TABLE adapter_calibrations
            ADD INDEX idx_adapter_probe_status
              (status, created_at) VISIBLE'
  ELSE 'ALTER TABLE adapter_calibrations
          DROP INDEX idx_adapter_probe_status,
          ADD INDEX idx_adapter_probe_status
            (status, created_at) VISIBLE'
END;

PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

SET @dpms_account_calibration_status_index_signature = (
  SELECT GROUP_CONCAT(
           CONCAT(
             SEQ_IN_INDEX, ':', COLUMN_NAME, ':', NON_UNIQUE, ':',
             COALESCE(CAST(SUB_PART AS CHAR), 'FULL'), ':',
             IS_VISIBLE
           )
           ORDER BY SEQ_IN_INDEX
           SEPARATOR ','
         )
  FROM information_schema.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'account_calibrations'
    AND INDEX_NAME = 'idx_account_calibration_status'
);

SET @dpms_account_calibration_queued_index_signature = (
  SELECT GROUP_CONCAT(
           CONCAT(
             SEQ_IN_INDEX, ':', COLUMN_NAME, ':', NON_UNIQUE, ':',
             COALESCE(CAST(SUB_PART AS CHAR), 'FULL'), ':',
             IS_VISIBLE
           )
           ORDER BY SEQ_IN_INDEX
           SEPARATOR ','
         )
  FROM information_schema.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'account_calibrations'
    AND INDEX_NAME = 'idx_account_calibration_platform_queued'
);

SET @dpms_account_calibration_running_index_signature = (
  SELECT GROUP_CONCAT(
           CONCAT(
             SEQ_IN_INDEX, ':', COLUMN_NAME, ':', NON_UNIQUE, ':',
             COALESCE(CAST(SUB_PART AS CHAR), 'FULL'), ':',
             IS_VISIBLE
           )
           ORDER BY SEQ_IN_INDEX
           SEPARATOR ','
         )
  FROM information_schema.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'account_calibrations'
    AND INDEX_NAME = 'idx_account_calibration_platform_running'
);

SET @dpms_account_calibration_status_index_action = CASE
  WHEN @dpms_account_calibration_status_index_signature =
       '1:status:1:FULL:YES,2:created_at:1:FULL:YES'
    THEN ''
  WHEN @dpms_account_calibration_status_index_signature =
       '1:status:1:FULL:NO,2:created_at:1:FULL:NO'
    THEN 'ALTER INDEX idx_account_calibration_status VISIBLE'
  WHEN @dpms_account_calibration_status_index_signature IS NULL
    THEN 'ADD INDEX idx_account_calibration_status
            (status, created_at) VISIBLE'
  ELSE 'DROP INDEX idx_account_calibration_status,
        ADD INDEX idx_account_calibration_status
          (status, created_at) VISIBLE'
END;

SET @dpms_account_calibration_queued_index_action = CASE
  WHEN @dpms_account_calibration_queued_index_signature =
       '1:platform:1:FULL:YES,2:status:1:FULL:YES,3:created_at:1:FULL:YES,4:id:1:FULL:YES'
    THEN ''
  WHEN @dpms_account_calibration_queued_index_signature =
       '1:platform:1:FULL:NO,2:status:1:FULL:NO,3:created_at:1:FULL:NO,4:id:1:FULL:NO'
    THEN 'ALTER INDEX idx_account_calibration_platform_queued VISIBLE'
  WHEN @dpms_account_calibration_queued_index_signature IS NULL
    THEN 'ADD INDEX idx_account_calibration_platform_queued
            (platform, status, created_at, id) VISIBLE'
  ELSE 'DROP INDEX idx_account_calibration_platform_queued,
        ADD INDEX idx_account_calibration_platform_queued
          (platform, status, created_at, id) VISIBLE'
END;

SET @dpms_account_calibration_running_index_action = CASE
  WHEN @dpms_account_calibration_running_index_signature =
       '1:platform:1:FULL:YES,2:status:1:FULL:YES,3:started_at:1:FULL:YES,4:created_at:1:FULL:YES,5:id:1:FULL:YES'
    THEN ''
  WHEN @dpms_account_calibration_running_index_signature =
       '1:platform:1:FULL:NO,2:status:1:FULL:NO,3:started_at:1:FULL:NO,4:created_at:1:FULL:NO,5:id:1:FULL:NO'
    THEN 'ALTER INDEX idx_account_calibration_platform_running VISIBLE'
  WHEN @dpms_account_calibration_running_index_signature IS NULL
    THEN 'ADD INDEX idx_account_calibration_platform_running
            (platform, status, started_at, created_at, id) VISIBLE'
  ELSE 'DROP INDEX idx_account_calibration_platform_running,
        ADD INDEX idx_account_calibration_platform_running
          (platform, status, started_at, created_at, id) VISIBLE'
END;

SET @dpms_account_calibration_index_actions = CONCAT_WS(
  ', ',
  NULLIF(@dpms_account_calibration_status_index_action, ''),
  NULLIF(@dpms_account_calibration_queued_index_action, ''),
  NULLIF(@dpms_account_calibration_running_index_action, '')
);

SET @dpms_sql = IF(
  @dpms_account_calibration_index_actions = '',
  'SELECT 1',
  CONCAT(
    'ALTER TABLE account_calibrations ',
    @dpms_account_calibration_index_actions
  )
);

PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;
