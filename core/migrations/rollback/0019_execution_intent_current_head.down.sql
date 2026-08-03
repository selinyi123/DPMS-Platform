-- Manual, fail-closed rollback for 0019.
--
-- Run only with every Core and Worker writer quiesced.  The 0014 schema cannot
-- represent more than one immutable root per lottery, so rollback refuses if
-- any historical generation exists.  It never deletes or rewrites root rows.

DROP TEMPORARY TABLE IF EXISTS dpms_0019_rollback_root_guard;

CREATE TEMPORARY TABLE dpms_0019_rollback_root_guard (
  has_multiple_roots TINYINT UNSIGNED NOT NULL,
  CONSTRAINT chk_0019_no_historical_roots
    CHECK (has_multiple_roots = 0) ENFORCED
);

INSERT INTO dpms_0019_rollback_root_guard (has_multiple_roots)
SELECT EXISTS (
  SELECT 1
  FROM lottery_execution_intents
  GROUP BY lottery_id
  HAVING COUNT(*) > 1
  LIMIT 1
);

DROP TEMPORARY TABLE dpms_0019_rollback_root_guard;

-- Retry safely if the unique fence was restored but the process stopped
-- before dropping the head table or deleting the migration ledger.
SET @dpms_0019_rollback_index_signature = (
  SELECT GROUP_CONCAT(
           CONCAT(
             SEQ_IN_INDEX, ':', COLUMN_NAME, ':', NON_UNIQUE, ':',
             COALESCE(CAST(SUB_PART AS CHAR), 'FULL'), ':', IS_VISIBLE
           )
           ORDER BY SEQ_IN_INDEX
           SEPARATOR ','
         )
  FROM information_schema.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'lottery_execution_intents'
    AND INDEX_NAME = 'uk_lottery_execution_intent_lottery'
);

DROP TEMPORARY TABLE IF EXISTS dpms_0019_rollback_index_guard;

CREATE TEMPORARY TABLE dpms_0019_rollback_index_guard (
  safe_signature TINYINT UNSIGNED NOT NULL,
  CONSTRAINT chk_0019_rollback_index_signature
    CHECK (safe_signature = 1) ENFORCED
);

INSERT INTO dpms_0019_rollback_index_guard (safe_signature)
VALUES (
  @dpms_0019_rollback_index_signature IS NULL
  OR @dpms_0019_rollback_index_signature =
     '1:lottery_id:0:FULL:YES'
);

DROP TEMPORARY TABLE dpms_0019_rollback_index_guard;

SET @dpms_sql = CASE
  WHEN @dpms_0019_rollback_index_signature IS NULL
    THEN 'ALTER TABLE lottery_execution_intents
            ADD UNIQUE KEY uk_lottery_execution_intent_lottery (lottery_id)'
  ELSE 'SELECT 1'
END;

PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

-- The unique index installation is the final concurrency-safe proof that no
-- second root appeared after the guard scan.  Only the derived current pointer
-- is removed; immutable intent history is never deleted by this rollback.
DROP TABLE IF EXISTS lottery_execution_intent_heads;

DELETE FROM schema_migrations WHERE version = '0019';
