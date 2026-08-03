-- Manual rollback for 0028.

DROP PROCEDURE IF EXISTS dpms_required_trigger_metadata;

DELETE FROM schema_migrations WHERE version = '0028';
