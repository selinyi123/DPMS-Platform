-- Repair every known legacy/partially-applied 0010 schema shape.  Each DDL
-- operation is independently guarded because MySQL commits DDL separately
-- from the schema_migrations version row.

-- Account execution identity is a monotonically increasing fencing value.
SET @dpms_sql = IF(
  (SELECT COUNT(*) FROM information_schema.COLUMNS
   WHERE TABLE_SCHEMA = DATABASE()
     AND TABLE_NAME = 'accounts'
     AND COLUMN_NAME = 'execution_revision') > 0,
  'SELECT 1',
  'ALTER TABLE accounts
     ADD COLUMN execution_revision BIGINT UNSIGNED NOT NULL DEFAULT 1 AFTER version'
);
PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

UPDATE accounts
SET execution_revision = 1
WHERE execution_revision IS NULL OR execution_revision = 0;

SET @dpms_sql = IF(
  (SELECT COUNT(*) FROM information_schema.COLUMNS
   WHERE TABLE_SCHEMA = DATABASE()
     AND TABLE_NAME = 'accounts'
     AND COLUMN_NAME = 'execution_revision'
     AND LOWER(COLUMN_TYPE) = 'bigint unsigned'
     AND IS_NULLABLE = 'NO'
     AND CAST(COLUMN_DEFAULT AS CHAR) = '1') > 0,
  'SELECT 1',
  'ALTER TABLE accounts
     MODIFY COLUMN execution_revision BIGINT UNSIGNED NOT NULL DEFAULT 1'
);
PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

SET @dpms_sql = IF(
  (SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS
   WHERE CONSTRAINT_SCHEMA = DATABASE()
     AND TABLE_NAME = 'accounts'
     AND CONSTRAINT_NAME = 'chk_account_execution_revision'
     AND CONSTRAINT_TYPE = 'CHECK') > 0,
  'SELECT 1',
  'ALTER TABLE accounts
     ADD CONSTRAINT chk_account_execution_revision CHECK (execution_revision > 0)'
);
PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

-- Add observation fields one at a time.  Old 0010 variants added the other
-- binding fields in the same table ALTER, so column-level guards are needed
-- for databases stopped between later repair attempts.
SET @dpms_sql = IF(
  (SELECT COUNT(*) FROM information_schema.COLUMNS
   WHERE TABLE_SCHEMA = DATABASE()
     AND TABLE_NAME = 'adapter_calibrations'
     AND COLUMN_NAME = 'observation_kind') > 0,
  'SELECT 1',
  'ALTER TABLE adapter_calibrations ADD COLUMN observation_kind VARCHAR(64) NULL'
);
PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

SET @dpms_sql = IF(
  (SELECT COUNT(*) FROM information_schema.COLUMNS
   WHERE TABLE_SCHEMA = DATABASE()
     AND TABLE_NAME = 'adapter_calibrations'
     AND COLUMN_NAME = 'observation_hash') > 0,
  'SELECT 1',
  'ALTER TABLE adapter_calibrations ADD COLUMN observation_hash CHAR(64) NULL'
);
PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

SET @dpms_sql = IF(
  (SELECT COUNT(*) FROM information_schema.COLUMNS
   WHERE TABLE_SCHEMA = DATABASE()
     AND TABLE_NAME = 'task_runs'
     AND COLUMN_NAME = 'preflight_observation') > 0,
  'SELECT 1',
  'ALTER TABLE task_runs ADD COLUMN preflight_observation JSON NULL'
);
PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

SET @dpms_sql = IF(
  (SELECT COUNT(*) FROM information_schema.COLUMNS
   WHERE TABLE_SCHEMA = DATABASE()
     AND TABLE_NAME = 'task_runs'
     AND COLUMN_NAME = 'preflight_observation_kind') > 0,
  'SELECT 1',
  'ALTER TABLE task_runs ADD COLUMN preflight_observation_kind VARCHAR(64) NULL'
);
PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

SET @dpms_sql = IF(
  (SELECT COUNT(*) FROM information_schema.COLUMNS
   WHERE TABLE_SCHEMA = DATABASE()
     AND TABLE_NAME = 'task_runs'
     AND COLUMN_NAME = 'preflight_observation_hash') > 0,
  'SELECT 1',
  'ALTER TABLE task_runs ADD COLUMN preflight_observation_hash CHAR(64) NULL'
);
PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

SET @dpms_sql = IF(
  (SELECT COUNT(*) FROM information_schema.COLUMNS
   WHERE TABLE_SCHEMA = DATABASE()
     AND TABLE_NAME = 'execution_evidence_bindings'
     AND COLUMN_NAME = 'probe_observation_kind') > 0,
  'SELECT 1',
  'ALTER TABLE execution_evidence_bindings
     ADD COLUMN probe_observation_kind VARCHAR(64) NULL AFTER shadow_task_id'
);
PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

SET @dpms_sql = IF(
  (SELECT COUNT(*) FROM information_schema.COLUMNS
   WHERE TABLE_SCHEMA = DATABASE()
     AND TABLE_NAME = 'execution_evidence_bindings'
     AND COLUMN_NAME = 'probe_observation_hash') > 0,
  'SELECT 1',
  'ALTER TABLE execution_evidence_bindings
     ADD COLUMN probe_observation_hash CHAR(64) NULL AFTER probe_observation_kind'
);
PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

SET @dpms_sql = IF(
  (SELECT COUNT(*) FROM information_schema.COLUMNS
   WHERE TABLE_SCHEMA = DATABASE()
     AND TABLE_NAME = 'execution_evidence_bindings'
     AND COLUMN_NAME = 'shadow_observation_kind') > 0,
  'SELECT 1',
  'ALTER TABLE execution_evidence_bindings
     ADD COLUMN shadow_observation_kind VARCHAR(64) NULL AFTER probe_observation_hash'
);
PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

SET @dpms_sql = IF(
  (SELECT COUNT(*) FROM information_schema.COLUMNS
   WHERE TABLE_SCHEMA = DATABASE()
     AND TABLE_NAME = 'execution_evidence_bindings'
     AND COLUMN_NAME = 'shadow_observation_hash') > 0,
  'SELECT 1',
  'ALTER TABLE execution_evidence_bindings
     ADD COLUMN shadow_observation_hash CHAR(64) NULL AFTER shadow_observation_kind'
);
PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

-- Build the v2 observation contract side-by-side.  Old constraints stay in
-- force until every replacement index, CHECK and FK exists, so a crash or a
-- concurrent legacy writer never sees an unconstrained evidence table.
SET @dpms_sql = IF(
  (SELECT COUNT(*) FROM information_schema.STATISTICS
   WHERE TABLE_SCHEMA = DATABASE()
     AND TABLE_NAME = 'adapter_calibrations'
     AND INDEX_NAME = 'uk_adapter_probe_exact_binding_v2') > 0,
  'SELECT 1',
  'ALTER TABLE adapter_calibrations
     ADD UNIQUE KEY uk_adapter_probe_exact_binding_v2 (
       probe_id, lottery_id, account_id, platform, rule_snapshot_id,
       execution_path_id, target_hash, rule_hash, action_plan_hash, config_hash,
       observation_kind, observation_hash
     )'
);
PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

SET @dpms_sql = IF(
  (SELECT COUNT(*) FROM information_schema.STATISTICS
   WHERE TABLE_SCHEMA = DATABASE()
     AND TABLE_NAME = 'adapter_calibrations'
     AND INDEX_NAME = 'idx_adapter_probe_binding_lookup_v2') > 0,
  'SELECT 1',
  'ALTER TABLE adapter_calibrations
     ADD INDEX idx_adapter_probe_binding_lookup_v2 (
       lottery_id, account_id, rule_snapshot_id, execution_path_id,
       target_hash, rule_hash, action_plan_hash, config_hash, status,
       finished_at, observation_kind, observation_hash
     )'
);
PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

SET @dpms_sql = IF(
  (SELECT COUNT(*) FROM information_schema.STATISTICS
   WHERE TABLE_SCHEMA = DATABASE()
     AND TABLE_NAME = 'task_runs'
     AND INDEX_NAME = 'uk_task_run_shadow_binding_v2') > 0,
  'SELECT 1',
  'ALTER TABLE task_runs
     ADD UNIQUE KEY uk_task_run_shadow_binding_v2 (
       task_id, lottery_id, account_id, rule_snapshot_id, execution_path_id,
       target_hash, rule_hash, action_plan_hash, config_hash,
       preflight_observation_kind, preflight_observation_hash
     )'
);
PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

SET @dpms_sql = IF(
  (SELECT COUNT(*) FROM information_schema.STATISTICS
   WHERE TABLE_SCHEMA = DATABASE()
     AND TABLE_NAME = 'task_runs'
     AND INDEX_NAME = 'idx_task_run_shadow_evidence_lookup_v2') > 0,
  'SELECT 1',
  'ALTER TABLE task_runs
     ADD INDEX idx_task_run_shadow_evidence_lookup_v2 (
       lottery_id, account_id, rule_snapshot_id, execution_path_id,
       target_hash, rule_hash, action_plan_hash, config_hash, task_mode, status,
       finished_at, preflight_observation_kind, preflight_observation_hash
     )'
);
PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

-- Do not manufacture observation identity for old evidence.  Verified rows
-- survive only when both exact source observations exist.  Non-authoritative
-- rows are cleared so the new composite FKs cannot accidentally preserve a
-- stale partial binding.
UPDATE execution_evidence_bindings eeb
LEFT JOIN adapter_calibrations probe
  ON probe.probe_id = eeb.probe_id
 AND probe.lottery_id = eeb.lottery_id
 AND probe.account_id = eeb.account_id
 AND probe.platform = eeb.platform
 AND probe.rule_snapshot_id = eeb.rule_snapshot_id
 AND probe.execution_path_id = eeb.execution_path_id
 AND probe.target_hash = eeb.target_hash
 AND probe.rule_hash = eeb.rule_hash
 AND probe.action_plan_hash = eeb.action_plan_hash
 AND probe.config_hash = eeb.config_hash
 AND probe.observation_kind = eeb.probe_observation_kind
 AND probe.observation_hash = eeb.probe_observation_hash
LEFT JOIN task_runs shadow
  ON shadow.task_id = eeb.shadow_task_id
 AND shadow.lottery_id = eeb.lottery_id
 AND shadow.account_id = eeb.account_id
 AND shadow.rule_snapshot_id = eeb.rule_snapshot_id
 AND shadow.execution_path_id = eeb.execution_path_id
 AND shadow.target_hash = eeb.target_hash
 AND shadow.rule_hash = eeb.rule_hash
 AND shadow.action_plan_hash = eeb.action_plan_hash
 AND shadow.config_hash = eeb.config_hash
 AND shadow.preflight_observation_kind = eeb.shadow_observation_kind
 AND shadow.preflight_observation_hash = eeb.shadow_observation_hash
SET eeb.status = 'revoked'
WHERE eeb.status = 'verified'
  AND (
    eeb.probe_observation_kind IS NULL
    OR CHAR_LENGTH(TRIM(eeb.probe_observation_kind)) = 0
    OR eeb.probe_observation_hash IS NULL
    OR NOT REGEXP_LIKE(eeb.probe_observation_hash, '^[0-9a-f]{64}$', 'c')
    OR eeb.shadow_observation_kind IS NULL
    OR CHAR_LENGTH(TRIM(eeb.shadow_observation_kind)) = 0
    OR eeb.shadow_observation_hash IS NULL
    OR NOT REGEXP_LIKE(eeb.shadow_observation_hash, '^[0-9a-f]{64}$', 'c')
    OR probe.probe_id IS NULL
    OR shadow.task_id IS NULL
  );

UPDATE execution_evidence_bindings
SET probe_observation_kind = NULL,
    probe_observation_hash = NULL,
    shadow_observation_kind = NULL,
    shadow_observation_hash = NULL
WHERE status <> 'verified'
  AND (
    probe_observation_kind IS NOT NULL
    OR probe_observation_hash IS NOT NULL
    OR shadow_observation_kind IS NOT NULL
    OR shadow_observation_hash IS NOT NULL
  );

SET @dpms_sql = IF(
  (SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS
   WHERE CONSTRAINT_SCHEMA = DATABASE()
     AND TABLE_NAME = 'execution_evidence_bindings'
     AND CONSTRAINT_NAME = 'chk_execution_evidence_observation_hashes_v2'
     AND CONSTRAINT_TYPE = 'CHECK') > 0,
  'SELECT 1',
  'ALTER TABLE execution_evidence_bindings
     ADD CONSTRAINT chk_execution_evidence_observation_hashes_v2 CHECK (
       status <> ''verified''
       OR (
         probe_observation_hash IS NOT NULL
         AND shadow_observation_hash IS NOT NULL
         AND probe_observation_kind IS NOT NULL
         AND shadow_observation_kind IS NOT NULL
         AND REGEXP_LIKE(probe_observation_hash, ''^[0-9a-f]{64}$'', ''c'')
         AND REGEXP_LIKE(shadow_observation_hash, ''^[0-9a-f]{64}$'', ''c'')
         AND CHAR_LENGTH(TRIM(probe_observation_kind)) > 0
         AND CHAR_LENGTH(TRIM(shadow_observation_kind)) > 0
       )
     ) ENFORCED'
);
PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

SET @dpms_sql = IF(
  (SELECT COUNT(*) FROM information_schema.STATISTICS
   WHERE TABLE_SCHEMA = DATABASE()
     AND TABLE_NAME = 'execution_evidence_bindings'
     AND INDEX_NAME = 'idx_execution_evidence_exact_binding_v2') > 0,
  'SELECT 1',
  'ALTER TABLE execution_evidence_bindings
     ADD INDEX idx_execution_evidence_exact_binding_v2 (
       lottery_id, account_id, platform, rule_snapshot_id, execution_path_id,
       target_hash, rule_hash, action_plan_hash, config_hash,
       probe_observation_kind, probe_observation_hash,
       shadow_observation_kind, shadow_observation_hash,
       status, expires_at, verified_at
     )'
);
PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

-- Add exact observation FKs before retiring their legacy predecessors.
SET @dpms_sql = IF(
  (SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS
   WHERE CONSTRAINT_SCHEMA = DATABASE()
     AND TABLE_NAME = 'execution_evidence_bindings'
     AND CONSTRAINT_NAME = 'fk_execution_evidence_probe_v2'
     AND CONSTRAINT_TYPE = 'FOREIGN KEY') > 0,
  'SELECT 1',
  'ALTER TABLE execution_evidence_bindings
     ADD CONSTRAINT fk_execution_evidence_probe_v2
     FOREIGN KEY (
       probe_id, lottery_id, account_id, platform, rule_snapshot_id,
       execution_path_id, target_hash, rule_hash, action_plan_hash, config_hash,
       probe_observation_kind, probe_observation_hash
     ) REFERENCES adapter_calibrations (
       probe_id, lottery_id, account_id, platform, rule_snapshot_id,
       execution_path_id, target_hash, rule_hash, action_plan_hash, config_hash,
       observation_kind, observation_hash
     ) ON DELETE RESTRICT ON UPDATE RESTRICT'
);
PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

SET @dpms_sql = IF(
  (SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS
   WHERE CONSTRAINT_SCHEMA = DATABASE()
     AND TABLE_NAME = 'execution_evidence_bindings'
     AND CONSTRAINT_NAME = 'fk_execution_evidence_shadow_v2'
     AND CONSTRAINT_TYPE = 'FOREIGN KEY') > 0,
  'SELECT 1',
  'ALTER TABLE execution_evidence_bindings
     ADD CONSTRAINT fk_execution_evidence_shadow_v2
     FOREIGN KEY (
       shadow_task_id, lottery_id, account_id, rule_snapshot_id,
       execution_path_id, target_hash, rule_hash, action_plan_hash, config_hash,
       shadow_observation_kind, shadow_observation_hash
     ) REFERENCES task_runs (
       task_id, lottery_id, account_id, rule_snapshot_id,
       execution_path_id, target_hash, rule_hash, action_plan_hash, config_hash,
       preflight_observation_kind, preflight_observation_hash
     ) ON DELETE RESTRICT ON UPDATE RESTRICT'
);
PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

-- The v2 contract is now complete; retire legacy objects only afterwards.
SET @dpms_sql = IF(
  (SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS
   WHERE CONSTRAINT_SCHEMA = DATABASE()
     AND TABLE_NAME = 'execution_evidence_bindings'
     AND CONSTRAINT_NAME = 'fk_execution_evidence_probe'
     AND CONSTRAINT_TYPE = 'FOREIGN KEY') > 0,
  'ALTER TABLE execution_evidence_bindings DROP FOREIGN KEY fk_execution_evidence_probe',
  'SELECT 1'
);
PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

SET @dpms_sql = IF(
  (SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS
   WHERE CONSTRAINT_SCHEMA = DATABASE()
     AND TABLE_NAME = 'execution_evidence_bindings'
     AND CONSTRAINT_NAME = 'fk_execution_evidence_shadow'
     AND CONSTRAINT_TYPE = 'FOREIGN KEY') > 0,
  'ALTER TABLE execution_evidence_bindings DROP FOREIGN KEY fk_execution_evidence_shadow',
  'SELECT 1'
);
PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

SET @dpms_sql = IF(
  (SELECT COUNT(*) FROM information_schema.STATISTICS
   WHERE TABLE_SCHEMA = DATABASE()
     AND TABLE_NAME = 'adapter_calibrations'
     AND INDEX_NAME = 'uk_adapter_probe_exact_binding') > 0,
  'ALTER TABLE adapter_calibrations DROP INDEX uk_adapter_probe_exact_binding',
  'SELECT 1'
);
PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

SET @dpms_sql = IF(
  (SELECT COUNT(*) FROM information_schema.STATISTICS
   WHERE TABLE_SCHEMA = DATABASE()
     AND TABLE_NAME = 'adapter_calibrations'
     AND INDEX_NAME = 'idx_adapter_probe_binding_lookup') > 0,
  'ALTER TABLE adapter_calibrations DROP INDEX idx_adapter_probe_binding_lookup',
  'SELECT 1'
);
PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

SET @dpms_sql = IF(
  (SELECT COUNT(*) FROM information_schema.STATISTICS
   WHERE TABLE_SCHEMA = DATABASE()
     AND TABLE_NAME = 'task_runs'
     AND INDEX_NAME = 'uk_task_run_shadow_binding') > 0,
  'ALTER TABLE task_runs DROP INDEX uk_task_run_shadow_binding',
  'SELECT 1'
);
PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

SET @dpms_sql = IF(
  (SELECT COUNT(*) FROM information_schema.STATISTICS
   WHERE TABLE_SCHEMA = DATABASE()
     AND TABLE_NAME = 'task_runs'
     AND INDEX_NAME = 'idx_task_run_shadow_evidence_lookup') > 0,
  'ALTER TABLE task_runs DROP INDEX idx_task_run_shadow_evidence_lookup',
  'SELECT 1'
);
PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

SET @dpms_sql = IF(
  (SELECT COUNT(*) FROM information_schema.STATISTICS
   WHERE TABLE_SCHEMA = DATABASE()
     AND TABLE_NAME = 'execution_evidence_bindings'
     AND INDEX_NAME = 'idx_execution_evidence_exact_binding') > 0,
  'ALTER TABLE execution_evidence_bindings DROP INDEX idx_execution_evidence_exact_binding',
  'SELECT 1'
);
PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

SET @dpms_sql = IF(
  (SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS
   WHERE CONSTRAINT_SCHEMA = DATABASE()
     AND TABLE_NAME = 'execution_evidence_bindings'
     AND CONSTRAINT_NAME = 'chk_execution_evidence_observation_hashes'
     AND CONSTRAINT_TYPE = 'CHECK') > 0,
  'ALTER TABLE execution_evidence_bindings DROP CHECK chk_execution_evidence_observation_hashes',
  'SELECT 1'
);
PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

-- A legacy failed intent does not prove that the remote side did nothing.
-- Quarantine its task and migrate the intent to explicit uncertainty before
-- enforcing the status/certainty state machine.
SET @dpms_sql = IF(
  (SELECT COUNT(*) FROM information_schema.COLUMNS
   WHERE TABLE_SCHEMA = DATABASE()
     AND TABLE_NAME = 'external_action_intents'
     AND COLUMN_NAME = 'effect_certainty') > 0,
  'SELECT 1',
  'ALTER TABLE external_action_intents
     ADD COLUMN effect_certainty VARCHAR(32) NOT NULL DEFAULT ''not_started'' AFTER status'
);
PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

UPDATE task_runs tr
JOIN external_action_intents eai ON eai.task_id = tr.task_id
SET tr.reconciliation_required = 1
WHERE eai.status IN ('failed', 'started', 'unknown')
   OR (eai.status = 'succeeded' AND tr.status <> 'succeeded')
   OR (tr.status = 'succeeded' AND eai.status <> 'succeeded');

UPDATE external_action_intents
SET status = 'unknown',
    outcome = 'unknown',
    effect_certainty = 'unknown',
    reconciliation_note = CONCAT_WS(
      CHAR(10),
      NULLIF(reconciliation_note, ''),
      'Legacy failed outcome migrated conservatively: remote effect is unknown'
    ),
    updated_at = updated_at
WHERE status = 'failed';

UPDATE external_action_intents
SET effect_certainty = CASE
  WHEN status IN ('pending', 'prepared') THEN 'not_started'
  WHEN status IN ('started', 'unknown') THEN 'unknown'
  WHEN status = 'succeeded' THEN 'confirmed_effect'
  ELSE 'unknown'
END,
    updated_at = updated_at
WHERE NOT (
  effect_certainty <=> CASE
    WHEN status IN ('pending', 'prepared') THEN 'not_started'
    WHEN status IN ('started', 'unknown') THEN 'unknown'
    WHEN status = 'succeeded' THEN 'confirmed_effect'
    ELSE 'unknown'
  END
);

-- Normalise legacy timestamps/outcomes before installing the lifecycle CHECK.
-- A legacy unknown row is quarantined, never promoted to a known failure.
UPDATE external_action_intents
SET attempt_no = CASE
      WHEN status = 'pending' THEN 0
      WHEN attempt_no = 0 THEN 1
      ELSE attempt_no
    END,
    started_at = CASE
      WHEN status IN ('pending', 'prepared') THEN NULL
      ELSE COALESCE(started_at, created_at)
    END,
    completed_at = CASE
      WHEN status IN ('succeeded', 'failed', 'unknown')
        THEN COALESCE(completed_at, updated_at, NOW())
      ELSE NULL
    END,
    outcome = CASE
      WHEN status = 'succeeded' THEN 'ok'
      WHEN status = 'unknown' THEN 'unknown'
      WHEN status = 'failed'
        AND outcome IN ('retry', 'limit', 'skip', 'captcha', 'risk', 'auth')
        THEN outcome
      ELSE NULL
    END,
    reconciliation_note = CASE
      WHEN status = 'unknown' THEN COALESCE(
        NULLIF(TRIM(reconciliation_note), ''),
        'Legacy uncertain outcome requires reconciliation'
      )
      ELSE reconciliation_note
    END,
    updated_at = updated_at
WHERE NOT (
      attempt_no <=> CASE
        WHEN status = 'pending' THEN 0
        WHEN attempt_no = 0 THEN 1
        ELSE attempt_no
      END
    )
   OR NOT (
      started_at <=> CASE
        WHEN status IN ('pending', 'prepared') THEN NULL
        ELSE COALESCE(started_at, created_at)
      END
    )
   OR NOT (
      completed_at <=> CASE
        WHEN status IN ('succeeded', 'failed', 'unknown')
          THEN COALESCE(completed_at, updated_at, NOW())
        ELSE NULL
      END
    )
   OR NOT (
      outcome <=> CASE
        WHEN status = 'succeeded' THEN 'ok'
        WHEN status = 'unknown' THEN 'unknown'
        WHEN status = 'failed'
          AND outcome IN ('retry', 'limit', 'skip', 'captcha', 'risk', 'auth')
          THEN outcome
        ELSE NULL
      END
    )
   OR NOT (
      reconciliation_note <=> CASE
        WHEN status = 'unknown' THEN COALESCE(
          NULLIF(TRIM(reconciliation_note), ''),
          'Legacy uncertain outcome requires reconciliation'
        )
        ELSE reconciliation_note
      END
    );

SET @dpms_sql = IF(
  (SELECT COUNT(*) FROM information_schema.COLUMNS
   WHERE TABLE_SCHEMA = DATABASE()
     AND TABLE_NAME = 'external_action_intents'
     AND COLUMN_NAME = 'effect_certainty'
     AND LOWER(COLUMN_TYPE) = 'varchar(32)'
     AND IS_NULLABLE = 'NO'
     AND COLUMN_DEFAULT = 'not_started') > 0,
  'SELECT 1',
  'ALTER TABLE external_action_intents
     MODIFY COLUMN effect_certainty VARCHAR(32) NOT NULL DEFAULT ''not_started'''
);
PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

-- Install both strict v2 constraints before removing legacy constraints.
SET @dpms_sql = IF(
  (SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS
   WHERE CONSTRAINT_SCHEMA = DATABASE()
     AND TABLE_NAME = 'external_action_intents'
     AND CONSTRAINT_NAME = 'chk_external_action_lifecycle_v2'
     AND CONSTRAINT_TYPE = 'CHECK') > 0,
  'SELECT 1',
  'ALTER TABLE external_action_intents
     ADD CONSTRAINT chk_external_action_lifecycle_v2 CHECK (
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
         AND outcome IN (''retry'', ''limit'', ''skip'', ''captcha'', ''risk'', ''auth''))
       OR (status = ''unknown'' AND attempt_no > 0
         AND started_at IS NOT NULL AND completed_at IS NOT NULL
         AND outcome IS NOT NULL
         AND outcome = ''unknown'' AND reconciliation_note IS NOT NULL
         AND CHAR_LENGTH(TRIM(reconciliation_note)) > 0)
     ) ENFORCED'
);
PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

SET @dpms_sql = IF(
  (SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS
   WHERE CONSTRAINT_SCHEMA = DATABASE()
     AND TABLE_NAME = 'external_action_intents'
     AND CONSTRAINT_NAME = 'chk_external_action_effect_certainty_v2'
     AND CONSTRAINT_TYPE = 'CHECK') > 0,
  'SELECT 1',
  'ALTER TABLE external_action_intents
     ADD CONSTRAINT chk_external_action_effect_certainty_v2 CHECK (
       (status IN (''pending'', ''prepared'') AND effect_certainty = ''not_started'')
       OR (status IN (''started'', ''unknown'') AND effect_certainty = ''unknown'')
       OR (status = ''succeeded'' AND effect_certainty = ''confirmed_effect'')
       OR (status = ''failed'' AND effect_certainty = ''confirmed_no_effect'')
     ) ENFORCED'
);
PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

SET @dpms_sql = IF(
  (SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS
   WHERE CONSTRAINT_SCHEMA = DATABASE()
     AND TABLE_NAME = 'external_action_intents'
     AND CONSTRAINT_NAME = 'chk_external_action_lifecycle'
     AND CONSTRAINT_TYPE = 'CHECK') > 0,
  'ALTER TABLE external_action_intents DROP CHECK chk_external_action_lifecycle',
  'SELECT 1'
);
PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

SET @dpms_sql = IF(
  (SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS
   WHERE CONSTRAINT_SCHEMA = DATABASE()
     AND TABLE_NAME = 'external_action_intents'
     AND CONSTRAINT_NAME = 'chk_external_action_effect_certainty'
     AND CONSTRAINT_TYPE = 'CHECK') > 0,
  'ALTER TABLE external_action_intents DROP CHECK chk_external_action_effect_certainty',
  'SELECT 1'
);
PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;
