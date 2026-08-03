-- Maintain one exact, transactionally updated active-risk row per account.
-- The one-time backfill may scan recent history; steady-state readiness reads
-- never rescan a high-frequency account's 24-hour risk-event population.

CREATE TABLE IF NOT EXISTS account_active_risk_states (
  account_id BIGINT NOT NULL,
  risk_event_id BIGINT NOT NULL,
  event_type VARCHAR(64) NOT NULL,
  detail JSON NULL,
  event_created_at TIMESTAMP NOT NULL,
  active_until TIMESTAMP NOT NULL,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (account_id),
  UNIQUE KEY uk_account_active_risk_event (risk_event_id),
  INDEX idx_account_active_risk_until (active_until, account_id),
  CONSTRAINT fk_account_active_risk_account
    FOREIGN KEY (account_id) REFERENCES accounts(id),
  CONSTRAINT fk_account_active_risk_event
    FOREIGN KEY (risk_event_id) REFERENCES risk_events(id)
) ENGINE=InnoDB;

-- Install the write-side contract before scanning history. Events committed
-- while the bounded backfill is running are therefore represented
-- transactionally and cannot fall into an online-migration gap.
DROP TRIGGER IF EXISTS trg_risk_events_active_state;

CREATE TRIGGER trg_risk_events_active_state AFTER INSERT ON risk_events FOR EACH ROW INSERT INTO account_active_risk_states (account_id, risk_event_id, event_type, detail, event_created_at, active_until) SELECT incoming.account_id, incoming.risk_event_id, incoming.event_type, incoming.detail, incoming.event_created_at, incoming.active_until FROM (SELECT NEW.account_id AS account_id, NEW.id AS risk_event_id, NEW.event_type AS event_type, NEW.detail AS detail, NEW.created_at AS event_created_at, TIMESTAMPADD(HOUR, CASE LOWER(TRIM(COALESCE(JSON_UNQUOTE(JSON_EXTRACT(NEW.detail, '$.reason')), ''))) WHEN 'action_window' THEN 4 WHEN 'sliding_window_exceeded' THEN 4 ELSE 24 END, NEW.created_at) AS active_until) AS incoming ON DUPLICATE KEY UPDATE event_type = IF(incoming.active_until > account_active_risk_states.active_until OR (incoming.active_until = account_active_risk_states.active_until AND incoming.risk_event_id > account_active_risk_states.risk_event_id), incoming.event_type, account_active_risk_states.event_type), detail = IF(incoming.active_until > account_active_risk_states.active_until OR (incoming.active_until = account_active_risk_states.active_until AND incoming.risk_event_id > account_active_risk_states.risk_event_id), incoming.detail, account_active_risk_states.detail), event_created_at = IF(incoming.active_until > account_active_risk_states.active_until OR (incoming.active_until = account_active_risk_states.active_until AND incoming.risk_event_id > account_active_risk_states.risk_event_id), incoming.event_created_at, account_active_risk_states.event_created_at), risk_event_id = IF(incoming.active_until > account_active_risk_states.active_until OR (incoming.active_until = account_active_risk_states.active_until AND incoming.risk_event_id > account_active_risk_states.risk_event_id), incoming.risk_event_id, account_active_risk_states.risk_event_id), active_until = GREATEST(account_active_risk_states.active_until, incoming.active_until);

INSERT INTO account_active_risk_states (
  account_id,
  risk_event_id,
  event_type,
  detail,
  event_created_at,
  active_until
)
SELECT account_id,
       id,
       event_type,
       detail,
       created_at,
       active_until
FROM (
  SELECT active_events.*,
         ROW_NUMBER() OVER (
           PARTITION BY account_id
           ORDER BY active_until DESC, created_at DESC, id DESC
         ) AS active_risk_rank
  FROM (
    SELECT re.id,
           re.account_id,
           re.event_type,
           re.detail,
           re.created_at,
           TIMESTAMPADD(
             HOUR,
             CASE LOWER(
               TRIM(
                 COALESCE(
                   JSON_UNQUOTE(JSON_EXTRACT(re.detail, '$.reason')),
                   ''
                 )
               )
             )
               WHEN 'action_window' THEN 4
               WHEN 'sliding_window_exceeded' THEN 4
               ELSE 24
             END,
             re.created_at
           ) AS active_until
    FROM risk_events re
    WHERE re.created_at >= DATE_SUB(NOW(), INTERVAL 24 HOUR)
  ) AS active_events
  WHERE active_until > NOW()
) AS ranked_active_events
WHERE active_risk_rank = 1
ON DUPLICATE KEY UPDATE
  event_type = IF(
    ranked_active_events.active_until
      > account_active_risk_states.active_until
      OR (
        ranked_active_events.active_until
          = account_active_risk_states.active_until
        AND ranked_active_events.id
          > account_active_risk_states.risk_event_id
      ),
    ranked_active_events.event_type,
    account_active_risk_states.event_type
  ),
  detail = IF(
    ranked_active_events.active_until
      > account_active_risk_states.active_until
      OR (
        ranked_active_events.active_until
          = account_active_risk_states.active_until
        AND ranked_active_events.id
          > account_active_risk_states.risk_event_id
      ),
    ranked_active_events.detail,
    account_active_risk_states.detail
  ),
  event_created_at = IF(
    ranked_active_events.active_until
      > account_active_risk_states.active_until
      OR (
        ranked_active_events.active_until
          = account_active_risk_states.active_until
        AND ranked_active_events.id
          > account_active_risk_states.risk_event_id
      ),
    ranked_active_events.created_at,
    account_active_risk_states.event_created_at
  ),
  risk_event_id = IF(
    ranked_active_events.active_until
      > account_active_risk_states.active_until
      OR (
        ranked_active_events.active_until
          = account_active_risk_states.active_until
        AND ranked_active_events.id
          > account_active_risk_states.risk_event_id
      ),
    ranked_active_events.id,
    account_active_risk_states.risk_event_id
  ),
  active_until = GREATEST(
    account_active_risk_states.active_until,
    ranked_active_events.active_until
  );
