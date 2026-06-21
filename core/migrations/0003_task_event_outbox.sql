CREATE TABLE IF NOT EXISTS task_outbox_events (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  event_kind ENUM('event_store','notify_stream') NOT NULL,
  aggregate VARCHAR(64) NULL,
  aggregate_id VARCHAR(128) NULL,
  event_type VARCHAR(128) NULL,
  stream_key VARCHAR(128) NULL,
  payload JSON NOT NULL,
  status ENUM('pending','sending','sent','failed') NOT NULL DEFAULT 'pending',
  dedup_key VARCHAR(191) NOT NULL,
  attempts INT NOT NULL DEFAULT 0,
  last_error VARCHAR(512) NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  sent_at TIMESTAMP NULL,
  UNIQUE KEY uk_task_outbox_dedup (dedup_key),
  INDEX idx_task_outbox_status (status, id),
  INDEX idx_task_outbox_aggregate (aggregate, aggregate_id, id)
) ENGINE=InnoDB;