-- Optional retry-safe rollback for the readiness risk-range index.
-- InnoDB may discard the implicit single-column account_id index after the
-- composite index becomes a valid FK support index. Recreate an explicit
-- fallback in the same ALTER before dropping the composite when necessary.

SET @dpms_risk_account_created_index_exists = (
  SELECT COUNT(*)
  FROM information_schema.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'risk_events'
    AND INDEX_NAME = 'idx_risk_account_created_id'
);

SET @dpms_risk_alternative_account_index_exists = (
  SELECT COUNT(*)
  FROM information_schema.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'risk_events'
    AND INDEX_NAME <> 'idx_risk_account_created_id'
    AND SEQ_IN_INDEX = 1
    AND COLUMN_NAME = 'account_id'
);

SET @dpms_sql = CASE
  WHEN @dpms_risk_account_created_index_exists = 0
    THEN 'SELECT 1'
  WHEN @dpms_risk_alternative_account_index_exists > 0
    THEN 'ALTER TABLE risk_events
            DROP INDEX idx_risk_account_created_id'
  ELSE 'ALTER TABLE risk_events
          ADD INDEX idx_risk_events_account_fk_rollback (account_id),
          DROP INDEX idx_risk_account_created_id'
END;

PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

DELETE FROM schema_migrations WHERE version = '0021';
