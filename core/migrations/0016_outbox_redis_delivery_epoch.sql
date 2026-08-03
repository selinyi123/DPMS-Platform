-- Bind every successfully relayed task Outbox row to the Redis process and
-- dataset-continuity epoch that accepted its XADD.  A changed Redis run_id or
-- continuity sentinel can then replay only queued task rows whose acknowledged
-- stream write may have disappeared.

SET @dpms_outbox_epoch_column_signature = (
  SELECT CONCAT(
           LOWER(DATA_TYPE), ':',
           COALESCE(CAST(CHARACTER_MAXIMUM_LENGTH AS CHAR), 'NONE'), ':',
           UPPER(IS_NULLABLE)
         )
  FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'outbox_events'
    AND COLUMN_NAME = 'redis_delivery_epoch'
);

SET @dpms_sql = CASE
  WHEN @dpms_outbox_epoch_column_signature = 'varchar:128:YES'
    THEN 'SELECT 1'
  WHEN @dpms_outbox_epoch_column_signature IS NULL
    THEN 'ALTER TABLE outbox_events
            ADD COLUMN redis_delivery_epoch VARCHAR(128) NULL AFTER sent_at'
  ELSE 'ALTER TABLE outbox_events
          MODIFY COLUMN redis_delivery_epoch VARCHAR(128) NULL'
END;

PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

-- The periodic replay query must start from queued tasks and point-lookup the
-- corresponding immutable Outbox authority.  Repair both supporting indexes
-- rather than allowing a drifted deployment to fall back to terminal-history
-- scans every five seconds.
SET @dpms_outbox_dedup_index_signature = (
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
    AND TABLE_NAME = 'outbox_events'
    AND INDEX_NAME = 'uk_outbox_dedup'
);

SET @dpms_sql = CASE
  WHEN @dpms_outbox_dedup_index_signature = '1:dedup_key:0:FULL:YES'
    THEN 'SELECT 1'
  WHEN @dpms_outbox_dedup_index_signature IS NULL
    THEN 'ALTER TABLE outbox_events
            ADD UNIQUE KEY uk_outbox_dedup (dedup_key) VISIBLE'
  ELSE 'ALTER TABLE outbox_events
          DROP INDEX uk_outbox_dedup,
          ADD UNIQUE KEY uk_outbox_dedup (dedup_key) VISIBLE'
END;

PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

ALTER TABLE outbox_events ALTER INDEX uk_outbox_dedup VISIBLE;

SET @dpms_task_status_index_signature = (
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
    AND INDEX_NAME = 'idx_task_run_status'
);

SET @dpms_sql = CASE
  WHEN @dpms_task_status_index_signature =
       '1:status:1:FULL:YES,2:created_at:1:FULL:YES'
    THEN 'SELECT 1'
  WHEN @dpms_task_status_index_signature IS NULL
    THEN 'ALTER TABLE task_runs
            ADD INDEX idx_task_run_status (status, created_at) VISIBLE'
  ELSE 'ALTER TABLE task_runs
          DROP INDEX idx_task_run_status,
          ADD INDEX idx_task_run_status (status, created_at) VISIBLE'
END;

PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

ALTER TABLE task_runs ALTER INDEX idx_task_run_status VISIBLE;
