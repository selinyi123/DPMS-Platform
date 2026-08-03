-- Manual, fail-closed rollback for 0014.
--
-- Run only with Core and Worker quiesced.  MySQL DDL is not transactional.
-- The script refuses to discard any frozen intent/binding row, verifies the
-- complete legacy nine-column evidence relationship before restoring it, and
-- takes write locks around the final empty-table check/drop to close the race
-- with an in-flight dispatcher.

DROP PROCEDURE IF EXISTS dpms_refuse_0014_rollback;

CREATE PROCEDURE dpms_refuse_0014_rollback()
SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = @dpms_0014_refusal;

SELECT COUNT(*) INTO @dpms_0014_root_rows
  FROM lottery_execution_intents;

SELECT COUNT(*) INTO @dpms_0014_binding_rows
  FROM task_execution_intent_bindings;

SET @dpms_0014_refusal =
  '0014 rollback refused: execution intent tables are not empty';
SET @dpms_sql = IF(
  @dpms_0014_root_rows > 0 OR @dpms_0014_binding_rows > 0,
  'CALL dpms_refuse_0014_rollback()',
  'DO 0'
);
PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

-- A match on execution_evidence_id alone is insufficient.  The legacy FK
-- bound all nine fields, so every task carrying an evidence id must still
-- resolve to the same immutable lottery/account/rule/path/target/config
-- contract before the old constraint can be restored.
SELECT COUNT(*) INTO @dpms_0014_unmatched_evidence_rows
  FROM task_runs AS task
  LEFT JOIN execution_evidence_bindings AS evidence
    ON evidence.id = task.execution_evidence_id
   AND evidence.lottery_id = task.lottery_id
   AND evidence.account_id = task.account_id
   AND evidence.rule_snapshot_id = task.rule_snapshot_id
   AND evidence.execution_path_id = task.execution_path_id
   AND evidence.target_hash = task.target_hash
   AND evidence.rule_hash = task.rule_hash
   AND evidence.action_plan_hash = task.action_plan_hash
   AND evidence.config_hash = task.config_hash
 WHERE task.execution_evidence_id IS NOT NULL
   AND evidence.id IS NULL;

SET @dpms_0014_refusal =
  '0014 rollback refused: task evidence does not match the legacy composite FK';
SET @dpms_sql = IF(
  @dpms_0014_unmatched_evidence_rows > 0,
  'CALL dpms_refuse_0014_rollback()',
  'DO 0'
);
PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

SET @dpms_0014_old_fk_preexisting = (
  SELECT COUNT(*)
    FROM information_schema.TABLE_CONSTRAINTS
   WHERE CONSTRAINT_SCHEMA = DATABASE()
     AND TABLE_NAME = 'task_runs'
     AND CONSTRAINT_NAME = 'fk_task_run_execution_evidence'
     AND CONSTRAINT_TYPE = 'FOREIGN KEY'
) > 0;

SET @dpms_sql = IF(
  @dpms_0014_old_fk_preexisting,
  'DO 0',
  'ALTER TABLE task_runs
     ADD CONSTRAINT fk_task_run_execution_evidence
       FOREIGN KEY (
         execution_evidence_id,
         lottery_id,
         account_id,
         rule_snapshot_id,
         execution_path_id,
         target_hash,
         rule_hash,
         action_plan_hash,
         config_hash
       )
       REFERENCES execution_evidence_bindings (
         id,
         lottery_id,
         account_id,
         rule_snapshot_id,
         execution_path_id,
         target_hash,
         rule_hash,
         action_plan_hash,
         config_hash
       )'
);
PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

-- Fence late dispatchers, recheck while both replacement tables are write
-- locked, then drop the empty child/parent pair in one atomic MySQL 8 DDL.
LOCK TABLES
  task_execution_intent_bindings WRITE,
  lottery_execution_intents WRITE;

SELECT COUNT(*) INTO @dpms_0014_root_rows
  FROM lottery_execution_intents;

SELECT COUNT(*) INTO @dpms_0014_binding_rows
  FROM task_execution_intent_bindings;

SET @dpms_0014_empty_after_lock = (
  @dpms_0014_root_rows = 0 AND @dpms_0014_binding_rows = 0
);
SET @dpms_sql = IF(
  @dpms_0014_empty_after_lock,
  'DROP TABLE task_execution_intent_bindings, lottery_execution_intents',
  'DO 0'
);
PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

UNLOCK TABLES;

-- If a dispatcher won the race before LOCK TABLES, restore the 0014 state
-- before refusing: do not leave the Bilibili-only FK attached to a live
-- multi-platform task table.
SET @dpms_sql = IF(
  NOT @dpms_0014_empty_after_lock
  AND NOT @dpms_0014_old_fk_preexisting,
  'ALTER TABLE task_runs
     DROP FOREIGN KEY fk_task_run_execution_evidence',
  'DO 0'
);
PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

SET @dpms_0014_refusal =
  '0014 rollback refused: execution intent rows appeared during rollback';
SET @dpms_sql = IF(
  @dpms_0014_empty_after_lock,
  'DO 0',
  'CALL dpms_refuse_0014_rollback()'
);
PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

DROP PROCEDURE dpms_refuse_0014_rollback;

-- The physical rollback and migration ledger must move together.  Leaving
-- 0014 recorded would make the current runner skip the required forward
-- migration, while an older runner without the 0014 file would reject the
-- unexplained applied version.
DELETE FROM schema_migrations WHERE version = '0014';
