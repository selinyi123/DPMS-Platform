-- Real-run v2 stores every authorising input as immutable, hash-addressed
-- state.  Nullable columns on existing tables preserve pre-v2 history; the
-- dispatch contract requires them to be populated for every new real run.

CREATE TABLE IF NOT EXISTS lottery_rule_snapshots (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  lottery_id BIGINT NOT NULL,
  platform VARCHAR(32) NOT NULL,
  source_kind VARCHAR(32) NOT NULL,
  source_locator VARCHAR(1024) NOT NULL,
  fetch_method VARCHAR(64) NOT NULL,
  rule_text LONGTEXT NOT NULL,
  rule_hash CHAR(64) NOT NULL,
  is_complete TINYINT UNSIGNED NOT NULL DEFAULT 0,
  attested_by VARCHAR(128) NULL,
  attested_at TIMESTAMP NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  -- Discovery and independently attested snapshots may legitimately contain
  -- the same text/hash.  Keeping this non-unique prevents an incomplete
  -- discovery row from being upgraded in place or blocking an immutable
  -- operator attestation.
  INDEX idx_rule_snapshot_lottery_hash (
    lottery_id,
    rule_hash,
    is_complete,
    created_at
  ),
  UNIQUE KEY uk_rule_snapshot_id_lottery (id, lottery_id),
  INDEX idx_rule_snapshot_attestation (lottery_id, is_complete, attested_at),
  INDEX idx_rule_snapshot_source (platform, source_kind, source_locator(191)),
  CONSTRAINT chk_rule_snapshot_complete CHECK (
    is_complete IN (0, 1)
    AND (
      is_complete = 0
      OR (attested_by IS NOT NULL AND attested_at IS NOT NULL)
    )
  ) ENFORCED,
  CONSTRAINT fk_rule_snapshot_lottery
    FOREIGN KEY (lottery_id) REFERENCES lotteries(id)
) ENGINE=InnoDB;

SET @dpms_sql = IF(
  (SELECT COUNT(*) FROM information_schema.COLUMNS
   WHERE TABLE_SCHEMA = DATABASE()
     AND TABLE_NAME = 'accounts'
     AND COLUMN_NAME = 'execution_revision') > 0,
  'SELECT 1',
  'ALTER TABLE accounts
     ADD COLUMN execution_revision BIGINT UNSIGNED NOT NULL DEFAULT 1 AFTER version,
     ADD CONSTRAINT chk_account_execution_revision CHECK (execution_revision > 0) ENFORCED'
);
PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

-- MySQL 8 atomic DDL makes each guarded ALTER all-or-nothing.  The guards make
-- a retry safe when the server/process stops after an earlier ALTER committed
-- but before schema_migrations was recorded.
SET @dpms_sql = IF(
  (SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS
   WHERE CONSTRAINT_SCHEMA = DATABASE()
     AND TABLE_NAME = 'lotteries'
     AND CONSTRAINT_NAME = 'fk_lottery_authoritative_rule_snapshot') > 0,
  'SELECT 1',
  'ALTER TABLE lotteries
     ADD COLUMN authoritative_rule_snapshot_id BIGINT NULL,
     ADD COLUMN rule_hash CHAR(64) NULL,
     ADD COLUMN action_plan_hash CHAR(64) NULL,
     ADD INDEX idx_lottery_authoritative_snapshot (authoritative_rule_snapshot_id, id),
     ADD INDEX idx_lottery_rule_action_hash (rule_hash, action_plan_hash),
     ADD CONSTRAINT fk_lottery_authoritative_rule_snapshot
       FOREIGN KEY (authoritative_rule_snapshot_id, id)
       REFERENCES lottery_rule_snapshots(id, lottery_id)'
);
PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

CREATE TABLE IF NOT EXISTS account_operation_leases (
  lease_id CHAR(36) PRIMARY KEY,
  account_id BIGINT NOT NULL,
  generation BIGINT UNSIGNED NOT NULL,
  operation_kind VARCHAR(32) NOT NULL,
  owner_id VARCHAR(128) NOT NULL,
  task_id CHAR(36) NULL,
  acquired_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  expires_at TIMESTAMP NOT NULL,
  released_at TIMESTAMP NULL,
  -- Leases are append-only fencing records.  Reusing one row per account
  -- would make the historical task/intent foreign keys either block the next
  -- lease or silently rewrite history via ON UPDATE CASCADE.
  UNIQUE KEY uk_account_operation_generation (account_id, generation),
  UNIQUE KEY uk_account_operation_lease_binding (lease_id, account_id),
  UNIQUE KEY uk_account_operation_lease_fence (
    lease_id,
    account_id,
    generation
  ),
  INDEX idx_account_operation_lease_expiry (account_id, released_at, expires_at),
  INDEX idx_account_operation_lease_owner (owner_id, expires_at),
  CONSTRAINT chk_account_operation_lease_generation CHECK (generation > 0) ENFORCED,
  CONSTRAINT fk_account_operation_lease_account
    FOREIGN KEY (account_id) REFERENCES accounts(id),
  CONSTRAINT fk_account_operation_lease_task
    FOREIGN KEY (task_id) REFERENCES task_runs(task_id)
) ENGINE=InnoDB;

-- Circular insert order for a real task is intentional and FK-safe:
-- (1) INSERT lease with task_id NULL, (2) INSERT task_run with the lease
-- binding, (3) UPDATE that immutable lease generation with its task_id.
-- Probe leases have no task_run and retain task_id NULL.

SET @dpms_sql = IF(
  (SELECT COUNT(*) FROM information_schema.STATISTICS
   WHERE TABLE_SCHEMA = DATABASE()
     AND TABLE_NAME = 'adapter_calibrations'
     AND INDEX_NAME = 'uk_adapter_probe_exact_binding') > 0,
  'SELECT 1',
  'ALTER TABLE adapter_calibrations
     ADD COLUMN execution_path_id VARCHAR(128) NULL,
     ADD COLUMN rule_snapshot_id BIGINT NULL,
     ADD COLUMN target_hash CHAR(64) NULL,
     ADD COLUMN rule_hash CHAR(64) NULL,
     ADD COLUMN action_plan_hash CHAR(64) NULL,
     ADD COLUMN config_hash CHAR(64) NULL,
     ADD COLUMN observation_kind VARCHAR(64) NULL,
     ADD COLUMN observation_hash CHAR(64) NULL,
     ADD COLUMN account_lease_id CHAR(36) NULL,
     ADD COLUMN account_lease_generation BIGINT UNSIGNED NULL,
     ADD UNIQUE KEY uk_adapter_probe_exact_binding (
       probe_id,
       lottery_id,
       account_id,
       platform,
       rule_snapshot_id,
       execution_path_id,
       target_hash,
       rule_hash,
       action_plan_hash,
       config_hash,
       observation_kind,
       observation_hash
     ),
     ADD INDEX idx_adapter_probe_binding_lookup (
       lottery_id,
       account_id,
       rule_snapshot_id,
       execution_path_id,
       target_hash,
       rule_hash,
       action_plan_hash,
       config_hash,
       status,
       finished_at
     ),
     ADD INDEX idx_adapter_probe_account_lease (
       account_lease_id,
       account_id,
       account_lease_generation
     ),
     ADD CONSTRAINT fk_adapter_probe_rule_snapshot
       FOREIGN KEY (rule_snapshot_id, lottery_id)
       REFERENCES lottery_rule_snapshots(id, lottery_id),
     ADD CONSTRAINT fk_adapter_probe_account_lease
       FOREIGN KEY (
         account_lease_id,
         account_id,
         account_lease_generation
       )
       REFERENCES account_operation_leases(lease_id, account_id, generation)'
);
PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

SET @dpms_sql = IF(
  (SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS
   WHERE CONSTRAINT_SCHEMA = DATABASE()
     AND TABLE_NAME = 'task_runs'
     AND CONSTRAINT_NAME = 'fk_task_run_account_lease') > 0,
  'SELECT 1',
  'ALTER TABLE task_runs
     ADD COLUMN rule_snapshot_id BIGINT NULL,
     ADD COLUMN rule_hash CHAR(64) NULL,
     ADD COLUMN action_plan_hash CHAR(64) NULL,
     ADD COLUMN execution_evidence_id CHAR(36) NULL,
     ADD COLUMN execution_path_id VARCHAR(128) NULL,
     ADD COLUMN target_hash CHAR(64) NULL,
     ADD COLUMN config_hash CHAR(64) NULL,
     ADD COLUMN preflight_observation JSON NULL,
     ADD COLUMN preflight_observation_kind VARCHAR(64) NULL,
     ADD COLUMN preflight_observation_hash CHAR(64) NULL,
     ADD COLUMN account_lease_id CHAR(36) NULL,
     ADD COLUMN account_lease_generation BIGINT UNSIGNED NULL,
     ADD COLUMN reconciliation_required TINYINT UNSIGNED NOT NULL DEFAULT 0,
     ADD UNIQUE KEY uk_task_run_entity_binding (task_id, lottery_id, account_id),
     ADD UNIQUE KEY uk_task_run_shadow_binding (
       task_id,
       lottery_id,
       account_id,
       rule_snapshot_id,
       execution_path_id,
       target_hash,
       rule_hash,
       action_plan_hash,
       config_hash,
       preflight_observation_kind,
       preflight_observation_hash
     ),
     ADD INDEX idx_task_run_shadow_evidence_lookup (
       lottery_id,
       account_id,
       rule_snapshot_id,
       execution_path_id,
       target_hash,
       rule_hash,
       action_plan_hash,
       config_hash,
       task_mode,
       status,
       finished_at
     ),
     ADD INDEX idx_task_run_execution_evidence (execution_evidence_id),
     ADD INDEX idx_task_run_account_lease (
       account_lease_id,
       account_id,
       account_lease_generation
     ),
     ADD INDEX idx_task_run_reconciliation (reconciliation_required, status, created_at),
     ADD CONSTRAINT chk_task_run_reconciliation_required
       CHECK (reconciliation_required IN (0, 1)) ENFORCED,
     ADD CONSTRAINT fk_task_run_rule_snapshot
       FOREIGN KEY (rule_snapshot_id, lottery_id)
       REFERENCES lottery_rule_snapshots(id, lottery_id),
     ADD CONSTRAINT fk_task_run_account_lease
       FOREIGN KEY (
         account_lease_id,
         account_id,
         account_lease_generation
       )
       REFERENCES account_operation_leases(lease_id, account_id, generation)'
);
PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

CREATE TABLE IF NOT EXISTS execution_evidence_bindings (
  id CHAR(36) PRIMARY KEY,
  lottery_id BIGINT NOT NULL,
  account_id BIGINT NOT NULL,
  platform VARCHAR(32) NOT NULL,
  rule_snapshot_id BIGINT NOT NULL,
  execution_path_id VARCHAR(128) NOT NULL,
  target_hash CHAR(64) NOT NULL,
  rule_hash CHAR(64) NOT NULL,
  action_plan_hash CHAR(64) NOT NULL,
  config_hash CHAR(64) NOT NULL,
  probe_id CHAR(36) NULL,
  shadow_task_id CHAR(36) NULL,
  -- Nullable only for revoked pre-observation history upgraded by 0011.
  -- Verified evidence must carry all four values via the CHECK below.
  probe_observation_kind VARCHAR(64) NULL,
  probe_observation_hash CHAR(64) NULL,
  shadow_observation_kind VARCHAR(64) NULL,
  shadow_observation_hash CHAR(64) NULL,
  status VARCHAR(32) NOT NULL DEFAULT 'pending',
  verified_at TIMESTAMP NULL,
  expires_at TIMESTAMP NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uk_execution_evidence_task_binding (
    id,
    lottery_id,
    account_id,
    rule_snapshot_id,
    execution_path_id,
    target_hash,
    rule_hash,
    action_plan_hash,
    config_hash
  ),
  INDEX idx_execution_evidence_exact_binding (
    lottery_id,
    account_id,
    platform,
    rule_snapshot_id,
    execution_path_id,
    target_hash,
    rule_hash,
    action_plan_hash,
    config_hash,
    probe_observation_hash,
    shadow_observation_hash,
    status,
    expires_at,
    verified_at
  ),
  INDEX idx_execution_evidence_active (status, expires_at, verified_at),
  INDEX idx_execution_evidence_probe (probe_id),
  INDEX idx_execution_evidence_shadow (shadow_task_id),
  UNIQUE KEY uk_execution_evidence_probe_shadow (probe_id, shadow_task_id),
  CONSTRAINT chk_execution_evidence_status CHECK (
    status IN ('pending', 'verified', 'revoked', 'expired')
  ) ENFORCED,
  CONSTRAINT chk_execution_evidence_pair CHECK (
    (probe_id IS NULL AND shadow_task_id IS NULL)
    OR (probe_id IS NOT NULL AND shadow_task_id IS NOT NULL)
  ) ENFORCED,
  CONSTRAINT chk_execution_evidence_verified CHECK (
    status <> 'verified'
    OR (
      verified_at IS NOT NULL
      AND probe_id IS NOT NULL
      AND shadow_task_id IS NOT NULL
      AND expires_at > verified_at
    )
  ) ENFORCED,
  CONSTRAINT chk_execution_evidence_expiry CHECK (expires_at > created_at) ENFORCED,
  CONSTRAINT chk_execution_evidence_observation_hashes CHECK (
    status <> 'verified'
    OR (
      probe_observation_hash IS NOT NULL
      AND shadow_observation_hash IS NOT NULL
      AND probe_observation_kind IS NOT NULL
      AND shadow_observation_kind IS NOT NULL
      AND REGEXP_LIKE(probe_observation_hash, '^[0-9a-f]{64}$', 'c')
      AND REGEXP_LIKE(shadow_observation_hash, '^[0-9a-f]{64}$', 'c')
      AND CHAR_LENGTH(TRIM(probe_observation_kind)) > 0
      AND CHAR_LENGTH(TRIM(shadow_observation_kind)) > 0
    )
  ) ENFORCED,
  CONSTRAINT fk_execution_evidence_lottery
    FOREIGN KEY (lottery_id) REFERENCES lotteries(id),
  CONSTRAINT fk_execution_evidence_account
    FOREIGN KEY (account_id) REFERENCES accounts(id),
  CONSTRAINT fk_execution_evidence_rule_snapshot
    FOREIGN KEY (rule_snapshot_id, lottery_id)
    REFERENCES lottery_rule_snapshots(id, lottery_id),
  CONSTRAINT fk_execution_evidence_probe
    FOREIGN KEY (
      probe_id,
      lottery_id,
      account_id,
      platform,
      rule_snapshot_id,
      execution_path_id,
      target_hash,
      rule_hash,
      action_plan_hash,
      config_hash,
      probe_observation_kind,
      probe_observation_hash
    )
    REFERENCES adapter_calibrations (
      probe_id,
      lottery_id,
      account_id,
      platform,
      rule_snapshot_id,
      execution_path_id,
      target_hash,
      rule_hash,
      action_plan_hash,
      config_hash,
      observation_kind,
      observation_hash
    ),
  CONSTRAINT fk_execution_evidence_shadow
    FOREIGN KEY (
      shadow_task_id,
      lottery_id,
      account_id,
      rule_snapshot_id,
      execution_path_id,
      target_hash,
      rule_hash,
      action_plan_hash,
      config_hash,
      shadow_observation_kind,
      shadow_observation_hash
    )
    REFERENCES task_runs (
      task_id,
      lottery_id,
      account_id,
      rule_snapshot_id,
      execution_path_id,
      target_hash,
      rule_hash,
      action_plan_hash,
      config_hash,
      preflight_observation_kind,
      preflight_observation_hash
    )
) ENGINE=InnoDB;

SET @dpms_sql = IF(
  (SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS
   WHERE CONSTRAINT_SCHEMA = DATABASE()
     AND TABLE_NAME = 'task_runs'
     AND CONSTRAINT_NAME = 'fk_task_run_execution_evidence') > 0,
  'SELECT 1',
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

CREATE TABLE IF NOT EXISTS external_action_intents (
  intent_id CHAR(36) PRIMARY KEY,
  task_id CHAR(36) NOT NULL,
  account_id BIGINT NOT NULL,
  lottery_id BIGINT NOT NULL,
  lease_id CHAR(36) NOT NULL,
  lease_generation BIGINT UNSIGNED NOT NULL,
  action VARCHAR(32) NOT NULL,
  payload_hash CHAR(64) NOT NULL,
  status VARCHAR(32) NOT NULL DEFAULT 'pending',
  effect_certainty VARCHAR(32) NOT NULL DEFAULT 'not_started',
  attempt_no INT UNSIGNED NOT NULL DEFAULT 0,
  started_at TIMESTAMP NULL,
  completed_at TIMESTAMP NULL,
  outcome VARCHAR(32) NULL,
  remote_ref VARCHAR(512) NULL,
  error_message TEXT NULL,
  reconciliation_note TEXT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_external_action_task_action (task_id, action),
  INDEX idx_external_action_reconciliation (status, updated_at),
  INDEX idx_external_action_account_created (account_id, created_at),
  INDEX idx_external_action_lease (lease_id, account_id, lease_generation),
  CONSTRAINT chk_external_action_status CHECK (
    status IN ('pending', 'prepared', 'started', 'succeeded', 'failed', 'unknown')
  ) ENFORCED,
  CONSTRAINT chk_external_action_effect_certainty CHECK (
    (status IN ('pending', 'prepared') AND effect_certainty = 'not_started')
    OR (status IN ('started', 'unknown') AND effect_certainty = 'unknown')
    OR (status = 'succeeded' AND effect_certainty = 'confirmed_effect')
    OR (status = 'failed' AND effect_certainty = 'confirmed_no_effect')
  ) ENFORCED,
  CONSTRAINT chk_external_action_lifecycle CHECK (
    (
      status = 'pending'
      AND attempt_no = 0
      AND started_at IS NULL
      AND completed_at IS NULL
      AND outcome IS NULL
    )
    OR (
      status = 'prepared'
      AND attempt_no > 0
      AND started_at IS NULL
      AND completed_at IS NULL
      AND outcome IS NULL
    )
    OR (
      status = 'started'
      AND attempt_no > 0
      AND started_at IS NOT NULL
      AND completed_at IS NULL
      AND outcome IS NULL
    )
    OR (
      status = 'succeeded'
      AND attempt_no > 0
      AND started_at IS NOT NULL
      AND completed_at IS NOT NULL
      AND outcome IS NOT NULL
      AND outcome = 'ok'
    )
    OR (
      status = 'failed'
      AND attempt_no > 0
      AND started_at IS NOT NULL
      AND completed_at IS NOT NULL
      AND outcome IS NOT NULL
      AND outcome IN ('retry', 'limit', 'skip', 'captcha', 'risk', 'auth')
    )
    OR (
      status = 'unknown'
      AND attempt_no > 0
      AND started_at IS NOT NULL
      AND completed_at IS NOT NULL
      AND outcome IS NOT NULL
      AND outcome = 'unknown'
      AND reconciliation_note IS NOT NULL
      AND CHAR_LENGTH(TRIM(reconciliation_note)) > 0
    )
  ) ENFORCED,
  CONSTRAINT chk_external_action_lease_generation CHECK (lease_generation > 0) ENFORCED,
  CONSTRAINT fk_external_action_task_binding
    FOREIGN KEY (task_id, lottery_id, account_id)
    REFERENCES task_runs(task_id, lottery_id, account_id),
  CONSTRAINT fk_external_action_account
    FOREIGN KEY (account_id) REFERENCES accounts(id),
  CONSTRAINT fk_external_action_lottery
    FOREIGN KEY (lottery_id) REFERENCES lotteries(id),
  CONSTRAINT fk_external_action_lease_binding
    FOREIGN KEY (lease_id, account_id, lease_generation)
    REFERENCES account_operation_leases(lease_id, account_id, generation)
) ENGINE=InnoDB;
