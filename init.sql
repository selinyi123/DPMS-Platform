
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

  `execution_revision` BIGINT UNSIGNED NOT NULL DEFAULT 1 COMMENT 'Cookie/代理等执行身份修订号',

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

  INDEX `idx_accounts_deleted` (`deleted_at`, `status`),

  INDEX `idx_account_strategy_candidate`
    (`platform`, `status`, `deleted_at`, `daily_task_count`, `id`),

  CONSTRAINT `chk_account_execution_revision` CHECK (`execution_revision` > 0)

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

  INDEX `idx_pending` (`platform`, `status`, `expires_at`),

  INDEX `idx_lottery_extracted_platform_id`
    (`extracted_at`, `platform`, `id`)

) ENGINE=InnoDB;



CREATE TABLE IF NOT EXISTS `xiaohongshu_target_sources` (

  `id` BIGINT NOT NULL AUTO_INCREMENT,

  `source_type` VARCHAR(32) NOT NULL,

  `source_value` VARCHAR(256)
    CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,

  `active` TINYINT UNSIGNED NOT NULL DEFAULT 1,

  `last_scan_at` TIMESTAMP(6) NULL,

  `status` VARCHAR(16) NOT NULL DEFAULT 'idle',

  `last_error_code` VARCHAR(128) NULL,

  `version` BIGINT UNSIGNED NOT NULL DEFAULT 1,

  `created_at` TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),

  `updated_at` TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
    ON UPDATE CURRENT_TIMESTAMP(6),

  PRIMARY KEY (`id`),

  UNIQUE KEY `uk_xhs_target_source_identity`
    (`source_type`, `source_value`),

  INDEX `idx_xhs_target_source_scan_queue`
    (`active`, `status`, `last_scan_at`, `id`),

  CONSTRAINT `chk_xhs_target_source_type`
    CHECK (
      `source_type` IN (
        'keyword',
        'author_profile',
        'offline_search_result'
      )
    ) ENFORCED,

  CONSTRAINT `chk_xhs_target_source_active`
    CHECK (`active` IN (0, 1)) ENFORCED,

  CONSTRAINT `chk_xhs_target_source_status`
    CHECK (
      `status` IN ('idle', 'scanning', 'succeeded', 'failed')
    ) ENFORCED,

  CONSTRAINT `chk_xhs_target_source_version`
    CHECK (`version` > 0) ENFORCED

) ENGINE=InnoDB;



CREATE TABLE IF NOT EXISTS `xiaohongshu_target_candidates` (

  `id` BIGINT NOT NULL AUTO_INCREMENT,

  `platform` VARCHAR(32) NOT NULL DEFAULT 'xiaohongshu',

  `raw_url` VARCHAR(512) NOT NULL,

  `canonical_url` VARCHAR(512) NOT NULL,

  `url_hash` CHAR(64)
    GENERATED ALWAYS AS (SHA2(`canonical_url`, 256)) STORED,

  `title` VARCHAR(256) NULL,

  `evidence` JSON NOT NULL,

  `rule` JSON NOT NULL,

  `classification` JSON NOT NULL,

  `published_at` TIMESTAMP NULL,

  `value_score` TINYINT UNSIGNED NOT NULL DEFAULT 0,

  `expires_at` TIMESTAMP NULL,

  `decision_status` VARCHAR(16) NOT NULL DEFAULT 'pending',

  `decision_reason` VARCHAR(512) NULL,

  `accepted_lottery_id` BIGINT NULL,

  `version` BIGINT UNSIGNED NOT NULL DEFAULT 1,

  `first_seen_at` TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),

  `last_seen_at` TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),

  `decided_at` TIMESTAMP(6) NULL,

  `decision_actor_id` VARCHAR(128) NULL,

  `created_at` TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),

  `updated_at` TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
    ON UPDATE CURRENT_TIMESTAMP(6),

  PRIMARY KEY (`id`),

  UNIQUE KEY `uk_xhs_target_candidate_url_hash` (`url_hash`),

  INDEX `idx_xhs_target_candidate_review_queue`
    (`decision_status`, `last_seen_at`, `id`),

  INDEX `idx_xhs_target_candidate_accepted_lottery`
    (`accepted_lottery_id`, `id`),

  CONSTRAINT `fk_xhs_target_candidate_lottery`
    FOREIGN KEY (`accepted_lottery_id`) REFERENCES `lotteries`(`id`),

  CONSTRAINT `chk_xhs_target_candidate_platform`
    CHECK (`platform` = 'xiaohongshu') ENFORCED,

  CONSTRAINT `chk_xhs_target_candidate_decision`
    CHECK (
      `decision_status` IN (
        'pending',
        'accepted',
        'skipped',
        'needs_review'
      )
    ) ENFORCED,

  CONSTRAINT `chk_xhs_target_candidate_accept_binding`
    CHECK (
      (
        `decision_status` = 'accepted'
        AND `accepted_lottery_id` IS NOT NULL
      )
      OR (
        `decision_status` <> 'accepted'
        AND `accepted_lottery_id` IS NULL
      )
    ) ENFORCED,

  CONSTRAINT `chk_xhs_target_candidate_version`
    CHECK (`version` > 0) ENFORCED,

  CONSTRAINT `chk_xhs_target_candidate_seen_order`
    CHECK (`last_seen_at` >= `first_seen_at`) ENFORCED

) ENGINE=InnoDB;



CREATE TABLE IF NOT EXISTS `xiaohongshu_target_candidate_source_hits` (

  `id` BIGINT NOT NULL AUTO_INCREMENT,

  `candidate_id` BIGINT NOT NULL,

  `source_id` BIGINT NOT NULL,

  `tracked_source_id` BIGINT NULL,

  `source_type` VARCHAR(32) NOT NULL,

  `source_value` VARCHAR(256) NOT NULL,

  `evidence` JSON NOT NULL,

  `hit_count` BIGINT UNSIGNED NOT NULL DEFAULT 1,

  `first_seen_at` TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),

  `last_seen_at` TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),

  `created_at` TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),

  `updated_at` TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
    ON UPDATE CURRENT_TIMESTAMP(6),

  PRIMARY KEY (`id`),

  UNIQUE KEY `uk_xhs_target_candidate_source_hit`
    (`candidate_id`, `source_id`),

  INDEX `idx_xhs_target_source_hit_queue`
    (`source_id`, `last_seen_at`, `candidate_id`),

  INDEX `idx_xhs_target_hit_tracked_source`
    (`tracked_source_id`, `id`),

  CONSTRAINT `fk_xhs_target_hit_candidate`
    FOREIGN KEY (`candidate_id`)
    REFERENCES `xiaohongshu_target_candidates`(`id`)
    ON DELETE RESTRICT
    ON UPDATE RESTRICT,

  CONSTRAINT `fk_xhs_target_hit_source`
    FOREIGN KEY (`source_id`)
    REFERENCES `xiaohongshu_target_sources`(`id`),

  CONSTRAINT `fk_xhs_target_hit_tracked_source`
    FOREIGN KEY (`tracked_source_id`) REFERENCES `tracked_sources`(`id`),

  CONSTRAINT `chk_xhs_target_hit_source_type`
    CHECK (
      `source_type` IN (
        'keyword',
        'author_profile',
        'offline_search_result'
      )
    ) ENFORCED,

  CONSTRAINT `chk_xhs_target_hit_count`
    CHECK (`hit_count` > 0) ENFORCED,

  CONSTRAINT `chk_xhs_target_hit_seen_order`
    CHECK (`last_seen_at` >= `first_seen_at`) ENFORCED

) ENGINE=InnoDB;



CREATE TABLE IF NOT EXISTS `task_phases` (

  `id` BIGINT AUTO_INCREMENT PRIMARY KEY,

  `account_id` BIGINT NOT NULL,

  `lottery_id` BIGINT NOT NULL,

  `task_id` CHAR(36) NOT NULL COMMENT '对应 Redis 消息 ID 或自身 UUID',

  `phase` ENUM('init','followed','liked','commented','favorited','reposted','completed') DEFAULT 'init',

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

  INDEX `idx_task_run_status` (`status`, `created_at`),

  INDEX `idx_task_run_account_created_id`
    (`account_id`, `created_at`, `id`),

  INDEX `idx_task_run_created_lottery_id`
    (`created_at`, `lottery_id`, `id`)

) ENGINE=InnoDB;



CREATE TABLE IF NOT EXISTS `risk_events` (

  `id` BIGINT AUTO_INCREMENT PRIMARY KEY,

  `account_id` BIGINT NOT NULL,

  `event_type` VARCHAR(64) NOT NULL COMMENT 'captcha/cooling/frozen/banned',

  `detail` JSON NULL,

  `created_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

  INDEX `idx_risk_account_created_id` (`account_id`, `created_at`, `id`),

  INDEX `idx_risk_created_account_id` (`created_at`, `account_id`, `id`),

  FOREIGN KEY (`account_id`) REFERENCES `accounts`(`id`)

) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS `account_active_risk_states` (

  `account_id` BIGINT NOT NULL,

  `risk_event_id` BIGINT NOT NULL,

  `event_type` VARCHAR(64) NOT NULL,

  `detail` JSON NULL,

  `event_created_at` TIMESTAMP NOT NULL,

  `active_until` TIMESTAMP NOT NULL,

  `updated_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    ON UPDATE CURRENT_TIMESTAMP,

  PRIMARY KEY (`account_id`),

  UNIQUE KEY `uk_account_active_risk_event` (`risk_event_id`),

  INDEX `idx_account_active_risk_until` (`active_until`, `account_id`),

  CONSTRAINT `fk_account_active_risk_account`
    FOREIGN KEY (`account_id`) REFERENCES `accounts`(`id`),

  CONSTRAINT `fk_account_active_risk_event`
    FOREIGN KEY (`risk_event_id`) REFERENCES `risk_events`(`id`)

) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS `bilibili_action_ledger` (

  `id` BIGINT AUTO_INCREMENT PRIMARY KEY,

  `task_id` CHAR(36) NOT NULL,

  `account_id` BIGINT NOT NULL,

  `lottery_id` BIGINT NOT NULL,

  `dynamic_id` VARCHAR(64) NULL,

  `action` VARCHAR(32) NOT NULL,

  `phase` VARCHAR(32) NULL,

  `code` INT NULL,

  `outcome` VARCHAR(32) NOT NULL,

  `message` TEXT NULL,

  `ok` TINYINT DEFAULT 0,

  `task_mode` VARCHAR(32) NOT NULL DEFAULT 'real_run',

  `source` VARCHAR(32) NOT NULL DEFAULT 'api_real_run',

  `created_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

  `updated_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

  UNIQUE KEY `uk_bilibili_action_task_action` (`task_id`, `action`),

  INDEX `idx_bilibili_action_lottery_created` (`lottery_id`, `created_at`),

  INDEX `idx_bilibili_action_account_created` (`account_id`, `created_at`),

  INDEX `idx_bilibili_action_outcome_created` (`outcome`, `created_at`),

  FOREIGN KEY (`task_id`) REFERENCES `task_runs`(`task_id`),

  FOREIGN KEY (`account_id`) REFERENCES `accounts`(`id`),

  FOREIGN KEY (`lottery_id`) REFERENCES `lotteries`(`id`)

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
  INDEX `idx_account_calibration_platform_queued`
    (`platform`, `status`, `created_at`, `id`),
  INDEX `idx_account_calibration_platform_running`
    (`platform`, `status`, `started_at`, `created_at`, `id`),
  INDEX `idx_account_calibration_account_platform_id`
    (`account_id`, `platform`, `id`),
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



CREATE TABLE IF NOT EXISTS `account_profile_cleanup_intents` (

  `id` BIGINT NOT NULL AUTO_INCREMENT,

  `account_id` BIGINT NOT NULL,

  `platform` VARCHAR(32) NOT NULL,

  `status` VARCHAR(16) NOT NULL DEFAULT 'pending',

  `attempts` INT UNSIGNED NOT NULL DEFAULT 0,

  `claim_token` CHAR(36) NULL,

  `worker_id` VARCHAR(128) NULL,

  `claimed_at` TIMESTAMP NULL,

  `next_attempt_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

  `completed_at` TIMESTAMP NULL,

  `last_error_code` VARCHAR(128) NULL,

  `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

  `updated_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

  PRIMARY KEY (`id`),

  UNIQUE KEY `uk_account_profile_cleanup_account` (`account_id`),

  INDEX `idx_account_profile_cleanup_pending` (`platform`, `status`, `next_attempt_at`, `id`),

  INDEX `idx_account_profile_cleanup_running` (`platform`, `status`, `claimed_at`, `id`),

  CONSTRAINT `fk_account_profile_cleanup_account`
    FOREIGN KEY (`account_id`) REFERENCES `accounts`(`id`),

  CONSTRAINT `chk_account_profile_cleanup_platform`
    CHECK (`platform` IN ('bilibili', 'douyin', 'weibo', 'xiaohongshu')) ENFORCED,

  CONSTRAINT `chk_account_profile_cleanup_status`
    CHECK (`status` IN ('pending', 'running', 'succeeded')) ENFORCED,

  CONSTRAINT `chk_account_profile_cleanup_attempts`
    CHECK (`attempts` <= 2147483647) ENFORCED,

  CONSTRAINT `chk_account_profile_cleanup_lifecycle`
    CHECK (
      (
        `status` = 'pending'
        AND `claim_token` IS NULL
        AND `worker_id` IS NULL
        AND `claimed_at` IS NULL
        AND `completed_at` IS NULL
      )
      OR (
        `status` = 'running'
        AND `attempts` > 0
        AND `claim_token` IS NOT NULL
        AND `worker_id` IS NOT NULL
        AND `claimed_at` IS NOT NULL
        AND `completed_at` IS NULL
      )
      OR (
        `status` = 'succeeded'
        AND `attempts` > 0
        AND `claim_token` IS NULL
        AND `worker_id` IS NULL
        AND `claimed_at` IS NULL
        AND `completed_at` IS NOT NULL
      )
    ) ENFORCED

) ENGINE=InnoDB;



CREATE TABLE IF NOT EXISTS `login_profile_cleanup_intents` (

  `id` BIGINT NOT NULL AUTO_INCREMENT,

  `session_id` CHAR(36) NOT NULL,

  `status` VARCHAR(16) NOT NULL DEFAULT 'pending',

  `attempts` INT UNSIGNED NOT NULL DEFAULT 0,

  `claim_token` CHAR(36) NULL,

  `worker_id` VARCHAR(128) NULL,

  `claimed_at` TIMESTAMP NULL,

  `next_attempt_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

  `completed_at` TIMESTAMP NULL,

  `last_error_code` VARCHAR(128) NULL,

  `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

  `updated_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

  PRIMARY KEY (`id`),

  UNIQUE KEY `uk_login_profile_cleanup_session` (`session_id`),

  INDEX `idx_login_profile_cleanup_pending` (`status`, `next_attempt_at`, `id`),

  INDEX `idx_login_profile_cleanup_running` (`status`, `claimed_at`, `id`),

  CONSTRAINT `fk_login_profile_cleanup_session`
    FOREIGN KEY (`session_id`) REFERENCES `login_sessions`(`session_id`),

  CONSTRAINT `chk_login_profile_cleanup_status`
    CHECK (`status` IN ('pending', 'running', 'succeeded')) ENFORCED,

  CONSTRAINT `chk_login_profile_cleanup_attempts`
    CHECK (`attempts` <= 2147483647) ENFORCED,

  CONSTRAINT `chk_login_profile_cleanup_lifecycle`
    CHECK (
      (
        `status` = 'pending'
        AND `claim_token` IS NULL
        AND `worker_id` IS NULL
        AND `claimed_at` IS NULL
        AND `completed_at` IS NULL
      )
      OR (
        `status` = 'running'
        AND `attempts` > 0
        AND `claim_token` IS NOT NULL
        AND `worker_id` IS NOT NULL
        AND `claimed_at` IS NOT NULL
        AND `completed_at` IS NULL
      )
      OR (
        `status` = 'succeeded'
        AND `attempts` > 0
        AND `claim_token` IS NULL
        AND `worker_id` IS NULL
        AND `claimed_at` IS NULL
        AND `completed_at` IS NOT NULL
      )
    ) ENFORCED

) ENGINE=InnoDB;



CREATE TABLE IF NOT EXISTS `account_profile_context_leases` (

  `account_id` BIGINT NOT NULL,

  `platform` VARCHAR(32) NOT NULL,

  `lease_token` CHAR(36) NOT NULL,

  `owner_id` VARCHAR(128) NOT NULL,

  `acquired_at` TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),

  `renewed_at` TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),

  `lease_expires_at` TIMESTAMP(6) NOT NULL,

  `created_at` TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),

  `updated_at` TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
    ON UPDATE CURRENT_TIMESTAMP(6),

  PRIMARY KEY (`account_id`),

  UNIQUE KEY `uk_account_profile_context_lease_token` (`lease_token`),

  INDEX `idx_account_profile_context_lease_expiry`
    (`platform`, `lease_expires_at`, `account_id`),

  CONSTRAINT `fk_account_profile_context_lease_account`
    FOREIGN KEY (`account_id`) REFERENCES `accounts`(`id`),

  CONSTRAINT `chk_account_profile_context_lease_platform`
    CHECK (
      `platform` IN ('bilibili', 'douyin', 'weibo', 'xiaohongshu')
    ) ENFORCED,

  CONSTRAINT `chk_account_profile_context_lease_lifecycle`
    CHECK (
      `renewed_at` >= `acquired_at`
      AND `lease_expires_at` > `renewed_at`
    ) ENFORCED

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

DROP PROCEDURE IF EXISTS dpms_required_trigger_metadata;

CREATE PROCEDURE dpms_required_trigger_metadata()
SQL SECURITY DEFINER
READS SQL DATA
SELECT
  'dpms-trigger-metadata-v1' AS CONTRACT_VERSION,
  TRIGGER_NAME,
  EVENT_MANIPULATION,
  EVENT_OBJECT_TABLE,
  ACTION_TIMING,
  ACTION_STATEMENT
FROM information_schema.TRIGGERS
WHERE TRIGGER_SCHEMA = DATABASE();
