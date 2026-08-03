-- Manual, fail-closed rollback for 0026.
--
-- Quiesce Core and the Xiaohongshu pursuit worker first.  These tables are the
-- authoritative review projection and provenance history; rollback refuses to
-- discard even skipped candidates or inactive source definitions.

DROP TEMPORARY TABLE IF EXISTS dpms_0026_rollback_guard;

CREATE TEMPORARY TABLE dpms_0026_rollback_guard (
  persisted_rows BIGINT UNSIGNED NOT NULL,
  CONSTRAINT chk_0026_no_persisted_target_pursuit_state
    CHECK (persisted_rows = 0) ENFORCED
);

SET @dpms_0026_table_exists = (
  SELECT COUNT(*)
  FROM information_schema.TABLES
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'xiaohongshu_target_candidate_source_hits'
);

SET @dpms_sql = IF(
  @dpms_0026_table_exists > 0,
  'INSERT INTO dpms_0026_rollback_guard (persisted_rows)
   SELECT COUNT(*) FROM xiaohongshu_target_candidate_source_hits',
  'INSERT INTO dpms_0026_rollback_guard (persisted_rows) VALUES (0)'
);

PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

SET @dpms_0026_table_exists = (
  SELECT COUNT(*)
  FROM information_schema.TABLES
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'xiaohongshu_target_candidates'
);

SET @dpms_sql = IF(
  @dpms_0026_table_exists > 0,
  'INSERT INTO dpms_0026_rollback_guard (persisted_rows)
   SELECT COUNT(*) FROM xiaohongshu_target_candidates',
  'INSERT INTO dpms_0026_rollback_guard (persisted_rows) VALUES (0)'
);

PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

SET @dpms_0026_table_exists = (
  SELECT COUNT(*)
  FROM information_schema.TABLES
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'xiaohongshu_target_sources'
);

SET @dpms_sql = IF(
  @dpms_0026_table_exists > 0,
  'INSERT INTO dpms_0026_rollback_guard (persisted_rows)
   SELECT COUNT(*) FROM xiaohongshu_target_sources',
  'INSERT INTO dpms_0026_rollback_guard (persisted_rows) VALUES (0)'
);

PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

DROP TEMPORARY TABLE dpms_0026_rollback_guard;

DROP TABLE IF EXISTS xiaohongshu_target_candidate_source_hits;

DROP TABLE IF EXISTS xiaohongshu_target_candidates;

DROP TABLE IF EXISTS xiaohongshu_target_sources;

DELETE FROM schema_migrations WHERE version = '0026';
