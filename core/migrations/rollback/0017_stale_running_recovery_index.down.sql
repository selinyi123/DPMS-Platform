-- Optional rollback for the bounded stale-running scan index.

SET @dpms_sql = IF(
  (
    SELECT COUNT(*)
    FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'task_runs'
      AND INDEX_NAME = 'idx_task_run_stale_running'
  ) > 0,
  'ALTER TABLE task_runs DROP INDEX idx_task_run_stale_running',
  'SELECT 1'
);

PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

SET @dpms_sql = IF(
  (
    SELECT COUNT(*)
    FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'lotteries'
      AND INDEX_NAME = 'idx_lottery_platform_recovery'
  ) > 0,
  'ALTER TABLE lotteries DROP INDEX idx_lottery_platform_recovery',
  'SELECT 1'
);

PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

SET @dpms_sql = IF(
  (
    SELECT COUNT(*)
    FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'task_runs'
      AND INDEX_NAME = 'idx_task_run_lottery_stale'
  ) > 0,
  'ALTER TABLE task_runs DROP INDEX idx_task_run_lottery_stale',
  'SELECT 1'
);

PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

DELETE FROM schema_migrations WHERE version = '0017';
