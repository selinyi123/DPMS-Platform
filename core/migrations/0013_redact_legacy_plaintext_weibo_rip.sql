UPDATE task_runs tr
JOIN outbox_events o
  ON o.dedup_key = tr.task_id
 AND o.stream_key = 'lottery_tasks'
LEFT JOIN lotteries l
  ON l.id = tr.lottery_id
 AND l.execution_lock = tr.task_id
 AND l.status = 'claimed'
LEFT JOIN account_operation_leases lease
  ON lease.lease_id = tr.account_lease_id
 AND lease.account_id = tr.account_id
 AND lease.generation = tr.account_lease_generation
 AND lease.operation_kind = tr.task_mode
 AND lease.owner_id = tr.task_id
 AND lease.task_id = tr.task_id
SET tr.status = 'failed',
    tr.error_message = 'legacy_plaintext_weibo_rip_redacted',
    tr.finished_at = COALESCE(tr.finished_at, NOW()),
    tr.worker_id = NULL,
    tr.stream_message_id = NULL,
    tr.lease_expires_at = NULL,
    tr.reconciliation_required = 0,
    l.status = 'pending',
    l.execution_lock = NULL,
    l.locked_at = NULL,
    lease.released_at = COALESCE(lease.released_at, NOW())
WHERE tr.status = 'queued'
  AND JSON_CONTAINS_PATH(o.payload, 'one', '$.weibo_rip') = 1
  AND COALESCE(NULLIF(JSON_UNQUOTE(JSON_EXTRACT(o.payload, '$.weibo_rip')), 'null'), '') <> '';

UPDATE outbox_events
SET last_error = CASE
      WHEN status IN ('pending', 'sending') THEN 'legacy_plaintext_weibo_rip_redacted'
      ELSE last_error
    END,
    status = CASE
      WHEN status IN ('pending', 'sending') THEN 'failed'
      ELSE status
    END
WHERE stream_key = 'lottery_tasks'
  AND JSON_CONTAINS_PATH(payload, 'one', '$.weibo_rip') = 1
  AND COALESCE(NULLIF(JSON_UNQUOTE(JSON_EXTRACT(payload, '$.weibo_rip')), 'null'), '') <> '';

UPDATE outbox_events
SET payload = JSON_SET(
      JSON_REMOVE(payload, '$.weibo_rip'),
      '$.weibo_rip_encrypted',
      ''
    )
WHERE stream_key = 'lottery_tasks'
  AND JSON_CONTAINS_PATH(payload, 'one', '$.weibo_rip') = 1;

UPDATE failed_task_messages
SET payload = JSON_REMOVE(payload, '$.weibo_rip')
WHERE stream_key = 'lottery_tasks'
  AND payload IS NOT NULL
  AND JSON_CONTAINS_PATH(payload, 'one', '$.weibo_rip') = 1;
