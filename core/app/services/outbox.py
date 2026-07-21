"""Transactional outbox for Redis stream enqueues (P1-2).

The dispatch path previously did three independent writes with no shared
transaction:

    INSERT task_runs            (DB)
    UPDATE lotteries claimed    (DB)
    redis.xadd lottery_tasks    (Redis)

If the process died — or Redis was briefly unavailable — between the DB writes
and the ``xadd``, the lottery was left ``claimed`` with a ``task_runs`` row but
the worker never received the job: a silent stuck task. Conversely a duplicate
click could enqueue two live tasks for the same lottery.

This module makes the enqueue part of the *same* database transaction as the
state mutation, by writing the would-be stream message into an
``outbox_events`` row inside the transaction. A background dispatcher then
relays committed outbox rows to Redis at-least-once, keying on ``dedup_key`` so
a retry (or an immediate best-effort flush racing the dispatcher) never enqueues
the same task twice.

The message builder and the retry predicates are intentionally pure so they can
be unit-tested without a database or Redis (this repo has no DB-integration
test harness).
"""

import json

from app.db import database, execute_affected_rows, redis
from app.utils.log import structured_log


# Every field the worker's ``execute_task_with_phases`` needs to run a task.
# Kept as an explicit contract so the dispatch builder, the recovery daemon and
# any future producer all agree on the message shape.
LOTTERY_TASK_FIELDS = (
    "task_id",
    "account_id",
    "lottery_id",
    "platform",
    "raw_url",
    "canonical_url",
    "dry_run",
    "mode",
    "selector_config",
    "action_plan",
    "rule_snapshot_id",
    "rule_hash",
    "action_plan_hash",
    "execution_evidence_id",
    "execution_path_id",
    "target_hash",
    "config_hash",
    "execution_revision",
    "account_lease_id",
    "account_lease_generation",
)

OUTBOX_MAX_ATTEMPTS = 5
OUTBOX_BATCH = 50
OUTBOX_POLL_SECONDS = 5
# A row claimed for delivery (``sending``) by a relayer that then crashed is
# reclaimed to ``pending`` after this window so it is not stuck forever.
OUTBOX_SENDING_RECLAIM_SECONDS = 60
# Lotteries left locked this long whose task already reached a terminal state
# (or never materialised) get their execution lock released by the reconciler.
ORPHAN_LOCK_GRACE_MINUTES = 15


def build_lottery_task_message(
    *,
    task_id: str,
    account_id,
    lottery_id,
    platform: str,
    raw_url: str | None,
    canonical_url: str | None,
    task_mode: str,
    dry_run: bool,
    platform_selectors,
    action_plan,
    rule_snapshot_id=None,
    rule_hash: str | None = None,
    action_plan_hash: str | None = None,
    execution_evidence_id: str | None = None,
    execution_path_id: str | None = None,
    target_hash: str | None = None,
    config_hash: str | None = None,
    execution_revision=None,
    account_lease_id: str | None = None,
    account_lease_generation=None,
) -> dict[str, str]:
    """Build the Redis-stream message for a lottery task.

    Pure: every value is coerced to ``str`` (Redis stream fields must be
    str/bytes/int/float) and the field set is exactly ``LOTTERY_TASK_FIELDS``.
    """
    selectors = platform_selectors if isinstance(platform_selectors, dict) else {}
    plan = action_plan if isinstance(action_plan, (dict, list)) else {}
    return {
        "task_id": str(task_id),
        "account_id": str(account_id),
        "lottery_id": str(lottery_id),
        "platform": platform or "",
        "raw_url": raw_url or "",
        "canonical_url": canonical_url or "",
        "dry_run": "0" if not dry_run else "1",
        "mode": task_mode,
        "selector_config": json.dumps(selectors, ensure_ascii=False),
        "action_plan": json.dumps(plan, ensure_ascii=False),
        "rule_snapshot_id": str(rule_snapshot_id or ""),
        "rule_hash": str(rule_hash or ""),
        "action_plan_hash": str(action_plan_hash or ""),
        "execution_evidence_id": str(execution_evidence_id or ""),
        "execution_path_id": str(execution_path_id or ""),
        "target_hash": str(target_hash or ""),
        "config_hash": str(config_hash or ""),
        "execution_revision": str(execution_revision or ""),
        "account_lease_id": str(account_lease_id or ""),
        "account_lease_generation": str(account_lease_generation or ""),
    }


def should_retry(attempts: int) -> bool:
    """Whether an outbox row that failed ``attempts`` times may be retried."""
    return attempts < OUTBOX_MAX_ATTEMPTS


def terminal_status(attempts: int) -> str:
    """Status to set after a failed flush: still ``pending`` if retryable."""
    return "pending" if should_retry(attempts) else "failed"


async def enqueue_outbox(message: dict[str, str], stream_key: str, *, dedup_key: str | None = None) -> None:
    """Insert an outbox row. MUST be called inside an open DB transaction.

    ``dedup_key`` (the task_id) makes the relay idempotent: a unique index on it
    means a second enqueue for the same task is a no-op rather than a duplicate
    job.
    """
    await database.execute(
        """INSERT INTO outbox_events (stream_key, payload, status, dedup_key)
           VALUES (:stream_key, :payload, 'pending', :dedup_key)
           ON DUPLICATE KEY UPDATE id = id""",
        {
            "stream_key": stream_key,
            "payload": json.dumps(message, ensure_ascii=False),
            "dedup_key": dedup_key,
        },
    )


async def _claim_row(row_id) -> dict | None:
    """Atomically move a pending row to ``sending`` so exactly one relayer owns it.

    The ``SELECT ... FOR UPDATE`` serialises the immediate post-commit flush
    against the background dispatcher, preventing a double ``xadd`` (which would
    run a task twice). Returns the claimed row, or ``None`` if someone else got
    there first / it is no longer pending.
    """
    async with database.transaction():
        row = await database.fetch_one(
            "SELECT id, stream_key, payload, attempts, dedup_key FROM outbox_events WHERE id = :id AND status = 'pending' FOR UPDATE",
            {"id": row_id},
        )
        if not row:
            return None
        claim_attempt = int(row["attempts"] or 0) + 1
        await database.execute(
            """UPDATE outbox_events
               SET status = 'sending', attempts = :attempts
               WHERE id = :id AND status = 'pending'""",
            {"id": row_id, "attempts": claim_attempt},
        )
        claimed = dict(row)
        claimed["attempts"] = claim_attempt
        return claimed


async def _deliver_claimed(row) -> bool:
    """Relay a row already claimed (status ``sending``) to Redis and finalise it."""
    # ``attempts`` is incremented when the row is claimed and acts as a
    # no-schema fencing token. A stale relayer may still have an in-flight
    # Redis request (the stream is intentionally at-least-once), but it cannot
    # overwrite the state owned by a newer claim generation.
    attempts = int(row["attempts"])
    try:
        message = json.loads(row["payload"]) if isinstance(row["payload"], str) else dict(row["payload"])
        msg_id = await redis.xadd(row["stream_key"], message)
        if not msg_id:
            raise RuntimeError("xadd returned no id")
        updated = await execute_affected_rows(
            """UPDATE outbox_events
               SET status = 'sent', sent_at = NOW()
               WHERE id = :id AND status = 'sending' AND attempts = :attempts""",
            {"id": row["id"], "attempts": attempts},
            db=database,
        )
        if updated == 0:
            structured_log(
                "warning",
                "outbox_stale_delivery_receipt_ignored",
                outbox_id=row["id"],
                attempts=attempts,
            )
            return False
        return True
    except Exception as exc:
        status = terminal_status(attempts)
        if status == "failed":
            await _settle_terminal_delivery_failure(row, attempts, exc)
        else:
            await database.execute(
                """UPDATE outbox_events
                   SET status = 'pending', last_error = :err
                   WHERE id = :id AND status = 'sending' AND attempts = :attempts""",
                {"id": row["id"], "attempts": attempts, "err": str(exc)[:480]},
            )
        structured_log(
            "error" if not should_retry(attempts) else "warning",
            "outbox_relay_failed",
            outbox_id=row["id"],
            attempts=attempts,
            error=str(exc),
        )
        return False


async def _settle_terminal_delivery_failure(row: dict, attempts: int, exc: BaseException) -> None:
    """Atomically fail an undeliverable queued task and release its claim.

    Without this closure, an outbox row that reaches its retry limit remains
    ``failed`` while the task stays ``queued`` and the lottery stays ``claimed``
    forever. No worker or orphan-lock reconciler can then make progress.
    """
    error = str(exc)[:480]
    async with database.transaction():
        current = await database.fetch_one(
            """SELECT id, stream_key, dedup_key, payload, status, attempts
               FROM outbox_events WHERE id = :id FOR UPDATE""",
            {"id": row["id"]},
        )
        if not current:
            return
        if (
            str(current["status"] or "").strip().lower() != "sending"
            or int(current["attempts"] or 0) != attempts
        ):
            return
        await database.execute(
            """UPDATE outbox_events
               SET status = 'failed', last_error = :err
               WHERE id = :id AND status = 'sending' AND attempts = :attempts""",
            {"id": row["id"], "attempts": attempts, "err": error},
        )
        stream_key = str(current["stream_key"] or "").strip()
        if stream_key == "adapter_probe_requests":
            current_data = dict(current)
            payload = current_data.get("payload")
            try:
                message = json.loads(payload) if isinstance(payload, str) else dict(payload or {})
            except (TypeError, ValueError, json.JSONDecodeError):
                message = {}
            probe_id = str(message.get("probe_id") or "").strip()
            if not probe_id:
                dedup_key = str(current_data.get("dedup_key") or "").strip()
                prefix = "adapter-probe:"
                probe_id = dedup_key[len(prefix):] if dedup_key.startswith(prefix) else ""
            if probe_id:
                probe = await database.fetch_one(
                    """SELECT account_id, status, account_lease_id,
                              account_lease_generation
                       FROM adapter_calibrations
                       WHERE probe_id = :probe_id FOR UPDATE""",
                    {"probe_id": probe_id},
                )
                # A Redis XADD may have succeeded even when the relay did not
                # receive its acknowledgement.  Once a Worker has claimed the
                # probe, an outbox timeout must not fail it or release the
                # lease underneath the in-flight observation.
                if not probe or str(probe["status"] or "").strip().lower() != "queued":
                    return
                await database.fetch_one(
                    "SELECT id FROM accounts WHERE id = :account_id FOR UPDATE",
                    {"account_id": probe["account_id"]},
                )
                await database.execute(
                    """UPDATE adapter_calibrations
                       SET status = 'failed', error_message = :error, finished_at = NOW()
                       WHERE probe_id = :probe_id AND status = 'queued'""",
                    {
                        "probe_id": probe_id,
                        "error": f"outbox delivery exhausted: {error}"[:480],
                    },
                )
                await database.execute(
                    """UPDATE account_operation_leases
                       SET released_at = COALESCE(released_at, NOW())
                       WHERE lease_id = :lease_id
                         AND account_id = :account_id
                         AND generation = :lease_generation
                         AND operation_kind = 'adapter_probe'
                         AND owner_id = :probe_id""",
                    {
                        "lease_id": probe["account_lease_id"],
                        "account_id": probe["account_id"],
                        "lease_generation": probe["account_lease_generation"],
                        "probe_id": probe_id,
                    },
                )
            return

        task_id = str(current["dedup_key"] or "").strip()
        if stream_key != "lottery_tasks" or not task_id:
            return
        task = await database.fetch_one(
            """SELECT task_id, account_id, lottery_id, status, task_mode,
                      account_lease_id, account_lease_generation
               FROM task_runs WHERE task_id = :task_id FOR UPDATE""",
            {"task_id": task_id},
        )
        if not task or str(task["status"] or "").strip().lower() != "queued":
            return
        await database.fetch_one(
            "SELECT id, status, execution_lock FROM lotteries WHERE id = :lottery_id FOR UPDATE",
            {"lottery_id": task["lottery_id"]},
        )
        await database.fetch_one(
            "SELECT id FROM accounts WHERE id = :account_id FOR UPDATE",
            {"account_id": task["account_id"]},
        )
        await database.execute(
            """UPDATE task_runs
               SET status = 'failed', error_message = :error, finished_at = NOW(),
                   worker_id = NULL, stream_message_id = NULL, lease_expires_at = NULL
               WHERE task_id = :task_id AND status = 'queued'""",
            {"task_id": task_id, "error": f"outbox delivery exhausted: {error}"[:480]},
        )
        await database.execute(
            """UPDATE lotteries SET status = 'pending', execution_lock = NULL, locked_at = NULL
               WHERE id = :lottery_id AND execution_lock = :task_id AND status = 'claimed'""",
            {"lottery_id": task["lottery_id"], "task_id": task_id},
        )
        await database.execute(
            """UPDATE account_operation_leases
               SET released_at = COALESCE(released_at, NOW())
               WHERE lease_id = :lease_id
                 AND account_id = :account_id
                 AND generation = :lease_generation
                 AND operation_kind = :operation_kind
                 AND owner_id = :task_id""",
            {
                "lease_id": task["account_lease_id"],
                "account_id": task["account_id"],
                "lease_generation": task["account_lease_generation"],
                "operation_kind": str(task["task_mode"] or "").strip().lower(),
                "task_id": task_id,
            },
        )


async def _relay_by_id(row_id) -> bool:
    claimed = await _claim_row(row_id)
    if claimed is None:
        return False
    return await _deliver_claimed(claimed)


async def reclaim_stale_sending(threshold_seconds: int = OUTBOX_SENDING_RECLAIM_SECONDS) -> int:
    """Return rows stuck in ``sending`` (claimer crashed) to ``pending``."""
    result = await execute_affected_rows(
        """UPDATE outbox_events SET status = 'pending'
           WHERE status = 'sending' AND updated_at < (NOW() - INTERVAL :sec SECOND)""",
        {"sec": threshold_seconds},
        db=database,
    )
    return int(result or 0)


async def flush_pending_outbox(limit: int = OUTBOX_BATCH) -> dict[str, int]:
    """Relay up to ``limit`` pending outbox rows to Redis (one-at-a-time claim)."""
    rows = await database.fetch_all(
        "SELECT id FROM outbox_events WHERE status = 'pending' ORDER BY id LIMIT :limit",
        {"limit": limit},
    )
    sent = 0
    for row in rows:
        if await _relay_by_id(row["id"]):
            sent += 1
    return {"scanned": len(rows), "sent": sent}


async def try_flush_dedup(dedup_key: str) -> bool:
    """Best-effort immediate relay of the row for ``dedup_key`` after commit.

    Keeps dispatch latency low without giving up durability: the claim makes it
    safe against the dispatcher, and a failure just leaves the row for retry.
    """
    row = await database.fetch_one(
        "SELECT id FROM outbox_events WHERE dedup_key = :k AND status = 'pending'",
        {"k": dedup_key},
    )
    if not row:
        return False
    return await _relay_by_id(row["id"])


async def reconcile_orphaned_locks(grace_minutes: int = ORPHAN_LOCK_GRACE_MINUTES) -> int:
    """Release lottery execution locks stranded by a terminal/absent task.

    Conservative (roadmap item 5): only touches lotteries locked longer than the
    grace window whose claiming task already finished or never created a
    ``task_runs`` row, so it never races a live dispatch.  Reconciliation is a
    structured database contract: error-message wording is never authorization
    to release a lock, and an unsettled external-action intent independently
    keeps the lock quarantined.
    """
    result = await execute_affected_rows(
        """UPDATE lotteries l
           LEFT JOIN task_runs tr ON tr.task_id = l.execution_lock
           SET l.status = CASE
                 WHEN tr.task_mode = 'real_run' AND tr.status = 'succeeded'
                   THEN 'participated'
                 ELSE 'pending'
               END,
               l.execution_lock = NULL, l.locked_at = NULL
           WHERE l.execution_lock IS NOT NULL
             AND l.locked_at IS NOT NULL
             AND l.locked_at < (NOW() - INTERVAL :grace MINUTE)
             AND (
               tr.task_id IS NULL
               OR (
                 tr.status IN ('succeeded', 'failed')
                 AND tr.reconciliation_required = 0
                 -- A failed task may be retried only when every durable
                 -- intent is either not started or explicitly confirmed to
                 -- have produced no external effect.  Status wording alone
                 -- is never evidence that a timeout was side-effect free.
                 AND (
                   tr.status = 'succeeded'
                   OR NOT EXISTS (
                     SELECT 1
                     FROM external_action_intents eai
                     WHERE eai.task_id = tr.task_id
                       AND (
                         eai.status IN ('started', 'unknown', 'succeeded')
                         OR eai.effect_certainty IN ('unknown', 'confirmed_effect')
                         OR (
                           eai.status = 'failed'
                           AND eai.effect_certainty <> 'confirmed_no_effect'
                         )
                       )
                   )
                 )
               )
             )""",
        {"grace": grace_minutes},
        db=database,
    )
    return int(result or 0)


async def start_outbox_dispatcher():
    """Background loop: relay pending outbox rows and reconcile stale locks."""
    import asyncio

    while True:
        try:
            await reclaim_stale_sending()
            await flush_pending_outbox()
            await reconcile_orphaned_locks()
        except Exception as exc:
            structured_log("error", "outbox_dispatcher_error", exception=exc)
        await asyncio.sleep(OUTBOX_POLL_SECONDS)
