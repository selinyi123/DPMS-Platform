-- Rollback is intentionally blocked once any notification or archived Outbox
-- evidence exists. Operators must export the evidence first and run the
-- explicit cleanup procedure; silently dropping it would violate the audit
-- contract.
CREATE TEMPORARY TABLE dpms_0030_rollback_guard AS
SELECT
  (SELECT COUNT(*) FROM notification_delivery_attempts) AS delivery_rows,
  (SELECT COUNT(*) FROM outbox_event_archive) AS archive_rows,
  (SELECT COUNT(*) FROM platform_runtime_security_domains
    WHERE status <> 'compat') AS active_security_domains;

SET @dpms_0030_block = (
  SELECT IF(delivery_rows > 0 OR archive_rows > 0 OR active_security_domains > 0,
            1, 0)
  FROM dpms_0030_rollback_guard
);
DROP TEMPORARY TABLE dpms_0030_rollback_guard;

SET @dpms_sql = IF(
  @dpms_0030_block = 1,
  'SELECT 1',
  'ALTER TABLE outbox_events DROP INDEX idx_outbox_archive_ready'
);
PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

SET @dpms_sql = IF(
  @dpms_0030_block = 1,
  'SELECT 1',
  'ALTER TABLE task_outbox_events DROP INDEX idx_task_outbox_archive_ready'
);
PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

SET @dpms_sql = IF(
  @dpms_0030_block = 1,
  'SELECT 1',
  'ALTER TABLE outbox_events DROP COLUMN archived_at'
);
PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

SET @dpms_sql = IF(
  @dpms_0030_block = 1,
  'SELECT 1',
  'ALTER TABLE task_outbox_events DROP COLUMN archived_at'
);
PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

SET @dpms_sql = IF(
  @dpms_0030_block = 1,
  'SELECT 1',
  'ALTER TABLE task_outbox_events DROP COLUMN redis_delivery_epoch'
);
PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

SET @dpms_sql = IF(
  @dpms_0030_block = 1,
  'SELECT 1',
  'DROP TABLE outbox_archive_watermarks'
);
PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

SET @dpms_sql = IF(
  @dpms_0030_block = 1,
  'SELECT 1',
  'DROP TABLE outbox_event_archive'
);
PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

SET @dpms_sql = IF(
  @dpms_0030_block = 1,
  'SELECT 1',
  'DROP TABLE notification_delivery_attempts'
);
PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

SET @dpms_sql = IF(
  @dpms_0030_block = 1,
  'SELECT 1',
  'DROP TABLE platform_runtime_security_domains'
);
PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

-- A blocked rollback deliberately leaves the migration recorded and the
-- schema intact. The operator must resolve the guard before retrying.
