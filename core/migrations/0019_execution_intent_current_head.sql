-- Preserve immutable execution-intent roots across full-plan generations.
--
-- 0014 intentionally allowed only one root per lottery.  This additive
-- migration moves "which root is current" into a separate, generation-fenced
-- pointer without rewriting or deleting the original root.  Deploy this
-- migration with all old Core writers quiesced, then start code which writes a
-- new root and advances this pointer in the same database transaction.

CREATE TABLE IF NOT EXISTS lottery_execution_intent_heads (
  lottery_id BIGINT NOT NULL,
  current_intent_id CHAR(36) NOT NULL,
  generation BIGINT UNSIGNED NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (lottery_id),
  UNIQUE KEY uk_lottery_execution_intent_head_identity (
    current_intent_id,
    lottery_id
  ),
  CONSTRAINT chk_lottery_execution_intent_head_generation
    CHECK (generation > 0) ENFORCED,
  CONSTRAINT fk_lottery_execution_intent_head_root
    FOREIGN KEY (current_intent_id, lottery_id)
    REFERENCES lottery_execution_intents(intent_id, lottery_id)
) ENGINE=InnoDB;

-- CREATE TABLE IF NOT EXISTS is retry-safe but accepts a same-named drifted
-- table.  Refuse to remove 0014's one-root fence until the complete replacement
-- contract is present.  The temporary CHECK guard leaves no stored procedure
-- or other durable helper behind when it fails.
SET @dpms_0019_ready_table = (
  SELECT COUNT(*)
  FROM information_schema.TABLES
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'lottery_execution_intent_heads'
    AND TABLE_TYPE = 'BASE TABLE'
    AND ENGINE = 'InnoDB'
);

SET @dpms_0019_ready_columns = (
  SELECT
    COUNT(*) = 5
    AND COALESCE(
      SUM(
        CASE
          WHEN COLUMN_NAME = 'lottery_id'
            AND ORDINAL_POSITION = 1
            AND LOWER(COLUMN_TYPE) = 'bigint'
            AND IS_NULLABLE = 'NO'
            AND COLUMN_DEFAULT IS NULL
            AND EXTRA = ''
            THEN 1
          WHEN COLUMN_NAME = 'current_intent_id'
            AND ORDINAL_POSITION = 2
            AND LOWER(COLUMN_TYPE) = 'char(36)'
            AND IS_NULLABLE = 'NO'
            AND COLUMN_DEFAULT IS NULL
            AND EXTRA = ''
            THEN 1
          WHEN COLUMN_NAME = 'generation'
            AND ORDINAL_POSITION = 3
            AND LOWER(COLUMN_TYPE) = 'bigint unsigned'
            AND IS_NULLABLE = 'NO'
            AND COLUMN_DEFAULT IS NULL
            AND EXTRA = ''
            THEN 1
          WHEN COLUMN_NAME = 'created_at'
            AND ORDINAL_POSITION = 4
            AND LOWER(COLUMN_TYPE) = 'timestamp'
            AND IS_NULLABLE = 'NO'
            AND COLUMN_DEFAULT = 'CURRENT_TIMESTAMP'
            AND UPPER(EXTRA) = 'DEFAULT_GENERATED'
            THEN 1
          WHEN COLUMN_NAME = 'updated_at'
            AND ORDINAL_POSITION = 5
            AND LOWER(COLUMN_TYPE) = 'timestamp'
            AND IS_NULLABLE = 'NO'
            AND COLUMN_DEFAULT = 'CURRENT_TIMESTAMP'
            AND UPPER(EXTRA) = 'DEFAULT_GENERATED'
            THEN 1
          ELSE 0
        END
      ),
      0
    ) = 5
  FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'lottery_execution_intent_heads'
);

SET @dpms_0019_primary_signature = (
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
    AND TABLE_NAME = 'lottery_execution_intent_heads'
    AND INDEX_NAME = 'PRIMARY'
);

SET @dpms_0019_identity_signature = (
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
    AND TABLE_NAME = 'lottery_execution_intent_heads'
    AND INDEX_NAME = 'uk_lottery_execution_intent_head_identity'
);

SET @dpms_0019_ready_indexes = (
  SELECT
    COUNT(DISTINCT INDEX_NAME) = 2
    AND @dpms_0019_primary_signature =
        '1:lottery_id:0:FULL:YES'
    AND @dpms_0019_identity_signature =
        '1:current_intent_id:0:FULL:YES,2:lottery_id:0:FULL:YES'
  FROM information_schema.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'lottery_execution_intent_heads'
);

SET @dpms_0019_head_fk_signature = (
  SELECT GROUP_CONCAT(
           CONCAT(
             kcu.ORDINAL_POSITION, ':', kcu.COLUMN_NAME, ':',
             kcu.REFERENCED_TABLE_NAME, ':',
             kcu.REFERENCED_COLUMN_NAME
           )
           ORDER BY kcu.ORDINAL_POSITION
           SEPARATOR ','
         )
  FROM information_schema.KEY_COLUMN_USAGE AS kcu
  WHERE kcu.CONSTRAINT_SCHEMA = DATABASE()
    AND kcu.TABLE_NAME = 'lottery_execution_intent_heads'
    AND kcu.CONSTRAINT_NAME = 'fk_lottery_execution_intent_head_root'
);

SET @dpms_0019_ready_foreign_key = (
  SELECT COUNT(*) = 1
  FROM information_schema.REFERENTIAL_CONSTRAINTS
  WHERE CONSTRAINT_SCHEMA = DATABASE()
    AND TABLE_NAME = 'lottery_execution_intent_heads'
    AND CONSTRAINT_NAME = 'fk_lottery_execution_intent_head_root'
    AND REFERENCED_TABLE_NAME = 'lottery_execution_intents'
    AND DELETE_RULE IN ('RESTRICT', 'NO ACTION')
    AND UPDATE_RULE IN ('RESTRICT', 'NO ACTION')
    AND @dpms_0019_head_fk_signature =
        '1:current_intent_id:lottery_execution_intents:intent_id,'
        '2:lottery_id:lottery_execution_intents:lottery_id'
);

SET @dpms_0019_ready_check = (
  SELECT COUNT(*) = 1
  FROM information_schema.TABLE_CONSTRAINTS AS tc
  JOIN information_schema.CHECK_CONSTRAINTS AS cc
    ON cc.CONSTRAINT_SCHEMA = tc.CONSTRAINT_SCHEMA
   AND cc.CONSTRAINT_NAME = tc.CONSTRAINT_NAME
  WHERE tc.CONSTRAINT_SCHEMA = DATABASE()
    AND tc.TABLE_NAME = 'lottery_execution_intent_heads'
    AND tc.CONSTRAINT_NAME =
        'chk_lottery_execution_intent_head_generation'
    AND tc.CONSTRAINT_TYPE = 'CHECK'
    AND tc.ENFORCED = 'YES'
    AND LOWER(
          REPLACE(
            REPLACE(
              REPLACE(
                REPLACE(cc.CHECK_CLAUSE, '`', ''),
                ' ',
                ''
              ),
              '(',
              ''
            ),
            ')',
            ''
          )
        ) = 'generation>0'
);

SET @dpms_0019_ready_constraints = (
  SELECT COUNT(*) = 4
  FROM information_schema.TABLE_CONSTRAINTS
  WHERE CONSTRAINT_SCHEMA = DATABASE()
    AND TABLE_NAME = 'lottery_execution_intent_heads'
);

DROP TEMPORARY TABLE IF EXISTS dpms_0019_head_contract_guard;

CREATE TEMPORARY TABLE dpms_0019_head_contract_guard (
  ready_table TINYINT UNSIGNED NOT NULL,
  ready_columns TINYINT UNSIGNED NOT NULL,
  ready_indexes TINYINT UNSIGNED NOT NULL,
  ready_foreign_key TINYINT UNSIGNED NOT NULL,
  ready_check TINYINT UNSIGNED NOT NULL,
  ready_constraints TINYINT UNSIGNED NOT NULL,
  CONSTRAINT chk_0019_head_table_ready
    CHECK (ready_table = 1) ENFORCED,
  CONSTRAINT chk_0019_head_columns_ready
    CHECK (ready_columns = 1) ENFORCED,
  CONSTRAINT chk_0019_head_indexes_ready
    CHECK (ready_indexes = 1) ENFORCED,
  CONSTRAINT chk_0019_head_foreign_key_ready
    CHECK (ready_foreign_key = 1) ENFORCED,
  CONSTRAINT chk_0019_head_check_ready
    CHECK (ready_check = 1) ENFORCED,
  CONSTRAINT chk_0019_head_constraints_ready
    CHECK (ready_constraints = 1) ENFORCED
);

INSERT INTO dpms_0019_head_contract_guard (
  ready_table,
  ready_columns,
  ready_indexes,
  ready_foreign_key,
  ready_check,
  ready_constraints
) VALUES (
  @dpms_0019_ready_table,
  @dpms_0019_ready_columns,
  @dpms_0019_ready_indexes,
  @dpms_0019_ready_foreign_key,
  @dpms_0019_ready_check,
  @dpms_0019_ready_constraints
);

DROP TEMPORARY TABLE dpms_0019_head_contract_guard;

-- On a fresh 0014 upgrade every lottery has exactly one root.  On a retry
-- after 0019 completed, multiple immutable roots are valid only when their
-- current-head row already exists.  Never choose an arbitrary root when a
-- drifted database has multiple roots but lost its head pointer.
DROP TEMPORARY TABLE IF EXISTS dpms_0019_backfill_guard;

CREATE TEMPORARY TABLE dpms_0019_backfill_guard (
  ambiguous_missing_heads TINYINT UNSIGNED NOT NULL,
  CONSTRAINT chk_0019_unambiguous_head_backfill
    CHECK (ambiguous_missing_heads = 0) ENFORCED
);

INSERT INTO dpms_0019_backfill_guard (ambiguous_missing_heads)
SELECT EXISTS (
  SELECT 1
  FROM (
    SELECT roots.lottery_id
    FROM lottery_execution_intents AS roots
    LEFT JOIN lottery_execution_intent_heads AS heads
      ON heads.lottery_id = roots.lottery_id
    WHERE heads.lottery_id IS NULL
    GROUP BY roots.lottery_id
    HAVING COUNT(*) <> 1
  ) AS ambiguous
  LIMIT 1
);

DROP TEMPORARY TABLE dpms_0019_backfill_guard;

INSERT INTO lottery_execution_intent_heads (
  lottery_id,
  current_intent_id,
  generation
)
SELECT
  roots.lottery_id,
  roots.intent_id,
  1
FROM lottery_execution_intents AS roots
LEFT JOIN lottery_execution_intent_heads AS heads
  ON heads.lottery_id = roots.lottery_id
WHERE heads.lottery_id IS NULL;

DROP TEMPORARY TABLE IF EXISTS dpms_0019_coverage_guard;

CREATE TEMPORARY TABLE dpms_0019_coverage_guard (
  missing_heads TINYINT UNSIGNED NOT NULL,
  invalid_heads TINYINT UNSIGNED NOT NULL,
  CONSTRAINT chk_0019_all_roots_have_head
    CHECK (missing_heads = 0) ENFORCED,
  CONSTRAINT chk_0019_all_heads_reference_root
    CHECK (invalid_heads = 0) ENFORCED
);

INSERT INTO dpms_0019_coverage_guard (missing_heads, invalid_heads)
VALUES (
  EXISTS (
    SELECT 1
    FROM lottery_execution_intents AS roots
    LEFT JOIN lottery_execution_intent_heads AS heads
      ON heads.lottery_id = roots.lottery_id
    WHERE heads.lottery_id IS NULL
    LIMIT 1
  ),
  EXISTS (
    SELECT 1
    FROM lottery_execution_intent_heads AS heads
    LEFT JOIN lottery_execution_intents AS roots
      ON roots.intent_id = heads.current_intent_id
     AND roots.lottery_id = heads.lottery_id
    WHERE roots.intent_id IS NULL
       OR heads.generation < 1
    LIMIT 1
  )
);

DROP TEMPORARY TABLE dpms_0019_coverage_guard;

-- The destructive-looking step only removes the obsolete uniqueness fence;
-- all immutable root rows remain.  Accept exactly the published 0014 index or
-- an already-completed retry where it is absent, and reject same-name drift.
SET @dpms_0019_legacy_lottery_index_signature = (
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

DROP TEMPORARY TABLE IF EXISTS dpms_0019_legacy_index_guard;

CREATE TEMPORARY TABLE dpms_0019_legacy_index_guard (
  safe_signature TINYINT UNSIGNED NOT NULL,
  CONSTRAINT chk_0019_legacy_index_signature
    CHECK (safe_signature = 1) ENFORCED
);

INSERT INTO dpms_0019_legacy_index_guard (safe_signature)
VALUES (
  @dpms_0019_legacy_lottery_index_signature IS NULL
  OR @dpms_0019_legacy_lottery_index_signature =
     '1:lottery_id:0:FULL:YES'
);

DROP TEMPORARY TABLE dpms_0019_legacy_index_guard;

SET @dpms_sql = CASE
  WHEN @dpms_0019_legacy_lottery_index_signature =
       '1:lottery_id:0:FULL:YES'
    THEN 'ALTER TABLE lottery_execution_intents
            DROP INDEX uk_lottery_execution_intent_lottery'
  ELSE 'SELECT 1'
END;

PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;
