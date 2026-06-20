"""Recovery daemon: re-dispatch tasks whose stream message went stale.

When a worker claims a ``lottery_tasks`` message but dies before acking it, the
message lingers in the consumer group's pending list. This daemon reclaims such
messages after an idle threshold and re-enqueues them.

Safety / correctness (P0-2): the re-enqueued message must be a *complete* task
payload — the same field set the original dispatch produced — rebuilt from the
authoritative database rows (``task_runs`` + ``lotteries``), not a stub. A stub
carrying only ``task_id`` made the worker fail immediately (``int(None)`` on the
missing ``account_id``), which then looped back into recovery until the task was
permanently failed. We reuse the *recorded* account/lottery/mode so recovery
never re-selects an account or changes the execution mode; the worker still
re-validates the account and the real-run gate at execution time.
"""

import asyncio
import json

from app.adapter_config import load_runtime_selector_config
from app.db import database, redis
from app.services.outbox import build_lottery_task_message
from app.utils.log import structured_log


STREAM_KEY = "lottery_tasks"
GROUP_NAME = "workers"
RECOVERY_CONSUMER = "recovery-daemon"
MAX_RECOVERY_COUNT = 3
IDLE_THRESHOLD_MS = 120_000
WORKER_HEARTBEAT_STALE_SECONDS = 90
TERMINAL_TASK_STATUSES = {"succeeded", "failed"}


def pending_idle_ms(entry: dict) -> int:
    """Idle time (ms) for a redis ``xpending_range`` entry.

    redis-py parses each pending entry with the keys ``message_id``,
    ``consumer``, ``time_since_delivered`` and ``times_delivered`` — there is no
    ``idle`` key, and ``time_since_delivered`` is already the elapsed
    milliseconds since the message was last delivered to a consumer.
    """
    return int(entry.get("time_since_delivered") or 0)


async def start_recovery_daemon():
    while True:
        try:
            pending = await redis.xpending_range(
                STREAM_KEY, GROUP_NAME, min="-", max="+", count=50
            )
            for msg in pending:
                idle_ms = pending_idle_ms(msg)
                if idle_ms < IDLE_THRESHOLD_MS:
                    continue

                message_id = msg["message_id"]
                fields = await _read_stream_fields(message_id)
                task_id = fields.get("task_id", message_id)

                decision = await _recovery_decision(task_id)
                if decision == "skip_running_with_live_worker":
                    structured_log(
                        "info",
                        "recovery_skipped_running_task_with_live_worker",
                        task_id=task_id,
                        message_id=message_id,
                        idle_ms=idle_ms,
                    )
                    continue
                if decision == "ack_terminal_task":
                    structured_log(
                        "info",
                        "recovery_ack_terminal_task",
                        task_id=task_id,
                        message_id=message_id,
                    )
                    await redis.xack(STREAM_KEY, GROUP_NAME, message_id)
                    await redis.delete(f"recovery_count:{task_id}")
                    continue

                claimed = await redis.xclaim(
                    STREAM_KEY, GROUP_NAME, RECOVERY_CONSUMER,
                    min_idle_time=IDLE_THRESHOLD_MS,
                    message_ids=[message_id],
                )
                if not claimed:
                    continue

                _claimed_id, claimed_fields = claimed[0]
                task_id = claimed_fields.get("task_id", task_id)

                recovery_key = f"recovery_count:{task_id}"
                current_count = int(await redis.get(recovery_key) or 0)

                if current_count >= MAX_RECOVERY_COUNT:
                    structured_log("error", "task_permanent_failure",
                                   task_id=task_id, recovery_count=current_count)
                    await redis.xack(STREAM_KEY, GROUP_NAME, message_id)
                    await redis.delete(recovery_key)
                    continue

                payload = await _rebuild_task_payload(task_id)
                if payload is None:
                    # No authoritative task row to rebuild from — re-enqueueing a
                    # stub would only fail again. Drop it and log, rather than
                    # loop until MAX_RECOVERY_COUNT.
                    structured_log("error", "recovery_task_row_missing", task_id=task_id)
                    await redis.xack(STREAM_KEY, GROUP_NAME, message_id)
                    await redis.delete(recovery_key)
                    continue

                new_count = await redis.incr(recovery_key)
                await redis.expire(recovery_key, 86400)

                payload["resume_from_phase"] = "latest"
                payload["recovery_generation"] = str(new_count)

                structured_log("warning", "recovered_pending_task",
                               task_id=task_id, recovery_count=new_count,
                               mode=payload.get("mode"))

                retry_msg_id = await redis.xadd(STREAM_KEY, payload)
                if retry_msg_id:
                    await redis.xack(STREAM_KEY, GROUP_NAME, message_id)
                else:
                    structured_log("error", "recovery_enqueue_failed", task_id=task_id)

        except Exception as e:
            structured_log("error", "recovery_daemon_error", exception=e)
        await asyncio.sleep(60)


async def _read_stream_fields(message_id) -> dict:
    """Read the original stream fields without claiming the message.

    Claiming a message resets ownership/idle time. We inspect the task_id first
    so a long-running task with a live worker is not stolen by recovery.
    """
    try:
        rows = await redis.xrange(STREAM_KEY, min=message_id, max=message_id, count=1)
    except Exception as exc:
        structured_log("warning", "recovery_read_stream_fields_failed", message_id=message_id, error=str(exc))
        return {}
    if not rows:
        return {}
    _mid, fields = rows[0]
    return dict(fields or {})


async def _recovery_decision(task_id: str) -> str:
    """Return whether a pending stream entry should be recovered now.

    Redis pending idle only proves a message has not been acked; it does not
    prove the worker died. If the DB says the task is running and at least one
    worker heartbeat is fresh, do not reclaim it. This conservative gate avoids
    duplicate side-effect execution during long shadow/real runs.
    """
    row = await database.fetch_one(
        "SELECT status FROM task_runs WHERE task_id = :task_id",
        {"task_id": task_id},
    )
    if not row:
        return "recover"
    status = str(row["status"] or "")
    if status in TERMINAL_TASK_STATUSES:
        return "ack_terminal_task"
    if status == "running" and await _has_recent_worker_heartbeat():
        return "skip_running_with_live_worker"
    return "recover"


async def _has_recent_worker_heartbeat() -> bool:
    row = await database.fetch_one(
        """SELECT COUNT(*) AS cnt
           FROM worker_heartbeats
           WHERE status = 'ok'
             AND last_seen_at >= (NOW() - INTERVAL :seconds SECOND)""",
        {"seconds": WORKER_HEARTBEAT_STALE_SECONDS},
    )
    return bool(row and int(row["cnt"] or 0) > 0)


async def _rebuild_task_payload(task_id: str) -> dict | None:
    """Rebuild the full dispatch message for ``task_id`` from the database.

    Mirrors the field set produced by ``dispatch_lottery`` so the worker has
    everything it needs (account, lottery, platform, urls, mode, selector
    config, action plan). Returns ``None`` if no task row exists.
    """
    row = await database.fetch_one(
        """SELECT tr.account_id, tr.lottery_id, tr.task_mode, tr.dry_run,
                  l.platform, l.raw_url, l.canonical_url, l.action_plan
           FROM task_runs tr
           JOIN lotteries l ON l.id = tr.lottery_id
           WHERE tr.task_id = :task_id""",
        {"task_id": task_id},
    )
    if not row:
        return None

    platform = row["platform"]
    # task_mode can be NULL on legacy rows; fall back to the dry_run flag.
    task_mode = row["task_mode"] or ("dry_run" if row["dry_run"] else "real_run")
    dry_run = task_mode != "real_run"

    selector_config = await load_runtime_selector_config()
    platform_selectors = selector_config.get(platform, {}) if isinstance(selector_config, dict) else {}

    # Reuse the canonical dispatch builder so a recovered message has exactly the
    # same field set the worker expects (no drift between dispatch and recovery).
    return build_lottery_task_message(
        task_id=task_id,
        account_id=row["account_id"],
        lottery_id=row["lottery_id"],
        platform=platform,
        raw_url=row["raw_url"],
        canonical_url=row["canonical_url"],
        task_mode=task_mode,
        dry_run=dry_run,
        platform_selectors=platform_selectors,
        action_plan=_parse_action_plan(row["action_plan"]),
    )


def _parse_action_plan(value):
    """Normalise an action_plan column value to a dict/list for the builder."""
    if value is None:
        return {}
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", "ignore")
    text = str(value).strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except Exception:
        return {}
