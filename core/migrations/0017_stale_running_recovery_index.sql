-- Bound database-authoritative recovery and its backlog metric when
-- Redis/Pending state is lost. Status and lease expiry form the global metric
-- range; task_id gives deterministic ordering without terminal history.

SET @dpms_stale_running_index_signature = (
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
    AND TABLE_NAME = 'task_runs'
    AND INDEX_NAME = 'idx_task_run_stale_running'
);

SET @dpms_sql = CASE
  WHEN @dpms_stale_running_index_signature =
       '1:status:1:FULL:YES,2:lease_expires_at:1:FULL:YES,3:task_id:1:FULL:YES'
    THEN 'SELECT 1'
  WHEN @dpms_stale_running_index_signature IS NULL
    THEN 'ALTER TABLE task_runs
            ADD INDEX idx_task_run_stale_running
              (status, lease_expires_at, task_id) VISIBLE'
  ELSE 'ALTER TABLE task_runs
          DROP INDEX idx_task_run_stale_running,
          ADD INDEX idx_task_run_stale_running
            (status, lease_expires_at, task_id) VISIBLE'
END;

PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

ALTER TABLE task_runs ALTER INDEX idx_task_run_stale_running VISIBLE;

SET @dpms_task_lottery_stale_index_signature = (
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
    AND TABLE_NAME = 'task_runs'
    AND INDEX_NAME = 'idx_task_run_lottery_stale'
);

SET @dpms_sql = CASE
  WHEN @dpms_task_lottery_stale_index_signature =
       '1:lottery_id:1:FULL:YES,2:status:1:FULL:YES,3:lease_expires_at:1:FULL:YES,4:task_id:1:FULL:YES'
    THEN 'SELECT 1'
  WHEN @dpms_task_lottery_stale_index_signature IS NULL
    THEN 'ALTER TABLE task_runs
            ADD INDEX idx_task_run_lottery_stale
              (lottery_id, status, lease_expires_at, task_id) VISIBLE'
  ELSE 'ALTER TABLE task_runs
          DROP INDEX idx_task_run_lottery_stale,
          ADD INDEX idx_task_run_lottery_stale
            (lottery_id, status, lease_expires_at, task_id) VISIBLE'
END;

PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

ALTER TABLE task_runs ALTER INDEX idx_task_run_lottery_stale VISIBLE;

-- Per-platform scanners start from lotteries so one platform's backlog cannot
-- become another platform's scan prefix. Use a recovery-owned index instead of
-- changing the pre-existing idx_pending contract used by other call paths.
SET @dpms_lottery_platform_index_signature = (
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
    AND TABLE_NAME = 'lotteries'
    AND INDEX_NAME = 'idx_lottery_platform_recovery'
);

SET @dpms_sql = CASE
  WHEN @dpms_lottery_platform_index_signature =
       '1:platform:1:FULL:YES,2:status:1:FULL:YES,3:id:1:FULL:YES'
    THEN 'SELECT 1'
  WHEN @dpms_lottery_platform_index_signature IS NULL
    THEN 'ALTER TABLE lotteries
            ADD INDEX idx_lottery_platform_recovery
              (platform, status, id) VISIBLE'
  ELSE 'ALTER TABLE lotteries
          DROP INDEX idx_lottery_platform_recovery,
          ADD INDEX idx_lottery_platform_recovery
            (platform, status, id) VISIBLE'
END;

PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

ALTER TABLE lotteries ALTER INDEX idx_lottery_platform_recovery VISIBLE;
