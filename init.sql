
CREATE TABLE IF NOT EXISTS `fingerprints` (

  `id` BIGINT AUTO_INCREMENT PRIMARY KEY,

  `user_agent` TEXT NOT NULL,

  `platform` VARCHAR(32) NOT NULL DEFAULT 'bilibili',

  `viewport_width` INT DEFAULT 1920,

  `viewport_height` INT DEFAULT 1080,

  `timezone` VARCHAR(64) DEFAULT 'Asia/Shanghai',

  `locale` VARCHAR(16) DEFAULT 'zh-CN',

  `extra_headers` JSON NULL,

  UNIQUE KEY `uk_platform_ua` (`platform`, `user_agent`(255))

) ENGINE=InnoDB;



CREATE TABLE IF NOT EXISTS `proxies` (

  `id` BIGINT AUTO_INCREMENT PRIMARY KEY,

  `proxy_url` TEXT NOT NULL COMMENT 'socks5://user:pass@host:port',

  `proxy_type` VARCHAR(32) DEFAULT 'socks5',

  `provider` VARCHAR(64) NULL,

  `country` VARCHAR(32) NULL,

  `health_score` FLOAT DEFAULT 100,

  `cooldown_until` TIMESTAMP NULL COMMENT '污染冷却时间',

  `status` ENUM('active','degraded','dead') DEFAULT 'active',

  `last_check_at` TIMESTAMP NULL

) ENGINE=InnoDB;



CREATE TABLE IF NOT EXISTS `accounts` (

  `id` BIGINT AUTO_INCREMENT PRIMARY KEY,

  `platform` VARCHAR(32) NOT NULL DEFAULT 'bilibili',

  `fingerprint_id` BIGINT NOT NULL,

  `proxy_id` BIGINT NULL UNIQUE COMMENT '强制1:1',

  `encrypted_credential` BLOB NOT NULL COMMENT 'AES-256-GCM加密后的cookie+token',

  `key_version` SMALLINT NOT NULL DEFAULT 1,

  `status` VARCHAR(32) NOT NULL DEFAULT 'cold',

  `version` BIGINT NOT NULL DEFAULT 0 COMMENT '乐观锁',

  `risk_score` TINYINT UNSIGNED DEFAULT 0,

  `daily_task_count` INT DEFAULT 0,

  `last_active_at` TIMESTAMP NULL,

  `deleted_at` TIMESTAMP NULL,

  `deleted_by` VARCHAR(128) NULL,

  `delete_reason` VARCHAR(255) NULL,

  `created_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

  `updated_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

  FOREIGN KEY (`fingerprint_id`) REFERENCES `fingerprints`(`id`),

  FOREIGN KEY (`proxy_id`) REFERENCES `proxies`(`id`),

  INDEX `idx_status` (`platform`, `status`),

  INDEX `idx_accounts_deleted` (`deleted_at`, `status`)

) ENGINE=InnoDB;



CREATE TABLE IF NOT EXISTS `tracked_sources` (

  `id` BIGINT AUTO_INCREMENT PRIMARY KEY,

  `platform` VARCHAR(32) NOT NULL,

  `source_type` VARCHAR(32) NOT NULL,

  `source_value` VARCHAR(256) NOT NULL COMMENT 'UP uid 或关键词',

  `scan_interval_minutes` INT DEFAULT 30,

  `active` TINYINT DEFAULT 1,

  `last_scan_at` TIMESTAMP NULL,

  UNIQUE KEY `uk_source` (`platform`, `source_type`, `source_value`)

) ENGINE=InnoDB;



CREATE TABLE IF NOT EXISTS `lotteries` (

  `id` BIGINT AUTO_INCREMENT PRIMARY KEY,

  `platform` VARCHAR(32) NOT NULL DEFAULT 'bilibili',

  `source_type` VARCHAR(32) NOT NULL,

  `source_id` VARCHAR(64) NULL,

  `raw_url` VARCHAR(512) NOT NULL,

  `canonical_url` VARCHAR(512) NOT NULL,

  `url_hash` CHAR(64) GENERATED ALWAYS AS (SHA2(`canonical_url`, 256)) STORED,

  `title` VARCHAR(256) NULL,

  `rule_text` TEXT NULL,

  `action_plan` JSON NULL,

  `published_at` TIMESTAMP NULL,

  `status` ENUM('pending','claimed','running','participated','won','lost','expired') DEFAULT 'pending',

  `value_score` TINYINT UNSIGNED DEFAULT 0,

  `expires_at` TIMESTAMP NULL,

  `extracted_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

  `execution_lock` CHAR(36) NULL COMMENT 'UUID of worker who claimed',

  `locked_at` TIMESTAMP NULL,

  UNIQUE KEY `uk_url_hash` (`url_hash`),

  INDEX `idx_pending` (`platform`, `status`, `expires_at`)

) ENGINE=InnoDB;



CREATE TABLE IF NOT EXISTS `task_phases` (

  `id` BIGINT AUTO_INCREMENT PRIMARY KEY,

  `account_id` BIGINT NOT NULL,

  `lottery_id` BIGINT NOT NULL,

  `task_id` CHAR(36) NOT NULL COMMENT '对应 Redis 消息 ID 或自身 UUID',

  `phase` ENUM('init','followed','liked','commented','reposted','completed') DEFAULT 'init',

  `updated_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

  FOREIGN KEY (`account_id`) REFERENCES `accounts`(`id`),

  FOREIGN KEY (`lottery_id`) REFERENCES `lotteries`(`id`),

  UNIQUE KEY `uk_task_phase` (`task_id`),

  INDEX `idx_task_id` (`task_id`)

) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS `task_runs` (

  `id` BIGINT AUTO_INCREMENT PRIMARY KEY,

  `task_id` CHAR(36) NOT NULL,

  `account_id` BIGINT NOT NULL,

  `lottery_id` BIGINT NOT NULL,

  `status` ENUM('queued','running','succeeded','failed') DEFAULT 'queued',

  `dry_run` TINYINT DEFAULT 1,

  `task_mode` VARCHAR(32) DEFAULT 'dry_run',

  `decision_id` CHAR(36) NULL COMMENT 'real-run gate decision that authorised this task',

  `policy_version` INT NULL COMMENT 'active policy version at dispatch time',

  `error_message` TEXT NULL,

  `screenshot_path` VARCHAR(512) NULL,

  `created_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

  `started_at` TIMESTAMP NULL,

  `finished_at` TIMESTAMP NULL,

  FOREIGN KEY (`account_id`) REFERENCES `accounts`(`id`),

  FOREIGN KEY (`lottery_id`) REFERENCES `lotteries`(`id`),

  UNIQUE KEY `uk_task_run` (`task_id`),

  INDEX `idx_task_run_status` (`status`, `created_at`)

) ENGINE=InnoDB;



CREATE TABLE IF NOT EXISTS `risk_events` (

  `id` BIGINT AUTO_INCREMENT PRIMARY KEY,

  `account_id` BIGINT NOT NULL,

  `event_type` VARCHAR(64) NOT NULL COMMENT 'captcha/cooling/frozen/banned',

  `detail` JSON NULL,

  `created_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

  FOREIGN KEY (`account_id`) REFERENCES `accounts`(`id`)

) ENGINE=InnoDB;



CREATE TABLE IF NOT EXISTS `notify_logs` (

  `id` BIGINT AUTO_INCREMENT PRIMARY KEY,

  `channel` VARCHAR(32) NOT NULL,

  `title` VARCHAR(256),

  `content` TEXT,

  `success` TINYINT DEFAULT 1,

  `created_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP

) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS `events` (

  `id` CHAR(36) PRIMARY KEY,

  `aggregate` VARCHAR(64) NOT NULL,

  `aggregate_id` VARCHAR(128) NOT NULL,

  `event_type` VARCHAR(128) NOT NULL,

  `payload` JSON NULL,

  `correlation_id` VARCHAR(128) NULL,

  `causation_id` VARCHAR(128) NULL,

  `actor_type` VARCHAR(32) NOT NULL DEFAULT 'system',

  `actor_id` VARCHAR(128) NULL,

  `source_service` VARCHAR(64) NOT NULL DEFAULT 'core-api',

  `occurred_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

  INDEX `idx_events_aggregate` (`aggregate`, `aggregate_id`, `occurred_at`),

  INDEX `idx_events_type_created` (`event_type`, `occurred_at`),

  INDEX `idx_events_correlation` (`correlation_id`, `occurred_at`)

) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS `adapter_calibrations` (
  `id` BIGINT AUTO_INCREMENT PRIMARY KEY,
  `probe_id` CHAR(36) NOT NULL,
  `platform` VARCHAR(32) NOT NULL,
  `account_id` BIGINT NOT NULL,
  `lottery_id` BIGINT NULL,
  `target_url` VARCHAR(1024) NOT NULL,
  `status` ENUM('queued','running','succeeded','failed') DEFAULT 'queued',
  `result` JSON NULL,
  `screenshot_path` VARCHAR(512) NULL,
  `error_message` TEXT NULL,
  `created_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  `started_at` TIMESTAMP NULL,
  `finished_at` TIMESTAMP NULL,
  UNIQUE KEY `uk_probe_id` (`probe_id`),
  INDEX `idx_adapter_probe_status` (`status`, `created_at`),
  INDEX `idx_adapter_probe_platform` (`platform`, `created_at`),
  FOREIGN KEY (`account_id`) REFERENCES `accounts`(`id`),
  FOREIGN KEY (`lottery_id`) REFERENCES `lotteries`(`id`)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS `account_calibrations` (
  `id` BIGINT AUTO_INCREMENT PRIMARY KEY,
  `calibration_id` CHAR(36) NOT NULL,
  `platform` VARCHAR(32) NOT NULL,
  `account_id` BIGINT NOT NULL,
  `status` ENUM('queued','running','succeeded','failed') DEFAULT 'queued',
  `check_url` VARCHAR(1024) NOT NULL,
  `result` JSON NULL,
  `screenshot_path` VARCHAR(512) NULL,
  `error_message` TEXT NULL,
  `created_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  `started_at` TIMESTAMP NULL,
  `finished_at` TIMESTAMP NULL,
  UNIQUE KEY `uk_account_calibration_id` (`calibration_id`),
  INDEX `idx_account_calibration_account` (`account_id`, `created_at`),
  INDEX `idx_account_calibration_status` (`status`, `created_at`),
  FOREIGN KEY (`account_id`) REFERENCES `accounts`(`id`)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS `login_sessions` (

  `id` BIGINT AUTO_INCREMENT PRIMARY KEY,

  `session_id` CHAR(36) NOT NULL,

  `platform` VARCHAR(32) NOT NULL,

  `status` ENUM('queued','opening','waiting_scan','confirmed','expired','failed') DEFAULT 'queued',

  `login_url` VARCHAR(512) NOT NULL,

  `qr_image_path` VARCHAR(512) NULL,

  `account_id` BIGINT NULL,

  `error_message` TEXT NULL,

  `created_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

  `updated_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

  `expires_at` TIMESTAMP NULL,

  `completed_at` TIMESTAMP NULL,

  UNIQUE KEY `uk_login_session` (`session_id`),

  INDEX `idx_login_status` (`status`, `created_at`)

) ENGINE=InnoDB;



CREATE TABLE IF NOT EXISTS `system_versions` (

  `id` BIGINT AUTO_INCREMENT PRIMARY KEY,

  `version` VARCHAR(32) NOT NULL,

  `description` TEXT,

  `file_hash` CHAR(64) NULL,

  `deployed_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP

) ENGINE=InnoDB;

ALTER TABLE `lotteries`
  MODIFY `status` ENUM('pending','claimed','running','participated','won','lost','expired') DEFAULT 'pending';
