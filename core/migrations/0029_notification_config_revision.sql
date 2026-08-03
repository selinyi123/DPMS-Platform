-- Bind notification delivery evidence to the current effective configuration.
-- Existing notify_logs rows remain intact with a NULL revision and therefore
-- cannot satisfy the revision-aware production gate.

CREATE TABLE IF NOT EXISTS notification_channel_revisions (
  channel VARCHAR(32) NOT NULL,
  revision BIGINT UNSIGNED NOT NULL DEFAULT 1,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (channel),
  CONSTRAINT chk_notification_channel_revision_channel CHECK (
    channel IN ('serverchan', 'feishu', 'webhook', 'telegram')
  ) ENFORCED,
  CONSTRAINT chk_notification_channel_revision_positive CHECK (
    revision > 0
  ) ENFORCED
) ENGINE=InnoDB;

INSERT INTO notification_channel_revisions (channel, revision)
VALUES
  ('serverchan', 1),
  ('feishu', 1),
  ('webhook', 1),
  ('telegram', 1)
ON DUPLICATE KEY UPDATE channel = VALUES(channel);

SET @dpms_0029_revision_column_exists = (
  SELECT COUNT(*)
  FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'notify_logs'
    AND COLUMN_NAME = 'config_revision'
);

SET @dpms_sql = IF(
  @dpms_0029_revision_column_exists > 0,
  'SELECT 1',
  'ALTER TABLE notify_logs
     ADD COLUMN config_revision VARCHAR(96) NULL AFTER success'
);

PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

SET @dpms_0029_revision_index_exists = (
  SELECT COUNT(*)
  FROM information_schema.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'notify_logs'
    AND INDEX_NAME = 'idx_notify_delivery_revision'
);

SET @dpms_sql = IF(
  @dpms_0029_revision_index_exists > 0,
  'SELECT 1',
  'ALTER TABLE notify_logs
     ADD INDEX idx_notify_delivery_revision
       (channel, config_revision, success, created_at, id)'
);

PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;
