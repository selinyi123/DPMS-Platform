-- Optional retry-safe rollback. Historical risk_events remain authoritative;
-- reapplying 0023 reconstructs the current state from the last 24 hours.

DROP TRIGGER IF EXISTS trg_risk_events_active_state;

DROP TABLE IF EXISTS account_active_risk_states;

DELETE FROM schema_migrations WHERE version = '0023';
