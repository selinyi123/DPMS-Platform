-- Second-phase security and durability contract.
--
-- This migration is additive and intentionally does not rewrite the frozen
-- 0011 checksum.  The old observation indexes are retired below only when
-- they still exist, so a MySQL 8 installation converges to one index contract
-- without changing the published migration history.

CREATE TABLE IF NOT EXISTS platform_runtime_security_domains (
  platform VARCHAR(32) NOT NULL,
  status ENUM('compat','provisioning','active','rotating','revoked') NOT NULL DEFAULT 'compat',
  database_user VARCHAR(64) NOT NULL,
  database_name VARCHAR(64) NOT NULL,
  core_redis_user VARCHAR(64) NOT NULL,
  worker_redis_user VARCHAR(64) NOT NULL,
  encryption_key_fingerprint CHAR(64) NULL,
  generation BIGINT UNSIGNED NOT NULL DEFAULT 1,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (platform),
  UNIQUE KEY uk_platform_security_database_user (database_user),
  UNIQUE KEY uk_platform_security_core_redis_user (core_redis_user),
  UNIQUE KEY uk_platform_security_worker_redis_user (worker_redis_user),
  INDEX idx_platform_security_status (status, updated_at)
) ENGINE=InnoDB;

INSERT IGNORE INTO platform_runtime_security_domains
  (platform, database_user, database_name, core_redis_user, worker_redis_user)
VALUES
  ('bilibili', 'dpms_runtime_bilibili', 'lottery_bilibili', 'core-bilibili', 'worker-bilibili'),
  ('weibo', 'dpms_runtime_weibo', 'lottery_weibo', 'core-weibo', 'worker-weibo'),
  ('xiaohongshu', 'dpms_runtime_xiaohongshu', 'lottery_xiaohongshu', 'core-xiaohongshu', 'worker-xiaohongshu'),
  ('douyin', 'dpms_runtime_douyin', 'lottery_douyin', 'core-douyin', 'worker-douyin')
;

CREATE TABLE IF NOT EXISTS notification_delivery_attempts (
  delivery_key VARCHAR(191) NOT NULL,
  stream_message_id VARCHAR(128) NULL,
  channel VARCHAR(32) NOT NULL,
  notify_log_id BIGINT NULL,
  status ENUM('pending','sending','sent','failed','uncertain') NOT NULL DEFAULT 'pending',
  attempts INT NOT NULL DEFAULT 0,
  claim_token CHAR(32) NULL,
  config_revision VARCHAR(96) NULL,
  last_error VARCHAR(512) NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  sent_at TIMESTAMP NULL,
  uncertain_at TIMESTAMP NULL,
  PRIMARY KEY (delivery_key),
  UNIQUE KEY uk_notification_delivery_message_channel (stream_message_id, channel),
  INDEX idx_notification_delivery_status (status, updated_at),
  INDEX idx_notification_delivery_log (notify_log_id, channel)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS outbox_archive_watermarks (
  stream_key VARCHAR(128) NOT NULL,
  continuity_epoch VARCHAR(128) NOT NULL,
  safe_outbox_id BIGINT UNSIGNED NOT NULL,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (stream_key)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS outbox_event_archive (
  source_table VARCHAR(32) NOT NULL,
  source_id BIGINT UNSIGNED NOT NULL,
  stream_key VARCHAR(128) NULL,
  payload JSON NOT NULL,
  dedup_key VARCHAR(191) NULL,
  delivery_epoch VARCHAR(128) NULL,
  created_at TIMESTAMP NULL,
  sent_at TIMESTAMP NULL,
  archived_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (source_table, source_id),
  INDEX idx_outbox_archive_stream_time (stream_key, archived_at),
  INDEX idx_outbox_archive_dedup (dedup_key)
) ENGINE=InnoDB;

SET @dpms_sql = IF(
  (SELECT COUNT(*) FROM information_schema.COLUMNS
   WHERE TABLE_SCHEMA = DATABASE()
     AND TABLE_NAME = 'outbox_events'
     AND COLUMN_NAME = 'archived_at') > 0,
  'SELECT 1',
  'ALTER TABLE outbox_events ADD COLUMN archived_at TIMESTAMP NULL AFTER sent_at'
);
PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

SET @dpms_sql = IF(
  (SELECT COUNT(*) FROM information_schema.COLUMNS
   WHERE TABLE_SCHEMA = DATABASE()
     AND TABLE_NAME = 'task_outbox_events'
     AND COLUMN_NAME = 'archived_at') > 0,
  'SELECT 1',
  'ALTER TABLE task_outbox_events ADD COLUMN archived_at TIMESTAMP NULL AFTER sent_at'
);
PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

SET @dpms_sql = IF(
  (SELECT COUNT(*) FROM information_schema.COLUMNS
   WHERE TABLE_SCHEMA = DATABASE()
     AND TABLE_NAME = 'task_outbox_events'
     AND COLUMN_NAME = 'redis_delivery_epoch') > 0,
  'SELECT 1',
  'ALTER TABLE task_outbox_events ADD COLUMN redis_delivery_epoch VARCHAR(128) NULL AFTER sent_at'
);
PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

SET @dpms_sql = IF(
  (SELECT COUNT(*) FROM information_schema.STATISTICS
   WHERE TABLE_SCHEMA = DATABASE()
     AND TABLE_NAME = 'outbox_events'
     AND INDEX_NAME = 'idx_outbox_archive_ready') > 0,
  'SELECT 1',
  'ALTER TABLE outbox_events ADD INDEX idx_outbox_archive_ready (stream_key, status, archived_at, id)'
);
PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

SET @dpms_sql = IF(
  (SELECT COUNT(*) FROM information_schema.STATISTICS
   WHERE TABLE_SCHEMA = DATABASE()
     AND TABLE_NAME = 'task_outbox_events'
     AND INDEX_NAME = 'idx_task_outbox_archive_ready') > 0,
  'SELECT 1',
  'ALTER TABLE task_outbox_events ADD INDEX idx_task_outbox_archive_ready (stream_key, status, archived_at, id)'
);
PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

-- 0011 deliberately kept the old names until the v2 indexes were proven. A
-- new installation may still have either name depending on where it stopped;
-- drop only the obsolete names and never touch the v2 contract.
SET @dpms_sql = IF(
  (SELECT COUNT(*) FROM information_schema.STATISTICS
   WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'adapter_calibrations'
     AND INDEX_NAME = 'uk_adapter_probe_exact_binding') > 0,
  'ALTER TABLE adapter_calibrations DROP INDEX uk_adapter_probe_exact_binding',
  'SELECT 1'
);
PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

SET @dpms_sql = IF(
  (SELECT COUNT(*) FROM information_schema.STATISTICS
   WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'adapter_calibrations'
     AND INDEX_NAME = 'idx_adapter_probe_binding_lookup') > 0,
  'ALTER TABLE adapter_calibrations DROP INDEX idx_adapter_probe_binding_lookup',
  'SELECT 1'
);
PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

SET @dpms_sql = IF(
  (SELECT COUNT(*) FROM information_schema.STATISTICS
   WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'task_runs'
     AND INDEX_NAME = 'uk_task_run_shadow_binding') > 0,
  'ALTER TABLE task_runs DROP INDEX uk_task_run_shadow_binding',
  'SELECT 1'
);
PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

SET @dpms_sql = IF(
  (SELECT COUNT(*) FROM information_schema.STATISTICS
   WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'task_runs'
     AND INDEX_NAME = 'idx_task_run_shadow_evidence_lookup') > 0,
  'ALTER TABLE task_runs DROP INDEX idx_task_run_shadow_evidence_lookup',
  'SELECT 1'
);
PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

SET @dpms_sql = IF(
  (SELECT COUNT(*) FROM information_schema.STATISTICS
   WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'execution_evidence_bindings'
     AND INDEX_NAME = 'idx_execution_evidence_exact_binding') > 0,
  'ALTER TABLE execution_evidence_bindings DROP INDEX idx_execution_evidence_exact_binding',
  'SELECT 1'
);
PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;
