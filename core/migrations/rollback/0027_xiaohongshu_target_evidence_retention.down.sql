-- Manual rollback for 0027. This restores the 0026 relationship exactly.
-- It does not delete rows, but it weakens source-hit retention semantics.

SET @dpms_0027_candidate_fk_exists = (
  SELECT COUNT(*)
  FROM information_schema.TABLE_CONSTRAINTS
  WHERE CONSTRAINT_SCHEMA = DATABASE()
    AND TABLE_NAME = 'xiaohongshu_target_candidate_source_hits'
    AND CONSTRAINT_NAME = 'fk_xhs_target_hit_candidate'
    AND CONSTRAINT_TYPE = 'FOREIGN KEY'
);

SET @dpms_sql = IF(
  @dpms_0027_candidate_fk_exists > 0,
  'ALTER TABLE xiaohongshu_target_candidate_source_hits
     DROP FOREIGN KEY fk_xhs_target_hit_candidate',
  'SELECT 1'
);

PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

ALTER TABLE xiaohongshu_target_candidate_source_hits
  ADD CONSTRAINT fk_xhs_target_hit_candidate
  FOREIGN KEY (candidate_id)
  REFERENCES xiaohongshu_target_candidates(id)
  ON DELETE CASCADE;

DELETE FROM schema_migrations WHERE version = '0027';
