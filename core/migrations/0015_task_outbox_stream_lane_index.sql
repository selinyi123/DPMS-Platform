-- Bound each platform Outbox relay to an index range for its own stream.
-- The signature guard repairs a same-named but drifted index and makes a
-- retry after MySQL's atomic ALTER safe before schema_migrations is recorded.

SET @dpms_outbox_lane_index_signature = (
  SELECT GROUP_CONCAT(
           CONCAT(
             SEQ_IN_INDEX, ':', COLUMN_NAME, ':', NON_UNIQUE, ':',
             COALESCE(CAST(SUB_PART AS CHAR), 'FULL'), ':',
             IS_VISIBLE
           )
           ORDER BY SEQ_IN_INDEX
           SEPARATOR ','
         )
  FROM information_schema.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'outbox_events'
    AND INDEX_NAME = 'idx_outbox_stream_status_id'
);

SET @dpms_sql = CASE
  WHEN @dpms_outbox_lane_index_signature =
       '1:stream_key:1:FULL:YES,2:status:1:FULL:YES,3:id:1:FULL:YES'
    THEN 'SELECT 1'
  WHEN @dpms_outbox_lane_index_signature IS NULL
    THEN 'ALTER TABLE outbox_events
            ADD INDEX idx_outbox_stream_status_id
              (stream_key, status, id) VISIBLE'
  ELSE 'ALTER TABLE outbox_events
          DROP INDEX idx_outbox_stream_status_id,
          ADD INDEX idx_outbox_stream_status_id
            (stream_key, status, id) VISIBLE'
END;

PREPARE dpms_stmt FROM @dpms_sql;
EXECUTE dpms_stmt;
DEALLOCATE PREPARE dpms_stmt;

-- MySQL preserves an existing index's visibility when a same-named index is
-- dropped and re-added in one ALTER, even when ADD says VISIBLE.  Normalize it
-- explicitly so FORCE/optimizer use cannot remain disabled after drift repair.
ALTER TABLE outbox_events
  ALTER INDEX idx_outbox_stream_status_id VISIBLE;
