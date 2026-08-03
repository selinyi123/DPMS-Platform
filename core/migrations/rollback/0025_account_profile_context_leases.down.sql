-- Manual, fail-closed rollback for 0025.
--
-- Quiesce every Worker first. An unexpired row means a process may still be
-- using the corresponding profile and the ownership fence must not be removed.

DROP TEMPORARY TABLE IF EXISTS dpms_0025_rollback_guard;

CREATE TEMPORARY TABLE dpms_0025_rollback_guard (
  active_profile_context_leases BIGINT UNSIGNED NOT NULL,
  CONSTRAINT chk_0025_no_active_profile_context_lease
    CHECK (active_profile_context_leases = 0) ENFORCED
);

SET @dpms_0025_lease_table_exists = (
  SELECT COUNT(*)
  FROM information_schema.TABLES
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'account_profile_context_leases'
);

SET @dpms_sql = IF(
  @dpms_0025_lease_table_exists > 0,
  'INSERT INTO dpms_0025_rollback_guard (active_profile_context_leases)
   SELECT COUNT(*)
   FROM account_profile_context_leases
   WHERE lease_expires_at > NOW(6)',
  'INSERT INTO dpms_0025_rollback_guard
     (active_profile_context_leases)
   VALUES (0)'
);

PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

DROP TEMPORARY TABLE dpms_0025_rollback_guard;

DROP TABLE IF EXISTS account_profile_context_leases;

DELETE FROM schema_migrations WHERE version = '0025';
