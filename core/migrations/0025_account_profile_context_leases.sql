-- Fence one persistent Chromium profile to one live Worker process.
--
-- Account-operation leases protect business actions, but they do not cover
-- selector probes, calibrations, or an idle persistent browser context. This
-- lease is intentionally scoped only to the on-disk profile owner. Deleting an
-- account locks the same accounts row; once deleted_at is set, a Worker cannot
-- acquire or renew this lease. Expiry permits cleanup to attempt the separate
-- process-lifetime profile flock; it is never treated as proof that Chromium
-- has stopped.

CREATE TABLE IF NOT EXISTS account_profile_context_leases (
  account_id BIGINT NOT NULL,
  platform VARCHAR(32) NOT NULL,
  lease_token CHAR(36) NOT NULL,
  owner_id VARCHAR(128) NOT NULL,
  acquired_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  renewed_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  lease_expires_at TIMESTAMP(6) NOT NULL,
  created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  updated_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
    ON UPDATE CURRENT_TIMESTAMP(6),
  PRIMARY KEY (account_id),
  UNIQUE KEY uk_account_profile_context_lease_token (lease_token),
  INDEX idx_account_profile_context_lease_expiry (
    platform, lease_expires_at, account_id
  ),
  CONSTRAINT fk_account_profile_context_lease_account
    FOREIGN KEY (account_id) REFERENCES accounts(id),
  CONSTRAINT chk_account_profile_context_lease_platform
    CHECK (
      platform IN ('bilibili', 'douyin', 'weibo', 'xiaohongshu')
    ) ENFORCED,
  CONSTRAINT chk_account_profile_context_lease_lifecycle
    CHECK (
      renewed_at >= acquired_at
      AND lease_expires_at > renewed_at
    ) ENFORCED
) ENGINE=InnoDB;
