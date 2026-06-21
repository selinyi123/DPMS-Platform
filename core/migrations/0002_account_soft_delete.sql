-- Preserve account audit history while allowing operators to retire an account.
ALTER TABLE accounts
  ADD COLUMN deleted_at TIMESTAMP NULL,
  ADD COLUMN deleted_by VARCHAR(128) NULL,
  ADD COLUMN delete_reason VARCHAR(255) NULL,
  ADD INDEX idx_accounts_deleted (deleted_at, status);
