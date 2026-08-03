-- Manual, fail-closed rollback for 0018.
--
-- Run only with Core and Worker quiesced.  The pre-0018 CHECK cannot represent
-- outcome='rejected', so rollback refuses while any such durable intent
-- exists.  The connection-scoped guard leaves no procedure behind on failure.
-- A concurrent rejected write between the scan and ALTER is also rejected by
-- the old CHECK while MySQL installs it.

DROP TEMPORARY TABLE IF EXISTS dpms_0018_rollback_guard;

CREATE TEMPORARY TABLE dpms_0018_rollback_guard (
  has_rejected TINYINT UNSIGNED NOT NULL,
  CONSTRAINT chk_0018_no_rejected_outcomes
    CHECK (has_rejected = 0) ENFORCED
);

INSERT INTO dpms_0018_rollback_guard (has_rejected)
SELECT EXISTS (
  SELECT 1
  FROM external_action_intents
  WHERE outcome = 'rejected'
  LIMIT 1
);

DROP TEMPORARY TABLE dpms_0018_rollback_guard;

SET @dpms_external_action_lifecycle_v2_exists = (
  SELECT COUNT(*)
  FROM information_schema.TABLE_CONSTRAINTS
  WHERE CONSTRAINT_SCHEMA = DATABASE()
    AND TABLE_NAME = 'external_action_intents'
    AND CONSTRAINT_NAME = 'chk_external_action_lifecycle_v2'
    AND CONSTRAINT_TYPE = 'CHECK'
);

SET @dpms_external_action_lifecycle_v2_definition =
  'ADD CONSTRAINT chk_external_action_lifecycle_v2 CHECK (
       (status = ''pending'' AND attempt_no = 0 AND started_at IS NULL
         AND completed_at IS NULL AND outcome IS NULL)
       OR (status = ''prepared'' AND attempt_no > 0 AND started_at IS NULL
         AND completed_at IS NULL AND outcome IS NULL)
       OR (status = ''started'' AND attempt_no > 0 AND started_at IS NOT NULL
         AND completed_at IS NULL AND outcome IS NULL)
       OR (status = ''succeeded'' AND attempt_no > 0
         AND started_at IS NOT NULL AND completed_at IS NOT NULL
         AND outcome IS NOT NULL
         AND outcome = ''ok'')
       OR (status = ''failed'' AND attempt_no > 0
         AND started_at IS NOT NULL AND completed_at IS NOT NULL
         AND outcome IS NOT NULL
         AND outcome IN (
           ''retry'', ''limit'', ''skip'', ''captcha'', ''risk'', ''auth''
         ))
       OR (status = ''unknown'' AND attempt_no > 0
         AND started_at IS NOT NULL AND completed_at IS NOT NULL
         AND outcome IS NOT NULL
         AND outcome = ''unknown'' AND reconciliation_note IS NOT NULL
         AND CHAR_LENGTH(TRIM(reconciliation_note)) > 0)
     ) ENFORCED';

SET @dpms_sql = CONCAT(
  'ALTER TABLE external_action_intents ',
  IF(
    @dpms_external_action_lifecycle_v2_exists > 0,
    'DROP CHECK chk_external_action_lifecycle_v2, ',
    ''
  ),
  @dpms_external_action_lifecycle_v2_definition
);

PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

-- Delete the ledger only after the old physical constraint is restored.
DELETE FROM schema_migrations WHERE version = '0018';
