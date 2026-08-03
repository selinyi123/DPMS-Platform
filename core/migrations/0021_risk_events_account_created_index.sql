-- Bound account-scoped readiness risk scans to one account/time index range.
-- The signature guard repairs a same-named drifted or invisible index and
-- makes a retry safe when MySQL committed DDL before schema_migrations was
-- recorded.

SET @dpms_risk_account_created_index_signature = (
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
    AND TABLE_NAME = 'risk_events'
    AND INDEX_NAME = 'idx_risk_account_created_id'
);

SET @dpms_sql = CASE
  WHEN @dpms_risk_account_created_index_signature =
       '1:account_id:1:FULL:YES,2:created_at:1:FULL:YES,3:id:1:FULL:YES'
    THEN 'SELECT 1'
  WHEN @dpms_risk_account_created_index_signature =
       '1:account_id:1:FULL:NO,2:created_at:1:FULL:NO,3:id:1:FULL:NO'
    THEN 'ALTER TABLE risk_events
            ALTER INDEX idx_risk_account_created_id VISIBLE'
  WHEN @dpms_risk_account_created_index_signature IS NULL
    THEN 'ALTER TABLE risk_events
            ADD INDEX idx_risk_account_created_id
              (account_id, created_at, id) VISIBLE'
  ELSE 'ALTER TABLE risk_events
          DROP INDEX idx_risk_account_created_id,
          ADD INDEX idx_risk_account_created_id
            (account_id, created_at, id) VISIBLE'
END;

PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

-- A rollback may have recreated this single-column index solely to keep the
-- account_id foreign key valid while dropping the composite index.  Once the
-- exact composite index is back, retaining that prefix duplicate adds write
-- amplification without helping the FK or readiness query.
SET @dpms_risk_rollback_fallback_exists = (
  SELECT COUNT(*)
  FROM information_schema.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'risk_events'
    AND INDEX_NAME = 'idx_risk_events_account_fk_rollback'
);

SET @dpms_sql = IF(
  @dpms_risk_rollback_fallback_exists > 0,
  'ALTER TABLE risk_events
     DROP INDEX idx_risk_events_account_fk_rollback',
  'SELECT 1'
);

PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;
