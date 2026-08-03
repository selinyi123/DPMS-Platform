-- Manual rollback for 0015. The old global (status, id) index remains.

SET @dpms_sql = IF(
  (SELECT COUNT(*)
   FROM information_schema.STATISTICS
   WHERE TABLE_SCHEMA = DATABASE()
     AND TABLE_NAME = 'outbox_events'
     AND INDEX_NAME = 'idx_outbox_stream_status_id') > 0,
  'ALTER TABLE outbox_events
     DROP INDEX idx_outbox_stream_status_id',
  'SELECT 1'
);

PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

DELETE FROM schema_migrations WHERE version = '0015';
