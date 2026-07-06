"""Recovery daemon: re-dispatch tasks whose stream message went stale.

A Redis pending entry only says a message has not been acknowledged; it does not
identify whether the owning worker is still making progress. Recovery therefore
uses the authoritative task row: terminal tasks are acked, running tasks are
skipped only while their owning worker heartbeat is fresh and their lease has not
expired, and all other stale messages are rebuilt from database state before
being re-enqueued.
"""

import asyncio
import json

from app.adapter_config import load_runtime_selector_config
from app.db import database, redis
from app.services.outbox import build_lottery_task_message
from app.services.real_run_gate import evaluate_real_run_decision
from app.utils.log import structured_log


STREAM_KEY = "lottery_tasks"
GROUP_NAME = "workers"
RECOVERY_CONSUMER = "recovery-daemon"
MAX_RECOVERY_COUNT = 3
IDLE_THRESHOLD_MS = 120_000
TERMINAL_TASK_STATUSES = {"succeeded", "failed"}


class RealRunRecoveryBlocked(Exception):
    """Raised when current real-run gates reject replaying a recovered task."""


def pending_idle_ms(entry: dict) -> int:
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
                if decision == "skip_owned_running_task":
                    structured_log("info", "recovery_skipped_owned_running_task", task_id=task_id, message_id=message_id, idle_ms=idle_ms)
                    continue
                if decision == "ack_terminal_task":
                    structured_log("info", "recovery_ack_terminal_task", task_id=task_id, message_id=message_id)
                    await redis.xack(STREAM_KEY, GROUP_NAME, message_id)
                    await redis.delete(f"recovery_count:{task_id}")
                    continue

                claimed = await redis.xclaim(
                    STREAM_KEY,
                    GROUP_NAME,
                    RECOVERY_CONSUMER,
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
                    structured_log("error", "task_permanent_failure", task_id=task_id, recovery_count=current_count)
                    await _mark_recovery_exhausted(task_id)
                    await redis.xack(STREAM_KEY, GROUP_NAME, message_id)
                    await redis.delete(recovery_key)
                    continue

                try:
                    payload = await _rebuild_task_payload(task_id)
                except RealRunRecoveryBlocked as exc:
                    structured_log("warning", "recovery_real_run_gate_blocked", task_id=task_id, reason=str(exc))
                    await redis.xack(STREAM_KEY, GROUP_NAME, message_id)
                    await redis.delete(recovery_key)
                    continue
                if payload is None:
                    structured_log("error", "recovery_task_row_missing", task_id=task_id)
                    await redis.xack(STREAM_KEY, GROUP_NAME, message_id)
                    await redis.delete(recovery_key)
                    continue

                new_count = await redis.incr(recovery_key)
                await redis.expire(recovery_key, 86400)

                payload["resume_from_phase"] = "latest"
                payload["recovery_generation"] = str(new_count)

                structured_log("warning", "recovered_pending_task", task_id=task_id, recovery_count=new_count, mode=payload.get("mode"))
                retry_msg_id = await redis.xadd(STREAM_KEY, payload)
                if retry_msg_id:
                    await redis.xack(STREAM_KEY, GROUP_NAME, message_id)
                else:
                    structured_log("error", "recovery_enqueue_failed", task_id=task_id)

        except Exception as e:
            structured_log("error", "recovery_daemon_error", exception=e)
        await asyncio.sleep(60)


async def _read_stream_fields(message_id) -> dict:
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
    row = await database.fetch_one(
        """SELECT status, worker_id,
                  CASE WHEN lease_expires_at IS NOT NULL AND lease_expires_at > NOW() THEN 1 ELSE 0 END AS lease_active
           FROM task_runs
           WHERE task_id = :task_id""",
        {"task_id": task_id},
    )
    if not row:
        return "recover"
    status = str(row["status"] or "")
    if status in TERMINAL_TASK_STATUSES:
        return "ack_terminal_task"
    worker_id = row["worker_id"]
    lease_active = int(row["lease_active"] or 0) == 1
    if status == "running" and worker_id and lease_active and await _worker_heartbeat_fresh(worker_id):
        return "skip_owned_running_task"
    return "recover"


async def _worker_heartbeat_fresh(worker_id: str) -> bool:
    row = await database.fetch_one(
        """SELECT COUNT(*) AS cnt
           FROM worker_heartbeats
           WHERE worker_id = :worker_id
             AND status = 'ok'
             AND last_seen_at >= (NOW() - INTERVAL 90 SECOND)""",
        {"worker_id": worker_id},
    )
    return bool(row and int(row["cnt"] or 0) > 0)


async def _mark_recovery_exhausted(task_id: str) -> None:
    try:
        row = await database.fetch_one(
            "SELECT account_id, lottery_id FROM task_runs WHERE task_id = :task_id",
            {"task_id": task_id},
        )
        if not row:
            return
        async with database.transaction():
            await database.execute(
                """UPDATE task_runs
                   SET status = 'failed', error_message = 'recovery exhausted', finished_at = NOW(),
                       worker_id = NULL, stream_message_id = NULL, lease_expires_at = NULL
                   WHERE task_id = :task_id AND status NOT IN ('succeeded', 'failed')""",
                {"task_id": task_id},
            )
            await database.execute(
                "UPDATE lotteries SET status = 'pending', execution_lock = NULL, locked_at = NULL WHERE id = :lottery_id AND execution_lock = :task_id",
                {"lottery_id": row["lottery_id"], "task_id": task_id},
            )
            await database.execute(
                "UPDATE accounts SET status = 'ready', updated_at = NOW() WHERE id = :account_id AND status = 'executing'",
                {"account_id": row["account_id"]},
            )
    except Exception as exc:
        structured_log("error", "recovery_exhausted_mark_failed_failed", task_id=task_id, error=str(exc))


async def _rebuild_task_payload(task_id: str) -> dict | None:
    row = await database.fetch_one(
        """SELECT tr.account_id, tr.lottery_id, tr.task_mode, tr.dry_run,
                  l.id, l.platform, l.raw_url, l.canonical_url, l.action_plan
           FROM task_runs tr
           JOIN lotteries l ON l.id = tr.lottery_id
           WHERE tr.task_id = :task_id""",
        {"task_id": task_id},
    )
    if not row:
        return None

    platform = row["platform"]
    task_mode = row["task_mode"] or ("dry_run" if row["dry_run"] else "real_run")
    dry_run = task_mode != "real_run"
    if task_mode == "real_run":
        decision = await evaluate_real_run_decision(row, account_id=row["account_id"], record=False)
        if not decision["allowed"]:
            blockers = ",".join(decision.get("failed_gates") or decision.get("blockers") or ["unknown"])
            raise RealRunRecoveryBlocked(blockers)

    selector_config = await load_runtime_selector_config()
    platform_selectors = selector_config.get(platform, {}) if isinstance(selector_config, dict) else {}

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
