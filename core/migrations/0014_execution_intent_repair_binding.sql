-- Freeze the complete reviewed real-run intent and bind every task to its
-- exact requested action subset.  There is deliberately no legacy backfill:
-- historical tasks cannot be reconstructed safely from mutable
-- lotteries.action_plan and therefore remain repair-ineligible.

-- Existing installations normally receive this key from init.sql.  Repair its
-- complete signature here as well so a same-named drifted index cannot let the
-- typed OAuth FK be created before the replacement contract is trustworthy.
SET @dpms_account_calibration_id_index_signature = (
  SELECT GROUP_CONCAT(
           CONCAT(
             SEQ_IN_INDEX, ':', COLUMN_NAME, ':', NON_UNIQUE, ':',
             COALESCE(CAST(SUB_PART AS CHAR), 'FULL')
           )
           ORDER BY SEQ_IN_INDEX
           SEPARATOR ','
         )
  FROM information_schema.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'account_calibrations'
    AND INDEX_NAME = 'uk_account_calibration_id'
);

SET @dpms_sql = CASE
  WHEN @dpms_account_calibration_id_index_signature =
       '1:calibration_id:0:FULL'
    THEN 'SELECT 1'
  WHEN @dpms_account_calibration_id_index_signature IS NULL
    THEN 'ALTER TABLE account_calibrations
            ADD UNIQUE KEY uk_account_calibration_id (calibration_id)'
  ELSE 'ALTER TABLE account_calibrations
          DROP INDEX uk_account_calibration_id,
          ADD UNIQUE KEY uk_account_calibration_id (calibration_id)'
END;
PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

CREATE TABLE IF NOT EXISTS lottery_execution_intents (
  contract_version TINYINT UNSIGNED NOT NULL,
  intent_id CHAR(36) PRIMARY KEY,
  intent_hash CHAR(64) NOT NULL,
  lottery_id BIGINT NOT NULL,
  source_task_id CHAR(36) NOT NULL,
  source_account_id BIGINT NOT NULL,
  platform VARCHAR(32) NOT NULL,
  raw_url VARCHAR(512) NOT NULL,
  canonical_url VARCHAR(512) NOT NULL,
  full_action_plan JSON NOT NULL,
  full_action_plan_hash CHAR(64) NOT NULL,
  full_required_actions JSON NOT NULL,
  full_required_actions_hash CHAR(64) NOT NULL,
  rule_snapshot_id BIGINT NOT NULL,
  rule_hash CHAR(64) NOT NULL,
  execution_path_id VARCHAR(128) NOT NULL,
  target_hash CHAR(64) NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uk_lottery_execution_intent_lottery (lottery_id),
  UNIQUE KEY uk_lottery_execution_intent_identity (intent_id, lottery_id),
  UNIQUE KEY uk_lottery_execution_intent_source_binding (
    source_task_id,
    lottery_id,
    source_account_id
  ),
  INDEX idx_lottery_execution_intent_rule (
    lottery_id,
    rule_snapshot_id,
    full_action_plan_hash
  ),
  CONSTRAINT chk_lottery_execution_intent_contract
    CHECK (contract_version = 1) ENFORCED,
  CONSTRAINT chk_lottery_execution_intent_actions
    CHECK (
      JSON_TYPE(full_required_actions) = 'ARRAY'
      AND JSON_LENGTH(full_required_actions) > 0
    ) ENFORCED,
  CONSTRAINT chk_lottery_execution_intent_hashes
    CHECK (
      REGEXP_LIKE(intent_hash, '^[0-9a-f]{64}$', 'c')
      AND REGEXP_LIKE(full_action_plan_hash, '^[0-9a-f]{64}$', 'c')
      AND REGEXP_LIKE(full_required_actions_hash, '^[0-9a-f]{64}$', 'c')
      AND REGEXP_LIKE(rule_hash, '^[0-9a-f]{64}$', 'c')
      AND REGEXP_LIKE(target_hash, '^[0-9a-f]{64}$', 'c')
    ) ENFORCED,
  CONSTRAINT fk_lottery_execution_intent_lottery
    FOREIGN KEY (lottery_id) REFERENCES lotteries(id),
  CONSTRAINT fk_lottery_execution_intent_source_task
    FOREIGN KEY (source_task_id, lottery_id, source_account_id)
    REFERENCES task_runs(task_id, lottery_id, account_id),
  CONSTRAINT fk_lottery_execution_intent_rule_snapshot
    FOREIGN KEY (rule_snapshot_id, lottery_id)
    REFERENCES lottery_rule_snapshots(id, lottery_id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS task_execution_intent_bindings (
  contract_version TINYINT UNSIGNED NOT NULL,
  task_id CHAR(36) PRIMARY KEY,
  intent_id CHAR(36) NOT NULL,
  lottery_id BIGINT NOT NULL,
  account_id BIGINT NOT NULL,
  binding_kind VARCHAR(16) NOT NULL,
  requested_actions JSON NOT NULL,
  requested_actions_hash CHAR(64) NOT NULL,
  bound_action_plan JSON NOT NULL,
  bound_action_plan_hash CHAR(64) NOT NULL,
  evidence_action_plan_hash CHAR(64) NOT NULL,
  rule_snapshot_id BIGINT NOT NULL,
  rule_hash CHAR(64) NOT NULL,
  execution_evidence_id CHAR(36) NOT NULL,
  execution_evidence_kind VARCHAR(32) NOT NULL,
  exact_execution_evidence_id CHAR(36) NULL,
  oauth_calibration_id CHAR(36) NULL,
  execution_path_id VARCHAR(128) NOT NULL,
  target_hash CHAR(64) NOT NULL,
  config_hash CHAR(64) NOT NULL,
  execution_revision BIGINT UNSIGNED NOT NULL,
  account_lease_id CHAR(36) NOT NULL,
  account_lease_generation BIGINT UNSIGNED NOT NULL,
  binding_hash CHAR(64) NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uk_task_execution_intent_binding_identity (
    task_id,
    intent_id,
    lottery_id,
    account_id
  ),
  INDEX idx_task_execution_intent_requested (
    intent_id,
    binding_kind,
    created_at
  ),
  INDEX idx_task_execution_intent_evidence (
    execution_evidence_id,
    lottery_id,
    account_id
  ),
  INDEX idx_task_execution_intent_exact_evidence (
    exact_execution_evidence_id
  ),
  INDEX idx_task_execution_intent_oauth_calibration (
    oauth_calibration_id
  ),
  INDEX idx_task_execution_intent_lease (
    account_lease_id,
    account_id,
    account_lease_generation
  ),
  CONSTRAINT chk_task_execution_intent_contract
    CHECK (contract_version = 1) ENFORCED,
  CONSTRAINT chk_task_execution_intent_kind
    CHECK (binding_kind IN ('full', 'repair')) ENFORCED,
  CONSTRAINT chk_task_execution_intent_evidence_kind
    CHECK (
      (
        execution_evidence_kind = 'exact_execution_evidence'
        AND exact_execution_evidence_id IS NOT NULL
        AND exact_execution_evidence_id = execution_evidence_id
        AND oauth_calibration_id IS NULL
      )
      OR
      (
        execution_evidence_kind = 'oauth_account_calibration'
        AND oauth_calibration_id IS NOT NULL
        AND oauth_calibration_id = execution_evidence_id
        AND exact_execution_evidence_id IS NULL
      )
    ) ENFORCED,
  CONSTRAINT chk_task_execution_intent_actions
    CHECK (
      JSON_TYPE(requested_actions) = 'ARRAY'
      AND JSON_LENGTH(requested_actions) > 0
    ) ENFORCED,
  CONSTRAINT chk_task_execution_intent_hashes
    CHECK (
      REGEXP_LIKE(requested_actions_hash, '^[0-9a-f]{64}$', 'c')
      AND REGEXP_LIKE(bound_action_plan_hash, '^[0-9a-f]{64}$', 'c')
      AND REGEXP_LIKE(evidence_action_plan_hash, '^[0-9a-f]{64}$', 'c')
      AND REGEXP_LIKE(rule_hash, '^[0-9a-f]{64}$', 'c')
      AND REGEXP_LIKE(target_hash, '^[0-9a-f]{64}$', 'c')
      AND REGEXP_LIKE(config_hash, '^[0-9a-f]{64}$', 'c')
      AND REGEXP_LIKE(binding_hash, '^[0-9a-f]{64}$', 'c')
    ) ENFORCED,
  CONSTRAINT chk_task_execution_intent_revision
    CHECK (
      execution_revision > 0
      AND account_lease_generation > 0
    ) ENFORCED,
  CONSTRAINT fk_task_execution_intent_task
    FOREIGN KEY (task_id, lottery_id, account_id)
    REFERENCES task_runs(task_id, lottery_id, account_id),
  CONSTRAINT fk_task_execution_intent_root
    FOREIGN KEY (intent_id, lottery_id)
    REFERENCES lottery_execution_intents(intent_id, lottery_id),
  CONSTRAINT fk_task_execution_intent_exact_evidence
    FOREIGN KEY (exact_execution_evidence_id)
    REFERENCES execution_evidence_bindings(id),
  CONSTRAINT fk_task_execution_intent_oauth_calibration
    FOREIGN KEY (oauth_calibration_id)
    REFERENCES account_calibrations(calibration_id),
  CONSTRAINT fk_task_execution_intent_lease
    FOREIGN KEY (
      account_lease_id,
      account_id,
      account_lease_generation
    )
    REFERENCES account_operation_leases(lease_id, account_id, generation)
) ENGINE=InnoDB;

-- CREATE TABLE IF NOT EXISTS deliberately tolerates an already-present table,
-- but that behaviour is unsafe for this migration's replacement contract: a
-- drifted same-name table must never cause the legacy fail-closed FK to be
-- removed.  Re-check the complete contract before retiring it.  The temporary
-- table turns any false readiness bit into an immediate CHECK violation and
-- disappears automatically if the migration connection aborts.
SELECT COUNT(*) INTO @dpms_0014_ready_tables
  FROM information_schema.TABLES
 WHERE TABLE_SCHEMA = DATABASE()
   AND TABLE_TYPE = 'BASE TABLE'
   AND ENGINE = 'InnoDB'
   AND TABLE_NAME IN (
     'lottery_execution_intents',
     'task_execution_intent_bindings'
   );

DROP TEMPORARY TABLE IF EXISTS dpms_0014_expected_columns;
CREATE TEMPORARY TABLE dpms_0014_expected_columns (
  table_name VARCHAR(64) NOT NULL,
  column_name VARCHAR(64) NOT NULL,
  column_type VARCHAR(128) NOT NULL,
  is_nullable VARCHAR(3) NOT NULL,
  column_default VARCHAR(64) NULL,
  extra VARCHAR(255) NOT NULL,
  PRIMARY KEY (table_name, column_name)
);
INSERT INTO dpms_0014_expected_columns
  (
    table_name,
    column_name,
    column_type,
    is_nullable,
    column_default,
    extra
  )
VALUES
  (
    'lottery_execution_intents',
    'contract_version',
    'tinyint unsigned',
    'NO',
    NULL,
    ''
  ),
  (
    'lottery_execution_intents',
    'intent_id',
    'char(36)',
    'NO',
    NULL,
    ''
  ),
  (
    'lottery_execution_intents',
    'intent_hash',
    'char(64)',
    'NO',
    NULL,
    ''
  ),
  (
    'lottery_execution_intents',
    'lottery_id',
    'bigint',
    'NO',
    NULL,
    ''
  ),
  (
    'lottery_execution_intents',
    'source_task_id',
    'char(36)',
    'NO',
    NULL,
    ''
  ),
  (
    'lottery_execution_intents',
    'source_account_id',
    'bigint',
    'NO',
    NULL,
    ''
  ),
  (
    'lottery_execution_intents',
    'platform',
    'varchar(32)',
    'NO',
    NULL,
    ''
  ),
  (
    'lottery_execution_intents',
    'raw_url',
    'varchar(512)',
    'NO',
    NULL,
    ''
  ),
  (
    'lottery_execution_intents',
    'canonical_url',
    'varchar(512)',
    'NO',
    NULL,
    ''
  ),
  (
    'lottery_execution_intents',
    'full_action_plan',
    'json',
    'NO',
    NULL,
    ''
  ),
  (
    'lottery_execution_intents',
    'full_action_plan_hash',
    'char(64)',
    'NO',
    NULL,
    ''
  ),
  (
    'lottery_execution_intents',
    'full_required_actions',
    'json',
    'NO',
    NULL,
    ''
  ),
  (
    'lottery_execution_intents',
    'full_required_actions_hash',
    'char(64)',
    'NO',
    NULL,
    ''
  ),
  (
    'lottery_execution_intents',
    'rule_snapshot_id',
    'bigint',
    'NO',
    NULL,
    ''
  ),
  (
    'lottery_execution_intents',
    'rule_hash',
    'char(64)',
    'NO',
    NULL,
    ''
  ),
  (
    'lottery_execution_intents',
    'execution_path_id',
    'varchar(128)',
    'NO',
    NULL,
    ''
  ),
  (
    'lottery_execution_intents',
    'target_hash',
    'char(64)',
    'NO',
    NULL,
    ''
  ),
  (
    'lottery_execution_intents',
    'created_at',
    'timestamp',
    'NO',
    'CURRENT_TIMESTAMP',
    'DEFAULT_GENERATED'
  ),
  (
    'task_execution_intent_bindings',
    'contract_version',
    'tinyint unsigned',
    'NO',
    NULL,
    ''
  ),
  (
    'task_execution_intent_bindings',
    'task_id',
    'char(36)',
    'NO',
    NULL,
    ''
  ),
  (
    'task_execution_intent_bindings',
    'intent_id',
    'char(36)',
    'NO',
    NULL,
    ''
  ),
  (
    'task_execution_intent_bindings',
    'lottery_id',
    'bigint',
    'NO',
    NULL,
    ''
  ),
  (
    'task_execution_intent_bindings',
    'account_id',
    'bigint',
    'NO',
    NULL,
    ''
  ),
  (
    'task_execution_intent_bindings',
    'binding_kind',
    'varchar(16)',
    'NO',
    NULL,
    ''
  ),
  (
    'task_execution_intent_bindings',
    'requested_actions',
    'json',
    'NO',
    NULL,
    ''
  ),
  (
    'task_execution_intent_bindings',
    'requested_actions_hash',
    'char(64)',
    'NO',
    NULL,
    ''
  ),
  (
    'task_execution_intent_bindings',
    'bound_action_plan',
    'json',
    'NO',
    NULL,
    ''
  ),
  (
    'task_execution_intent_bindings',
    'bound_action_plan_hash',
    'char(64)',
    'NO',
    NULL,
    ''
  ),
  (
    'task_execution_intent_bindings',
    'evidence_action_plan_hash',
    'char(64)',
    'NO',
    NULL,
    ''
  ),
  (
    'task_execution_intent_bindings',
    'rule_snapshot_id',
    'bigint',
    'NO',
    NULL,
    ''
  ),
  (
    'task_execution_intent_bindings',
    'rule_hash',
    'char(64)',
    'NO',
    NULL,
    ''
  ),
  (
    'task_execution_intent_bindings',
    'execution_evidence_id',
    'char(36)',
    'NO',
    NULL,
    ''
  ),
  (
    'task_execution_intent_bindings',
    'execution_evidence_kind',
    'varchar(32)',
    'NO',
    NULL,
    ''
  ),
  (
    'task_execution_intent_bindings',
    'exact_execution_evidence_id',
    'char(36)',
    'YES',
    NULL,
    ''
  ),
  (
    'task_execution_intent_bindings',
    'oauth_calibration_id',
    'char(36)',
    'YES',
    NULL,
    ''
  ),
  (
    'task_execution_intent_bindings',
    'execution_path_id',
    'varchar(128)',
    'NO',
    NULL,
    ''
  ),
  (
    'task_execution_intent_bindings',
    'target_hash',
    'char(64)',
    'NO',
    NULL,
    ''
  ),
  (
    'task_execution_intent_bindings',
    'config_hash',
    'char(64)',
    'NO',
    NULL,
    ''
  ),
  (
    'task_execution_intent_bindings',
    'execution_revision',
    'bigint unsigned',
    'NO',
    NULL,
    ''
  ),
  (
    'task_execution_intent_bindings',
    'account_lease_id',
    'char(36)',
    'NO',
    NULL,
    ''
  ),
  (
    'task_execution_intent_bindings',
    'account_lease_generation',
    'bigint unsigned',
    'NO',
    NULL,
    ''
  ),
  (
    'task_execution_intent_bindings',
    'binding_hash',
    'char(64)',
    'NO',
    NULL,
    ''
  ),
  (
    'task_execution_intent_bindings',
    'created_at',
    'timestamp',
    'NO',
    'CURRENT_TIMESTAMP',
    'DEFAULT_GENERATED'
  );

SELECT COUNT(*) INTO @dpms_0014_ready_columns
  FROM dpms_0014_expected_columns AS expected
  JOIN information_schema.COLUMNS AS actual
    ON actual.TABLE_SCHEMA = DATABASE()
   AND actual.TABLE_NAME = expected.table_name
   AND actual.COLUMN_NAME = expected.column_name
 WHERE LOWER(actual.COLUMN_TYPE) = expected.column_type
   AND actual.IS_NULLABLE = expected.is_nullable
   AND actual.COLUMN_DEFAULT <=> expected.column_default
   AND UPPER(actual.EXTRA) = expected.extra;

DROP TEMPORARY TABLE dpms_0014_expected_columns;

SELECT COUNT(*) INTO @dpms_0014_ready_unique_indexes
  FROM (
    SELECT
      TABLE_NAME,
      INDEX_NAME,
      MAX(NON_UNIQUE) AS non_unique,
      SUM(SUB_PART IS NOT NULL) AS prefix_columns,
      SUM(EXPRESSION IS NOT NULL) AS expression_columns,
      GROUP_CONCAT(
        COLUMN_NAME
        ORDER BY SEQ_IN_INDEX
        SEPARATOR ','
      ) AS indexed_columns
      FROM information_schema.STATISTICS
     WHERE TABLE_SCHEMA = DATABASE()
       AND (
         (
           TABLE_NAME = 'lottery_execution_intents'
           AND INDEX_NAME IN (
             'PRIMARY',
             'uk_lottery_execution_intent_lottery',
             'uk_lottery_execution_intent_identity',
             'uk_lottery_execution_intent_source_binding'
           )
         )
         OR
         (
           TABLE_NAME = 'task_execution_intent_bindings'
           AND INDEX_NAME IN (
             'PRIMARY',
             'uk_task_execution_intent_binding_identity'
           )
         )
         OR
         (
           TABLE_NAME = 'account_calibrations'
           AND INDEX_NAME = 'uk_account_calibration_id'
         )
       )
     GROUP BY TABLE_NAME, INDEX_NAME
  ) AS candidate_index
 WHERE candidate_index.non_unique = 0
   AND candidate_index.prefix_columns = 0
   AND candidate_index.expression_columns = 0
   AND (
     (
       candidate_index.TABLE_NAME = 'lottery_execution_intents'
       AND candidate_index.INDEX_NAME = 'PRIMARY'
       AND candidate_index.indexed_columns = 'intent_id'
     )
     OR
     (
       candidate_index.TABLE_NAME = 'lottery_execution_intents'
       AND candidate_index.INDEX_NAME =
         'uk_lottery_execution_intent_lottery'
       AND candidate_index.indexed_columns = 'lottery_id'
     )
     OR
     (
       candidate_index.TABLE_NAME = 'lottery_execution_intents'
       AND candidate_index.INDEX_NAME =
         'uk_lottery_execution_intent_identity'
       AND candidate_index.indexed_columns = 'intent_id,lottery_id'
     )
     OR
     (
       candidate_index.TABLE_NAME = 'lottery_execution_intents'
       AND candidate_index.INDEX_NAME =
         'uk_lottery_execution_intent_source_binding'
       AND candidate_index.indexed_columns =
         'source_task_id,lottery_id,source_account_id'
     )
     OR
     (
       candidate_index.TABLE_NAME = 'task_execution_intent_bindings'
       AND candidate_index.INDEX_NAME = 'PRIMARY'
       AND candidate_index.indexed_columns = 'task_id'
     )
     OR
     (
       candidate_index.TABLE_NAME = 'task_execution_intent_bindings'
       AND candidate_index.INDEX_NAME =
         'uk_task_execution_intent_binding_identity'
       AND candidate_index.indexed_columns =
         'task_id,intent_id,lottery_id,account_id'
     )
     OR
     (
       candidate_index.TABLE_NAME = 'account_calibrations'
       AND candidate_index.INDEX_NAME = 'uk_account_calibration_id'
       AND candidate_index.indexed_columns = 'calibration_id'
     )
   );

SELECT COUNT(*) INTO @dpms_0014_ready_foreign_keys
  FROM (
    SELECT
      key_column.TABLE_NAME,
      key_column.CONSTRAINT_NAME,
      MAX(key_column.REFERENCED_TABLE_NAME) AS referenced_table,
      GROUP_CONCAT(
        key_column.COLUMN_NAME
        ORDER BY key_column.ORDINAL_POSITION
        SEPARATOR ','
      ) AS local_columns,
      GROUP_CONCAT(
        key_column.REFERENCED_COLUMN_NAME
        ORDER BY key_column.ORDINAL_POSITION
        SEPARATOR ','
      ) AS referenced_columns,
      MAX(reference.UPDATE_RULE) AS update_rule,
      MAX(reference.DELETE_RULE) AS delete_rule,
      MAX(reference.MATCH_OPTION) AS match_option
      FROM information_schema.KEY_COLUMN_USAGE AS key_column
      JOIN information_schema.REFERENTIAL_CONSTRAINTS AS reference
        ON reference.CONSTRAINT_SCHEMA = key_column.CONSTRAINT_SCHEMA
       AND reference.TABLE_NAME = key_column.TABLE_NAME
       AND reference.CONSTRAINT_NAME = key_column.CONSTRAINT_NAME
     WHERE key_column.CONSTRAINT_SCHEMA = DATABASE()
       AND (
         (
           key_column.TABLE_NAME = 'lottery_execution_intents'
           AND key_column.CONSTRAINT_NAME IN (
             'fk_lottery_execution_intent_lottery',
             'fk_lottery_execution_intent_source_task',
             'fk_lottery_execution_intent_rule_snapshot'
           )
         )
         OR
         (
           key_column.TABLE_NAME = 'task_execution_intent_bindings'
           AND key_column.CONSTRAINT_NAME IN (
             'fk_task_execution_intent_task',
             'fk_task_execution_intent_root',
             'fk_task_execution_intent_exact_evidence',
             'fk_task_execution_intent_oauth_calibration',
             'fk_task_execution_intent_lease'
           )
         )
       )
     GROUP BY key_column.TABLE_NAME, key_column.CONSTRAINT_NAME
  ) AS candidate_fk
 WHERE candidate_fk.update_rule IN ('RESTRICT', 'NO ACTION')
   AND candidate_fk.delete_rule IN ('RESTRICT', 'NO ACTION')
   AND candidate_fk.match_option = 'NONE'
   AND (
     (
       candidate_fk.TABLE_NAME = 'lottery_execution_intents'
       AND candidate_fk.CONSTRAINT_NAME =
         'fk_lottery_execution_intent_lottery'
       AND candidate_fk.local_columns = 'lottery_id'
       AND candidate_fk.referenced_table = 'lotteries'
       AND candidate_fk.referenced_columns = 'id'
     )
     OR
     (
       candidate_fk.TABLE_NAME = 'lottery_execution_intents'
       AND candidate_fk.CONSTRAINT_NAME =
         'fk_lottery_execution_intent_source_task'
       AND candidate_fk.local_columns =
         'source_task_id,lottery_id,source_account_id'
       AND candidate_fk.referenced_table = 'task_runs'
       AND candidate_fk.referenced_columns =
         'task_id,lottery_id,account_id'
     )
     OR
     (
       candidate_fk.TABLE_NAME = 'lottery_execution_intents'
       AND candidate_fk.CONSTRAINT_NAME =
         'fk_lottery_execution_intent_rule_snapshot'
       AND candidate_fk.local_columns = 'rule_snapshot_id,lottery_id'
       AND candidate_fk.referenced_table = 'lottery_rule_snapshots'
       AND candidate_fk.referenced_columns = 'id,lottery_id'
     )
     OR
     (
       candidate_fk.TABLE_NAME = 'task_execution_intent_bindings'
       AND candidate_fk.CONSTRAINT_NAME =
         'fk_task_execution_intent_task'
       AND candidate_fk.local_columns = 'task_id,lottery_id,account_id'
       AND candidate_fk.referenced_table = 'task_runs'
       AND candidate_fk.referenced_columns =
         'task_id,lottery_id,account_id'
     )
     OR
     (
       candidate_fk.TABLE_NAME = 'task_execution_intent_bindings'
       AND candidate_fk.CONSTRAINT_NAME =
         'fk_task_execution_intent_root'
       AND candidate_fk.local_columns = 'intent_id,lottery_id'
       AND candidate_fk.referenced_table = 'lottery_execution_intents'
       AND candidate_fk.referenced_columns = 'intent_id,lottery_id'
     )
     OR
     (
       candidate_fk.TABLE_NAME = 'task_execution_intent_bindings'
       AND candidate_fk.CONSTRAINT_NAME =
         'fk_task_execution_intent_exact_evidence'
       AND candidate_fk.local_columns = 'exact_execution_evidence_id'
       AND candidate_fk.referenced_table = 'execution_evidence_bindings'
       AND candidate_fk.referenced_columns = 'id'
     )
     OR
     (
       candidate_fk.TABLE_NAME = 'task_execution_intent_bindings'
       AND candidate_fk.CONSTRAINT_NAME =
         'fk_task_execution_intent_oauth_calibration'
       AND candidate_fk.local_columns = 'oauth_calibration_id'
       AND candidate_fk.referenced_table = 'account_calibrations'
       AND candidate_fk.referenced_columns = 'calibration_id'
     )
     OR
     (
       candidate_fk.TABLE_NAME = 'task_execution_intent_bindings'
       AND candidate_fk.CONSTRAINT_NAME =
         'fk_task_execution_intent_lease'
       AND candidate_fk.local_columns =
         'account_lease_id,account_id,account_lease_generation'
       AND candidate_fk.referenced_table = 'account_operation_leases'
       AND candidate_fk.referenced_columns =
         'lease_id,account_id,generation'
     )
   );

SELECT COUNT(*) INTO @dpms_0014_ready_checks
  FROM (
    SELECT
      table_constraint.TABLE_NAME,
      table_constraint.CONSTRAINT_NAME,
      table_constraint.ENFORCED,
      LOWER(
        REGEXP_REPLACE(
          REPLACE(
            REPLACE(
              REPLACE(
                REPLACE(
                  REPLACE(
                    REPLACE(
                      REPLACE(
                        check_constraint.CHECK_CLAUSE,
                        CHAR(96),
                        ''
                      ),
                      CHAR(92),
                      ''
                    ),
                    '_latin1',
                    ''
                  ),
                  '_utf8mb4',
                  ''
                ),
                '_utf8mb3',
                ''
              ),
              '_ascii',
              ''
            ),
            '_binary',
            ''
          ),
          '[[:space:]]+',
          ''
        )
      ) AS normalized_clause
      FROM information_schema.TABLE_CONSTRAINTS AS table_constraint
      JOIN information_schema.CHECK_CONSTRAINTS AS check_constraint
        ON check_constraint.CONSTRAINT_SCHEMA =
             table_constraint.CONSTRAINT_SCHEMA
       AND check_constraint.CONSTRAINT_NAME =
             table_constraint.CONSTRAINT_NAME
     WHERE table_constraint.CONSTRAINT_SCHEMA = DATABASE()
       AND table_constraint.CONSTRAINT_TYPE = 'CHECK'
       AND table_constraint.TABLE_NAME IN (
         'lottery_execution_intents',
         'task_execution_intent_bindings'
       )
  ) AS candidate_check
 WHERE candidate_check.ENFORCED = 'YES'
   AND (
     (
       candidate_check.TABLE_NAME = 'lottery_execution_intents'
       AND candidate_check.CONSTRAINT_NAME =
         'chk_lottery_execution_intent_contract'
       AND candidate_check.normalized_clause = '(contract_version=1)'
     )
     OR
     (
       candidate_check.TABLE_NAME = 'lottery_execution_intents'
       AND candidate_check.CONSTRAINT_NAME =
         'chk_lottery_execution_intent_actions'
       AND candidate_check.normalized_clause =
         '((json_type(full_required_actions)=''array'')and(json_length(full_required_actions)>0))'
     )
     OR
     (
       candidate_check.TABLE_NAME = 'lottery_execution_intents'
       AND candidate_check.CONSTRAINT_NAME =
         'chk_lottery_execution_intent_hashes'
       AND candidate_check.normalized_clause =
         '(regexp_like(intent_hash,''^[0-9a-f]{64}$'',''c'')andregexp_like(full_action_plan_hash,''^[0-9a-f]{64}$'',''c'')andregexp_like(full_required_actions_hash,''^[0-9a-f]{64}$'',''c'')andregexp_like(rule_hash,''^[0-9a-f]{64}$'',''c'')andregexp_like(target_hash,''^[0-9a-f]{64}$'',''c''))'
     )
     OR
     (
       candidate_check.TABLE_NAME = 'task_execution_intent_bindings'
       AND candidate_check.CONSTRAINT_NAME =
         'chk_task_execution_intent_contract'
       AND candidate_check.normalized_clause = '(contract_version=1)'
     )
     OR
     (
       candidate_check.TABLE_NAME = 'task_execution_intent_bindings'
       AND candidate_check.CONSTRAINT_NAME =
         'chk_task_execution_intent_kind'
       AND candidate_check.normalized_clause =
         '(binding_kindin(''full'',''repair''))'
     )
     OR
     (
       candidate_check.TABLE_NAME = 'task_execution_intent_bindings'
       AND candidate_check.CONSTRAINT_NAME =
         'chk_task_execution_intent_evidence_kind'
       AND candidate_check.normalized_clause =
         '(((execution_evidence_kind=''exact_execution_evidence'')and(exact_execution_evidence_idisnotnull)and(exact_execution_evidence_id=execution_evidence_id)and(oauth_calibration_idisnull))or((execution_evidence_kind=''oauth_account_calibration'')and(oauth_calibration_idisnotnull)and(oauth_calibration_id=execution_evidence_id)and(exact_execution_evidence_idisnull)))'
     )
     OR
     (
       candidate_check.TABLE_NAME = 'task_execution_intent_bindings'
       AND candidate_check.CONSTRAINT_NAME =
         'chk_task_execution_intent_actions'
       AND candidate_check.normalized_clause =
         '((json_type(requested_actions)=''array'')and(json_length(requested_actions)>0))'
     )
     OR
     (
       candidate_check.TABLE_NAME = 'task_execution_intent_bindings'
       AND candidate_check.CONSTRAINT_NAME =
         'chk_task_execution_intent_hashes'
       AND candidate_check.normalized_clause =
         '(regexp_like(requested_actions_hash,''^[0-9a-f]{64}$'',''c'')andregexp_like(bound_action_plan_hash,''^[0-9a-f]{64}$'',''c'')andregexp_like(evidence_action_plan_hash,''^[0-9a-f]{64}$'',''c'')andregexp_like(rule_hash,''^[0-9a-f]{64}$'',''c'')andregexp_like(target_hash,''^[0-9a-f]{64}$'',''c'')andregexp_like(config_hash,''^[0-9a-f]{64}$'',''c'')andregexp_like(binding_hash,''^[0-9a-f]{64}$'',''c''))'
     )
     OR
     (
       candidate_check.TABLE_NAME = 'task_execution_intent_bindings'
       AND candidate_check.CONSTRAINT_NAME =
         'chk_task_execution_intent_revision'
       AND candidate_check.normalized_clause =
         '((execution_revision>0)and(account_lease_generation>0))'
     )
   );

DROP TEMPORARY TABLE IF EXISTS dpms_0014_replacement_contract_guard;
CREATE TEMPORARY TABLE dpms_0014_replacement_contract_guard (
  tables_ready TINYINT NOT NULL,
  columns_ready TINYINT NOT NULL,
  unique_indexes_ready TINYINT NOT NULL,
  foreign_keys_ready TINYINT NOT NULL,
  checks_ready TINYINT NOT NULL,
  CONSTRAINT chk_0014_replacement_tables_ready
    CHECK (tables_ready = 1) ENFORCED,
  CONSTRAINT chk_0014_replacement_columns_ready
    CHECK (columns_ready = 1) ENFORCED,
  CONSTRAINT chk_0014_replacement_unique_indexes_ready
    CHECK (unique_indexes_ready = 1) ENFORCED,
  CONSTRAINT chk_0014_replacement_foreign_keys_ready
    CHECK (foreign_keys_ready = 1) ENFORCED,
  CONSTRAINT chk_0014_replacement_checks_ready
    CHECK (checks_ready = 1) ENFORCED
);
INSERT INTO dpms_0014_replacement_contract_guard
  (
    tables_ready,
    columns_ready,
    unique_indexes_ready,
    foreign_keys_ready,
    checks_ready
  )
VALUES
  (
    @dpms_0014_ready_tables = 2,
    @dpms_0014_ready_columns = 43,
    @dpms_0014_ready_unique_indexes = 7,
    @dpms_0014_ready_foreign_keys = 8,
    @dpms_0014_ready_checks = 9
  );
DROP TEMPORARY TABLE dpms_0014_replacement_contract_guard;

-- task_runs.execution_evidence_id is a generic evidence identifier.  Weibo
-- binds it to account_calibrations while Bilibili binds it to the exact
-- execution_evidence_bindings table, so the old Bilibili-only composite FK
-- must not remain on the shared task table.  Retire it only after both typed
-- binding tables and all of their constraints exist.  MySQL DDL implicitly
-- commits, so this ordering keeps a failed/retried migration on the original
-- fail-closed constraint until the replacement contract is complete.
SET @dpms_sql = IF(
  (SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS
   WHERE CONSTRAINT_SCHEMA = DATABASE()
     AND TABLE_NAME = 'task_runs'
     AND CONSTRAINT_NAME = 'fk_task_run_execution_evidence'
     AND CONSTRAINT_TYPE = 'FOREIGN KEY') > 0,
  'ALTER TABLE task_runs
     DROP FOREIGN KEY fk_task_run_execution_evidence',
  'SELECT 1'
);
PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;
