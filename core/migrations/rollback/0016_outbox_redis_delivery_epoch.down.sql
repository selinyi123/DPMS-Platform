-- Manual rollback for 0016.  Removing this column disables safe replay after
-- a Redis process-epoch change and should only be done with Core stopped.

SET @dpms_sql = IF(
  (SELECT COUNT(*)
   FROM information_schema.COLUMNS
   WHERE TABLE_SCHEMA = DATABASE()
     AND TABLE_NAME = 'outbox_events'
     AND COLUMN_NAME = 'redis_delivery_epoch') > 0,
  'ALTER TABLE outbox_events
     DROP COLUMN redis_delivery_epoch',
  'SELECT 1'
);

PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

DELETE FROM schema_migrations WHERE version = '0016';
