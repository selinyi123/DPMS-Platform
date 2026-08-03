-- Manual fail-closed rollback. Revision-bound delivery evidence must not be
-- discarded implicitly; remove/retain it through an explicit data decision.

DROP TEMPORARY TABLE IF EXISTS dpms_0029_rollback_guard;

CREATE TEMPORARY TABLE dpms_0029_rollback_guard (
  revision_bound_logs BIGINT UNSIGNED NOT NULL,
  CONSTRAINT chk_0029_no_revision_bound_logs
    CHECK (revision_bound_logs = 0) ENFORCED
);

INSERT INTO dpms_0029_rollback_guard (revision_bound_logs)
SELECT COUNT(*)
FROM notify_logs
WHERE config_revision IS NOT NULL;

DROP TEMPORARY TABLE dpms_0029_rollback_guard;

ALTER TABLE notify_logs DROP INDEX idx_notify_delivery_revision;

ALTER TABLE notify_logs DROP COLUMN config_revision;

DROP TABLE notification_channel_revisions;

DELETE FROM schema_migrations WHERE version = '0029';
