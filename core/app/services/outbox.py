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

from app.db import database, redis
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
            "SELECT id, stream_key, payload, attempts FROM outbox_events WHERE id = :id AND status = 'pending' FOR UPDATE",
            {"id": row_id},
        )
        if not row:
            return None
        await database.execute(
            "UPDATE outbox_events SET status = 'sending' WHERE id = :id",
            {"id": row_id},
        )
        return dict(row)


async def _deliver_claimed(row) -> bool:
    """Relay a row already claimed (status ``sending``) to Redis and finalise it."""
    attempts = int(row["attempts"]) + 1
    try:
        message = json.loads(row["payload"]) if isinstance(row["payload"], str) else dict(row["payload"])
        msg_id = await redis.xadd(row["stream_key"], message)
        if not msg_id:
            raise RuntimeError("xadd returned no id")
        await database.execute(
            "UPDATE outbox_events SET status = 'sent', sent_at = NOW(), attempts = :attempts WHERE id = :id",
            {"id": row["id"], "attempts": attempts},
        )
        return True
    except Exception as exc:
        await database.execute(
            "UPDATE outbox_events SET status = :status, attempts = :attempts, last_error = :err WHERE id = :id",
            {"id": row["id"], "status": terminal_status(attempts), "attempts": attempts, "err": str(exc)[:480]},
        )
        structured_log(
            "error" if not should_retry(attempts) else "warning",
            "outbox_relay_failed",
            outbox_id=row["id"],
            attempts=attempts,
            error=str(exc),
        )
        return False


async def _relay_by_id(row_id) -> bool:
    claimed = await _claim_row(row_id)
    if claimed is None:
        return False
    return await _deliver_claimed(claimed)


async def reclaim_stale_sending(threshold_seconds: int = OUTBOX_SENDING_RECLAIM_SECONDS) -> int:
    """Return rows stuck in ``sending`` (claimer crashed) to ``pending``."""
    result = await database.execute(
        """UPDATE outbox_events SET status = 'pending'
           WHERE status = 'sending' AND updated_at < (NOW() - INTERVAL :sec SECOND)""",
        {"sec": threshold_seconds},
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
    ``task_runs`` row, so it never races a live dispatch.
    """
    result = await database.execute(
        """UPDATE lotteries l
           LEFT JOIN task_runs tr ON tr.task_id = l.execution_lock
           SET l.status = 'pending', l.execution_lock = NULL, l.locked_at = NULL
           WHERE l.execution_lock IS NOT NULL
             AND l.locked_at IS NOT NULL
             AND l.locked_at < (NOW() - INTERVAL :grace MINUTE)
             AND (tr.task_id IS NULL OR tr.status IN ('succeeded', 'failed'))""",
        {"grace": grace_minutes},
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
