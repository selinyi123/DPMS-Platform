-- Compatibility repair for databases that previously recorded a different
-- 0002 migration before worker lease/dead-letter became part of the contract.
SET @dpms_sql = IF(
  (SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'task_runs' AND COLUMN_NAME = 'worker_id') = 0,
  'ALTER TABLE task_runs ADD COLUMN worker_id VARCHAR(128) NULL',
  'SELECT 1'
);
PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

SET @dpms_sql = IF(
  (SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'task_runs' AND COLUMN_NAME = 'stream_message_id') = 0,
  'ALTER TABLE task_runs ADD COLUMN stream_message_id VARCHAR(128) NULL',
  'SELECT 1'
);
PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

SET @dpms_sql = IF(
  (SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'task_runs' AND COLUMN_NAME = 'lease_expires_at') = 0,
  'ALTER TABLE task_runs ADD COLUMN lease_expires_at TIMESTAMP NULL',
  'SELECT 1'
);
PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

SET @dpms_sql = IF(
  (SELECT COUNT(*) FROM information_schema.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'task_runs' AND INDEX_NAME = 'idx_task_runs_worker_lease') = 0,
  'ALTER TABLE task_runs ADD INDEX idx_task_runs_worker_lease (worker_id, lease_expires_at)',
  'SELECT 1'
);
PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

SET @dpms_sql = IF(
  (SELECT COUNT(*) FROM information_schema.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'task_runs' AND INDEX_NAME = 'idx_task_runs_stream_message') = 0,
  'ALTER TABLE task_runs ADD INDEX idx_task_runs_stream_message (stream_message_id)',
  'SELECT 1'
);
PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

CREATE TABLE IF NOT EXISTS failed_task_messages (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  stream_key VARCHAR(128) NOT NULL,
  message_id VARCHAR(128) NOT NULL,
  task_id VARCHAR(128) NULL,
  reason VARCHAR(255) NOT NULL,
  payload JSON NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_failed_task_messages_task (task_id, created_at),
  INDEX idx_failed_task_messages_message (stream_key, message_id)
) ENGINE=InnoDB;
