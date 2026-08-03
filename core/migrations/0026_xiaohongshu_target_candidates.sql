-- Xiaohongshu target pursuit is a read-only discovery pipeline.  Its sources
-- and review candidates must remain separate from tracked_sources/run_discovery
-- because that legacy path inserts discoveries directly into executable
-- lotteries.  Only an explicit accepted decision creates or reuses a lottery.

CREATE TABLE IF NOT EXISTS xiaohongshu_target_sources (
  id BIGINT NOT NULL AUTO_INCREMENT,
  source_type VARCHAR(32) NOT NULL,
  source_value VARCHAR(256)
    CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
  active TINYINT UNSIGNED NOT NULL DEFAULT 1,
  last_scan_at TIMESTAMP(6) NULL,
  status VARCHAR(16) NOT NULL DEFAULT 'idle',
  last_error_code VARCHAR(128) NULL,
  version BIGINT UNSIGNED NOT NULL DEFAULT 1,
  created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  updated_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
    ON UPDATE CURRENT_TIMESTAMP(6),
  PRIMARY KEY (id),
  UNIQUE KEY uk_xhs_target_source_identity (source_type, source_value),
  INDEX idx_xhs_target_source_scan_queue (
    active,
    status,
    last_scan_at,
    id
  ),
  CONSTRAINT chk_xhs_target_source_type CHECK (
    source_type IN ('keyword', 'author_profile', 'offline_search_result')
  ) ENFORCED,
  CONSTRAINT chk_xhs_target_source_active CHECK (
    active IN (0, 1)
  ) ENFORCED,
  CONSTRAINT chk_xhs_target_source_status CHECK (
    status IN ('idle', 'scanning', 'succeeded', 'failed')
  ) ENFORCED,
  CONSTRAINT chk_xhs_target_source_version CHECK (
    version > 0
  ) ENFORCED
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS xiaohongshu_target_candidates (
  id BIGINT NOT NULL AUTO_INCREMENT,
  platform VARCHAR(32) NOT NULL DEFAULT 'xiaohongshu',
  raw_url VARCHAR(512) NOT NULL,
  canonical_url VARCHAR(512) NOT NULL,
  url_hash CHAR(64)
    GENERATED ALWAYS AS (SHA2(canonical_url, 256)) STORED,
  title VARCHAR(256) NULL,
  evidence JSON NOT NULL,
  rule JSON NOT NULL,
  classification JSON NOT NULL,
  published_at TIMESTAMP NULL,
  value_score TINYINT UNSIGNED NOT NULL DEFAULT 0,
  expires_at TIMESTAMP NULL,
  decision_status VARCHAR(16) NOT NULL DEFAULT 'pending',
  decision_reason VARCHAR(512) NULL,
  accepted_lottery_id BIGINT NULL,
  version BIGINT UNSIGNED NOT NULL DEFAULT 1,
  first_seen_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  last_seen_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  decided_at TIMESTAMP(6) NULL,
  decision_actor_id VARCHAR(128) NULL,
  created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  updated_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
    ON UPDATE CURRENT_TIMESTAMP(6),
  PRIMARY KEY (id),
  UNIQUE KEY uk_xhs_target_candidate_url_hash (url_hash),
  INDEX idx_xhs_target_candidate_review_queue (
    decision_status,
    last_seen_at,
    id
  ),
  INDEX idx_xhs_target_candidate_accepted_lottery (
    accepted_lottery_id,
    id
  ),
  CONSTRAINT fk_xhs_target_candidate_lottery
    FOREIGN KEY (accepted_lottery_id) REFERENCES lotteries(id),
  CONSTRAINT chk_xhs_target_candidate_platform CHECK (
    platform = 'xiaohongshu'
  ) ENFORCED,
  CONSTRAINT chk_xhs_target_candidate_decision CHECK (
    decision_status IN ('pending', 'accepted', 'skipped', 'needs_review')
  ) ENFORCED,
  CONSTRAINT chk_xhs_target_candidate_accept_binding CHECK (
    (
      decision_status = 'accepted'
      AND accepted_lottery_id IS NOT NULL
    )
    OR (
      decision_status <> 'accepted'
      AND accepted_lottery_id IS NULL
    )
  ) ENFORCED,
  CONSTRAINT chk_xhs_target_candidate_version CHECK (
    version > 0
  ) ENFORCED,
  CONSTRAINT chk_xhs_target_candidate_seen_order CHECK (
    last_seen_at >= first_seen_at
  ) ENFORCED
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS xiaohongshu_target_candidate_source_hits (
  id BIGINT NOT NULL AUTO_INCREMENT,
  candidate_id BIGINT NOT NULL,
  source_id BIGINT NOT NULL,
  tracked_source_id BIGINT NULL,
  source_type VARCHAR(32) NOT NULL,
  source_value VARCHAR(256) NOT NULL,
  evidence JSON NOT NULL,
  hit_count BIGINT UNSIGNED NOT NULL DEFAULT 1,
  first_seen_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  last_seen_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  updated_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
    ON UPDATE CURRENT_TIMESTAMP(6),
  PRIMARY KEY (id),
  UNIQUE KEY uk_xhs_target_candidate_source_hit (
    candidate_id,
    source_id
  ),
  INDEX idx_xhs_target_source_hit_queue (
    source_id,
    last_seen_at,
    candidate_id
  ),
  INDEX idx_xhs_target_hit_tracked_source (
    tracked_source_id,
    id
  ),
  CONSTRAINT fk_xhs_target_hit_candidate
    FOREIGN KEY (candidate_id)
    REFERENCES xiaohongshu_target_candidates(id)
    ON DELETE CASCADE,
  CONSTRAINT fk_xhs_target_hit_source
    FOREIGN KEY (source_id)
    REFERENCES xiaohongshu_target_sources(id),
  CONSTRAINT fk_xhs_target_hit_tracked_source
    FOREIGN KEY (tracked_source_id)
    REFERENCES tracked_sources(id),
  CONSTRAINT chk_xhs_target_hit_source_type CHECK (
    source_type IN ('keyword', 'author_profile', 'offline_search_result')
  ) ENFORCED,
  CONSTRAINT chk_xhs_target_hit_count CHECK (
    hit_count > 0
  ) ENFORCED,
  CONSTRAINT chk_xhs_target_hit_seen_order CHECK (
    last_seen_at >= first_seen_at
  ) ENFORCED
) ENGINE=InnoDB;
