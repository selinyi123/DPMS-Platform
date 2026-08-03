-- Runtime roles intentionally do not hold MySQL's TRIGGER privilege because it
-- permits trigger DDL. MySQL consequently hides information_schema.TRIGGERS
-- from them. Expose only the metadata required by the read-only startup schema
-- verifier through a fixed SQL SECURITY DEFINER routine.

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
