ALTER TABLE task_runs
  ADD COLUMN worker_id VARCHAR(128) NULL,
  ADD COLUMN stream_message_id VARCHAR(128) NULL,
  ADD COLUMN lease_expires_at TIMESTAMP NULL,
  ADD INDEX idx_task_runs_worker_lease (worker_id, lease_expires_at),
  ADD INDEX idx_task_runs_stream_message (stream_message_id);

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