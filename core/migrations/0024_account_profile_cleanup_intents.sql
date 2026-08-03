-- Persist account-profile cleanup before credentials are reported as removed.
-- The queue is partitioned by platform so each platform Worker touches only
-- its own /profiles/{platform}/account_{id} namespace.

CREATE TABLE IF NOT EXISTS account_profile_cleanup_intents (
  id BIGINT NOT NULL AUTO_INCREMENT,
  account_id BIGINT NOT NULL,
  platform VARCHAR(32) NOT NULL,
  status VARCHAR(16) NOT NULL DEFAULT 'pending',
  attempts INT UNSIGNED NOT NULL DEFAULT 0,
  claim_token CHAR(36) NULL,
  worker_id VARCHAR(128) NULL,
  claimed_at TIMESTAMP NULL,
  next_attempt_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  completed_at TIMESTAMP NULL,
  last_error_code VARCHAR(128) NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_account_profile_cleanup_account (account_id),
  INDEX idx_account_profile_cleanup_pending (
    platform, status, next_attempt_at, id
  ),
  INDEX idx_account_profile_cleanup_running (
    platform, status, claimed_at, id
  ),
  CONSTRAINT fk_account_profile_cleanup_account
    FOREIGN KEY (account_id) REFERENCES accounts(id),
  CONSTRAINT chk_account_profile_cleanup_platform
    CHECK (
      platform IN ('bilibili', 'douyin', 'weibo', 'xiaohongshu')
    ) ENFORCED,
  CONSTRAINT chk_account_profile_cleanup_status
    CHECK (status IN ('pending', 'running', 'succeeded')) ENFORCED,
  CONSTRAINT chk_account_profile_cleanup_attempts
    CHECK (attempts <= 2147483647) ENFORCED,
  CONSTRAINT chk_account_profile_cleanup_lifecycle
    CHECK (
      (
        status = 'pending'
        AND claim_token IS NULL
        AND worker_id IS NULL
        AND claimed_at IS NULL
        AND completed_at IS NULL
      )
      OR (
        status = 'running'
        AND attempts > 0
        AND claim_token IS NOT NULL
        AND worker_id IS NOT NULL
        AND claimed_at IS NOT NULL
        AND completed_at IS NULL
      )
      OR (
        status = 'succeeded'
        AND attempts > 0
        AND claim_token IS NULL
        AND worker_id IS NULL
        AND claimed_at IS NULL
        AND completed_at IS NOT NULL
      )
    ) ENFORCED
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS login_profile_cleanup_intents (
  id BIGINT NOT NULL AUTO_INCREMENT,
  session_id CHAR(36) NOT NULL,
  status VARCHAR(16) NOT NULL DEFAULT 'pending',
  attempts INT UNSIGNED NOT NULL DEFAULT 0,
  claim_token CHAR(36) NULL,
  worker_id VARCHAR(128) NULL,
  claimed_at TIMESTAMP NULL,
  next_attempt_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  completed_at TIMESTAMP NULL,
  last_error_code VARCHAR(128) NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_login_profile_cleanup_session (session_id),
  INDEX idx_login_profile_cleanup_pending (
    status, next_attempt_at, id
  ),
  INDEX idx_login_profile_cleanup_running (
    status, claimed_at, id
  ),
  CONSTRAINT fk_login_profile_cleanup_session
    FOREIGN KEY (session_id) REFERENCES login_sessions(session_id),
  CONSTRAINT chk_login_profile_cleanup_status
    CHECK (status IN ('pending', 'running', 'succeeded')) ENFORCED,
  CONSTRAINT chk_login_profile_cleanup_attempts
    CHECK (attempts <= 2147483647) ENFORCED,
  CONSTRAINT chk_login_profile_cleanup_lifecycle
    CHECK (
      (
        status = 'pending'
        AND claim_token IS NULL
        AND worker_id IS NULL
        AND claimed_at IS NULL
        AND completed_at IS NULL
      )
      OR (
        status = 'running'
        AND attempts > 0
        AND claim_token IS NOT NULL
        AND worker_id IS NOT NULL
        AND claimed_at IS NOT NULL
        AND completed_at IS NULL
      )
      OR (
        status = 'succeeded'
        AND attempts > 0
        AND claim_token IS NULL
        AND worker_id IS NULL
        AND claimed_at IS NULL
        AND completed_at IS NOT NULL
      )
    ) ENFORCED
) ENGINE=InnoDB;

-- Retry-safe historical convergence: accounts soft-deleted before 0024 also
-- need their persistent Chromium state removed. A mismatched pre-existing
-- intent deliberately violates the platform CHECK instead of silently routing
-- an account to a different platform Worker.
INSERT INTO account_profile_cleanup_intents (
  account_id,
  platform,
  status,
  next_attempt_at
)
SELECT cleanup_source.account_id,
       cleanup_source.platform,
       cleanup_source.status,
       cleanup_source.next_attempt_at
FROM (
  SELECT account.id AS account_id,
         account.platform AS platform,
         'pending' AS status,
         NOW() AS next_attempt_at
  FROM accounts account
  WHERE account.deleted_at IS NOT NULL
) AS cleanup_source
ON DUPLICATE KEY UPDATE
  platform = IF(
    account_profile_cleanup_intents.platform = cleanup_source.platform,
    account_profile_cleanup_intents.platform,
    '__platform_mismatch__'
  );

INSERT INTO login_profile_cleanup_intents (
  session_id,
  status,
  next_attempt_at
)
SELECT login_session.session_id,
       'pending',
       NOW()
FROM login_sessions login_session
WHERE login_session.status IN ('confirmed', 'failed', 'expired')
ON DUPLICATE KEY UPDATE
  session_id = login_profile_cleanup_intents.session_id;
