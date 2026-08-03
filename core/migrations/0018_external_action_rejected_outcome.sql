-- Weibo can receive an explicit API rejection that proves the requested
-- remote action did not take effect.  Worker records that settled failure as
-- outcome='rejected'; extend the shared lifecycle contract without rewriting
-- the already-published 0010/0011 migration bytes.
--
-- The DROP/ADD pair is one atomic MySQL 8 ALTER.  If DDL completed but the
-- migration ledger write did not, retrying replaces the same constraint with
-- the same definition.

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
           ''retry'', ''limit'', ''skip'', ''captcha'', ''risk'', ''auth'',
           ''rejected''
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
