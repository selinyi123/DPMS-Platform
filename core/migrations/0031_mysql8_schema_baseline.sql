-- Installation baseline after the frozen 0011 upgrade and the additive
-- 0030 index convergence.  Do not edit 0011 or its checksum: new installers
-- use this marker to distinguish the known MySQL 8 warning path from a
-- genuinely incomplete migration.

CREATE TABLE IF NOT EXISTS dpms_schema_baselines (
  baseline_key VARCHAR(96) NOT NULL,
  migration_version VARCHAR(16) NOT NULL,
  mysql_major VARCHAR(16) NOT NULL,
  contract_revision VARCHAR(64) NOT NULL,
  applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (baseline_key)
) ENGINE=InnoDB;

-- Avoid the deprecated duplicate-key value-reference form on MySQL 8.0.20+; the
-- migration runner serialises this update/insert pair with its schema lock.
UPDATE dpms_schema_baselines
   SET migration_version = '0031',
       mysql_major = '8',
       contract_revision = '0011-plus-0030-index-convergence-v1'
 WHERE baseline_key = 'mysql8-v1';

INSERT IGNORE INTO dpms_schema_baselines
  (baseline_key, migration_version, mysql_major, contract_revision)
VALUES
  ('mysql8-v1', '0031', '8', '0011-plus-0030-index-convergence-v1');
