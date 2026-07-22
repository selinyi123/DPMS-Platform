"""Recovery daemon: re-dispatch tasks whose stream message went stale.

A Redis pending entry only says a message has not been acknowledged; it does not
identify whether the owning worker is still making progress. Recovery therefore
uses the authoritative task row: terminal tasks are acked, running tasks are
skipped only while their owning worker heartbeat is fresh and their lease has not
expired, and all other stale messages are rebuilt from database state before
being re-enqueued.
"""

import asyncio
import ipaddress
import json

from app.action_plan import (
    ActionPlanV2Error,
    BILIBILI_API_EXECUTION_PATH,
    WEIBO_OAUTH_EXECUTION_PATH,
    WEIBO_RIP_ACTIONS,
    compute_bilibili_api_config_hash,
    compute_config_hash,
)
from app.db import database, redis
from app.services.outbox import LOTTERY_TASK_FIELDS
from app.services.real_run_gate import evaluate_real_run_decision
from app.utils.crypto import decrypt_weibo_rip, weibo_rip_hmac
from app.utils.log import structured_log


STREAM_KEY = "lottery_tasks"
GROUP_NAME = "workers"
RECOVERY_CONSUMER = "recovery-daemon"
MAX_RECOVERY_COUNT = 3
IDLE_THRESHOLD_MS = 120_000
TERMINAL_TASK_STATUSES = {"succeeded", "failed"}


class TaskRecoveryBlocked(Exception):
    """Raised when a recovered task cannot be replayed from authoritative state."""


class RealRunRecoveryBlocked(TaskRecoveryBlocked):
    """Raised specifically when current real-run gates reject replay."""


def pending_idle_ms(entry: dict) -> int:
    return int(entry.get("time_since_delivered") or 0)


async def _ack_converged_stream_message(message_id: str, fields: dict) -> None:
    """Remove legacy plaintext only after authoritative task convergence."""

    if "weibo_rip" in fields:
        # XACK only clears the PEL; it does not remove stream entry bytes.
        # Delete the exact legacy entry first so an XACK failure cannot leave
        # plaintext resident indefinitely. The authoritative task has already
        # converged at every call site below.
        await redis.xdel(STREAM_KEY, message_id)
    await redis.xack(STREAM_KEY, GROUP_NAME, message_id)


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
                    await _ack_converged_stream_message(message_id, fields)
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

                recovery_state = await _prepare_task_for_recovery(task_id)
                if recovery_state == "skip_owned_running_task":
                    structured_log("info", "recovery_claim_recheck_skipped", task_id=task_id, message_id=message_id)
                    continue
                if recovery_state == "ack_terminal_task":
                    await _ack_converged_stream_message(message_id, claimed_fields)
                    await redis.delete(f"recovery_count:{task_id}")
                    continue
                if recovery_state == "task_missing":
                    await _ack_converged_stream_message(message_id, claimed_fields)
                    await redis.delete(f"recovery_count:{task_id}")
                    continue
                if recovery_state == "real_run_reconciliation_required":
                    structured_log("error", "recovery_real_run_quarantined", task_id=task_id, message_id=message_id)
                    await _ack_converged_stream_message(message_id, claimed_fields)
                    await redis.delete(f"recovery_count:{task_id}")
                    continue

                recovery_key = f"recovery_count:{task_id}"
                current_count = int(await redis.get(recovery_key) or 0)
                if current_count >= MAX_RECOVERY_COUNT:
                    structured_log("error", "task_permanent_failure", task_id=task_id, recovery_count=current_count)
                    settled = await _mark_recovery_exhausted(task_id)
                    if settled:
                        await _ack_converged_stream_message(message_id, claimed_fields)
                        await redis.delete(recovery_key)
                    else:
                        structured_log(
                            "info",
                            "recovery_exhausted_cleanup_skipped",
                            task_id=task_id,
                            message_id=message_id,
                        )
                    continue

                try:
                    payload = await _rebuild_task_payload(task_id)
                except TaskRecoveryBlocked as exc:
                    structured_log("warning", "recovery_task_replay_blocked", task_id=task_id, reason=str(exc))
                    settled = await _mark_recovery_blocked(task_id, str(exc))
                    if settled:
                        await _ack_converged_stream_message(message_id, claimed_fields)
                        await redis.delete(recovery_key)
                    else:
                        structured_log(
                            "info",
                            "recovery_gate_block_cleanup_skipped",
                            task_id=task_id,
                            message_id=message_id,
                        )
                    continue
                if payload is None:
                    structured_log("error", "recovery_task_row_missing", task_id=task_id)
                    await _ack_converged_stream_message(message_id, claimed_fields)
                    await redis.delete(recovery_key)
                    continue

                new_count = await redis.incr(recovery_key)
                await redis.expire(recovery_key, 86400)

                payload["resume_from_phase"] = "latest"
                payload["recovery_generation"] = str(new_count)

                structured_log("warning", "recovered_pending_task", task_id=task_id, recovery_count=new_count, mode=payload.get("mode"))
                retry_msg_id = await redis.xadd(STREAM_KEY, payload)
                if retry_msg_id:
                    await _ack_converged_stream_message(message_id, claimed_fields)
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


async def _prepare_task_for_recovery(task_id: str) -> str:
    """Atomically revoke an expired owner before a replacement is enqueued."""
    async with database.transaction():
        row = await database.fetch_one(
            """SELECT task_id, account_id, lottery_id, status, worker_id, task_mode,
                      reconciliation_required,
                      CASE WHEN lease_expires_at IS NOT NULL AND lease_expires_at > NOW() THEN 1 ELSE 0 END AS lease_active
               FROM task_runs
               WHERE task_id = :task_id
               FOR UPDATE""",
            {"task_id": task_id},
        )
        if not row:
            return "task_missing"
        status = str(row["status"] or "").strip().lower()
        if status in TERMINAL_TASK_STATUSES:
            return "ack_terminal_task"
        if int(row["reconciliation_required"] or 0) != 0:
            return "real_run_reconciliation_required"
        if status == "running" and int(row["lease_active"] or 0) == 1:
            return "skip_owned_running_task"
        if status not in {"queued", "running"}:
            return "skip_owned_running_task"

        if status == "running":
            # Match the worker's lock order: task -> lottery -> account.
            lottery = await database.fetch_one(
                "SELECT id, platform FROM lotteries WHERE id = :lottery_id FOR UPDATE",
                {"lottery_id": row["lottery_id"]},
            )
            await database.fetch_one(
                "SELECT id FROM accounts WHERE id = :account_id FOR UPDATE",
                {"account_id": row["account_id"]},
            )
            task_mode = str(row["task_mode"] or "").strip().lower()
            if task_mode not in {"dry_run", "shadow_run"}:
                platform = str(lottery["platform"] if lottery else "").strip().lower()
                await database.execute(
                    """UPDATE task_runs
                       SET status = 'failed',
                           reconciliation_required = 1,
                           error_message = 'real-run lease expired; external outcome requires reconciliation',
                           finished_at = NOW(), lease_expires_at = NULL
                       WHERE task_id = :task_id AND status = 'running'""",
                    {"task_id": task_id},
                )
                # ``started`` means the durable intent was committed before the
                # network mutation began, but no definitive settlement was
                # recorded.  Convert it to the explicit unknown state so all
                # recovery and operator tooling can rely solely on structured
                # fields rather than parsing an error string.
                await database.execute(
                    """UPDATE external_action_intents
                       SET status = 'unknown', outcome = 'unknown',
                           effect_certainty = 'unknown',
                           started_at = COALESCE(started_at, created_at),
                           completed_at = COALESCE(completed_at, NOW()),
                           reconciliation_note = COALESCE(
                             NULLIF(TRIM(reconciliation_note), ''),
                             'worker lease expired before external action settlement'
                           )
                       WHERE task_id = :task_id AND status = 'started'""",
                    {"task_id": task_id},
                )
                # Retain the lottery's running state and execution lock. Without
                # a durable reconciliation state, releasing it would let an
                # operator reset the breaker and blindly replay unknown actions.
                await database.execute(
                    """UPDATE accounts SET status = 'cooling', updated_at = NOW(), version = version + 1
                       WHERE id = :account_id AND status = 'executing'""",
                    {"account_id": row["account_id"]},
                )
                if platform:
                    reason = f"{platform}_real_run_lease_expired_outcome_unknown"
                    await database.execute(
                        """INSERT INTO circuit_breakers (scope, status, reason, opened_at)
                           VALUES (:scope, 'open', :reason, NOW())
                           ON DUPLICATE KEY UPDATE status = 'open', reason = :reason,
                             opened_at = NOW(), updated_at = NOW()""",
                        {"scope": f"platform:{platform}", "reason": reason},
                    )
                    breaker = await database.fetch_one(
                        "SELECT status FROM circuit_breakers WHERE scope = :scope FOR UPDATE",
                        {"scope": f"platform:{platform}"},
                    )
                    if not breaker or str(breaker["status"] or "").strip().lower() != "open":
                        raise RuntimeError("recovery_platform_breaker_not_persisted")
                return "real_run_reconciliation_required"
            await database.execute(
                """UPDATE task_runs
                   SET status = 'queued', worker_id = NULL, stream_message_id = NULL,
                       lease_expires_at = NULL
                   WHERE task_id = :task_id AND status = 'running'""",
                {"task_id": task_id},
            )
            await database.execute(
                """UPDATE lotteries
                   SET status = 'claimed'
                   WHERE id = :lottery_id AND execution_lock = :task_id AND status = 'running'""",
                {"lottery_id": row["lottery_id"], "task_id": task_id},
            )
        elif row["worker_id"]:
            await database.execute(
                """UPDATE task_runs
                   SET worker_id = NULL, stream_message_id = NULL, lease_expires_at = NULL
                   WHERE task_id = :task_id AND status = 'queued'""",
                {"task_id": task_id},
            )
        return "recover"


async def _mark_recovery_blocked(task_id: str, reason: str) -> bool:
    """Settle a recovered task that current gates no longer authorize."""
    async with database.transaction():
        row = await database.fetch_one(
            """SELECT account_id, lottery_id, status, account_lease_id,
                      account_lease_generation
               FROM task_runs WHERE task_id = :task_id FOR UPDATE""",
            {"task_id": task_id},
        )
        if not row or str(row["status"] or "").strip().lower() != "queued":
            return False
        # Preserve the worker lock order: task -> lottery -> account.
        await database.fetch_one(
            "SELECT id FROM lotteries WHERE id = :lottery_id FOR UPDATE",
            {"lottery_id": row["lottery_id"]},
        )
        await database.fetch_one(
            "SELECT id FROM accounts WHERE id = :account_id FOR UPDATE",
            {"account_id": row["account_id"]},
        )
        await database.execute(
            """UPDATE task_runs
               SET status = 'failed', error_message = :reason, finished_at = NOW(),
                   worker_id = NULL, stream_message_id = NULL, lease_expires_at = NULL
               WHERE task_id = :task_id AND status = 'queued'""",
            {"task_id": task_id, "reason": f"recovery blocked: {reason}"[:480]},
        )
        await database.execute(
            """UPDATE lotteries SET status = 'pending', execution_lock = NULL, locked_at = NULL
               WHERE id = :lottery_id AND execution_lock = :task_id AND status = 'claimed'""",
            {"lottery_id": row["lottery_id"], "task_id": task_id},
        )
        await database.execute(
            """UPDATE account_operation_leases
               SET released_at = COALESCE(released_at, NOW())
               WHERE lease_id = :lease_id
                 AND account_id = :account_id
                 AND generation = :lease_generation
                 AND owner_id = :task_id""",
            {
                "lease_id": row["account_lease_id"],
                "account_id": row["account_id"],
                "lease_generation": row["account_lease_generation"],
                "task_id": task_id,
            },
        )
    return True


async def _mark_recovery_exhausted(task_id: str) -> bool:
    try:
        async with database.transaction():
            row = await database.fetch_one(
                """SELECT account_id, lottery_id, status, account_lease_id,
                          account_lease_generation
                   FROM task_runs WHERE task_id = :task_id FOR UPDATE""",
                {"task_id": task_id},
            )
            if not row or str(row["status"] or "").strip().lower() != "queued":
                return False
            await database.fetch_one(
                "SELECT id FROM lotteries WHERE id = :lottery_id FOR UPDATE",
                {"lottery_id": row["lottery_id"]},
            )
            await database.fetch_one(
                "SELECT id FROM accounts WHERE id = :account_id FOR UPDATE",
                {"account_id": row["account_id"]},
            )
            await database.execute(
                """UPDATE task_runs
                   SET status = 'failed', error_message = 'recovery exhausted', finished_at = NOW(),
                       worker_id = NULL, stream_message_id = NULL, lease_expires_at = NULL
                   WHERE task_id = :task_id AND status NOT IN ('succeeded', 'failed')""",
                {"task_id": task_id},
            )
            await database.execute(
                "UPDATE lotteries SET status = 'pending', execution_lock = NULL, locked_at = NULL WHERE id = :lottery_id AND execution_lock = :task_id AND status = 'claimed'",
                {"lottery_id": row["lottery_id"], "task_id": task_id},
            )
            await database.execute(
                """UPDATE account_operation_leases
                   SET released_at = COALESCE(released_at, NOW())
                   WHERE lease_id = :lease_id
                     AND account_id = :account_id
                     AND generation = :lease_generation
                     AND owner_id = :task_id""",
                {
                    "lease_id": row["account_lease_id"],
                    "account_id": row["account_id"],
                    "lease_generation": row["account_lease_generation"],
                    "task_id": task_id,
                },
            )
        return True
    except Exception as exc:
        structured_log("error", "recovery_exhausted_mark_failed_failed", task_id=task_id, error=str(exc))
        return False


async def _rebuild_task_payload(task_id: str) -> dict | None:
    row = await database.fetch_one(
        """SELECT tr.account_id, tr.lottery_id, tr.task_mode, tr.dry_run,
                  tr.rule_snapshot_id AS task_rule_snapshot_id,
                  tr.rule_hash AS task_rule_hash,
                  tr.action_plan_hash AS task_action_plan_hash,
                  tr.execution_evidence_id, tr.execution_path_id,
                  tr.target_hash, tr.config_hash,
                  tr.account_lease_id, tr.account_lease_generation,
                  tr.reconciliation_required,
                  a.execution_revision AS current_execution_revision,
                  l.id, l.platform, l.raw_url, l.canonical_url, l.rule_text,
                  l.action_plan, l.authoritative_rule_snapshot_id,
                  l.rule_hash, l.action_plan_hash
           FROM task_runs tr
           JOIN lotteries l ON l.id = tr.lottery_id
           JOIN accounts a ON a.id = tr.account_id
           WHERE tr.task_id = :task_id""",
        {"task_id": task_id},
    )
    if not row:
        return None

    platform = str(row["platform"] or "").strip().lower()
    task_mode = str(row["task_mode"] or ("dry_run" if row["dry_run"] else "real_run")).strip().lower()
    if task_mode not in {"dry_run", "shadow_run", "real_run"}:
        raise TaskRecoveryBlocked("task_mode_invalid")
    if int(row["reconciliation_required"] or 0) != 0:
        raise RealRunRecoveryBlocked("task_reconciliation_required")
    dry_run = task_mode != "real_run"
    if task_mode == "real_run":
        decision = await evaluate_real_run_decision(row, account_id=row["account_id"], record=False)
        # A policy/input mapping regression must never turn an authoritative
        # readiness blocker into a replay authorization.  Recovery is the last
        # Core boundary before a stale message is re-enqueued, so fail closed
        # on any raw blocker even if ``allowed`` is accidentally inconsistent.
        if not decision["allowed"] or decision.get("blockers"):
            blockers = ",".join(decision.get("failed_gates") or decision.get("blockers") or ["unknown"])
            raise RealRunRecoveryBlocked(blockers)

    execution_path_id = str(row["execution_path_id"] or "")
    current_execution_revision = int(row["current_execution_revision"] or 0)
    if execution_path_id == BILIBILI_API_EXECUTION_PATH:
        try:
            current_config_hash = compute_bilibili_api_config_hash(
                current_execution_revision
            )
        except ActionPlanV2Error as exc:
            raise TaskRecoveryBlocked(exc.code) from exc
        if str(row["config_hash"] or "") != current_config_hash:
            raise TaskRecoveryBlocked("account_execution_revision_changed")

    # The transactional outbox is the only existing immutable copy of the
    # exact dispatched payload. Rebuilding from mutable lottery/config rows can
    # turn a missing-action repair into a full replay or silently swap selector
    # versions while retaining the old policy decision.
    outbox = await database.fetch_one(
        """SELECT stream_key, payload
           FROM outbox_events
           WHERE dedup_key = :task_id
           LIMIT 1""",
        {"task_id": task_id},
    )
    if not outbox or str(outbox["stream_key"] or "").strip() != STREAM_KEY:
        raise TaskRecoveryBlocked("immutable_task_payload_missing")
    payload = _parse_outbox_payload(outbox["payload"])
    if "weibo_rip" in payload:
        raise TaskRecoveryBlocked("legacy_plaintext_weibo_rip_forbidden")
    if execution_path_id == WEIBO_OAUTH_EXECUTION_PATH:
        parsed_plan = _parse_json_value(payload.get("action_plan"))
        if not isinstance(parsed_plan, dict):
            raise TaskRecoveryBlocked("immutable_task_action_plan_invalid")
        weibo_rip_encrypted = str(payload.get("weibo_rip_encrypted") or "")
        required_actions = set(parsed_plan.get("required_actions") or [])
        rip_required = bool(
            task_mode == "real_run"
            and required_actions.intersection(WEIBO_RIP_ACTIONS)
        )
        if rip_required:
            try:
                weibo_rip = decrypt_weibo_rip(weibo_rip_encrypted)
                parsed_rip = ipaddress.ip_address(weibo_rip)
            except (TypeError, ValueError) as exc:
                raise TaskRecoveryBlocked("weibo_public_rip_required") from exc
            if not parsed_rip.is_global or parsed_rip.compressed != weibo_rip:
                raise TaskRecoveryBlocked("weibo_public_rip_required")
        else:
            if weibo_rip_encrypted:
                raise TaskRecoveryBlocked("weibo_rip_not_applicable")
            weibo_rip = ""
        current_config_hash = compute_config_hash(
            {
                "execution_path_id": WEIBO_OAUTH_EXECUTION_PATH,
                "execution_revision": current_execution_revision,
                "runtime_capability_requirements": dict(
                    parsed_plan.get("runtime_capability_requirements") or {}
                ),
                "weibo_rip_hash": weibo_rip_hmac(weibo_rip),
            }
        )
        if str(row["config_hash"] or "") != current_config_hash:
            raise TaskRecoveryBlocked("account_execution_revision_changed")

    expected = {
        "task_id": str(task_id),
        "account_id": str(row["account_id"]),
        "lottery_id": str(row["lottery_id"]),
        "platform": platform,
        "raw_url": str(row["raw_url"] or ""),
        "canonical_url": str(row["canonical_url"] or ""),
        "dry_run": "1" if dry_run else "0",
        "mode": task_mode,
        "rule_snapshot_id": str(row["task_rule_snapshot_id"] or ""),
        "rule_hash": str(row["task_rule_hash"] or ""),
        "action_plan_hash": str(row["task_action_plan_hash"] or ""),
        "execution_evidence_id": str(row["execution_evidence_id"] or ""),
        "execution_path_id": execution_path_id,
        "target_hash": str(row["target_hash"] or ""),
        "config_hash": str(row["config_hash"] or ""),
        "execution_revision": (
            str(current_execution_revision)
            if execution_path_id
            in {BILIBILI_API_EXECUTION_PATH, WEIBO_OAUTH_EXECUTION_PATH}
            else ""
        ),
        "account_lease_id": str(row["account_lease_id"] or ""),
        "account_lease_generation": str(row["account_lease_generation"] or ""),
    }
    if any(str(payload.get(key, "")) != value for key, value in expected.items()):
        raise TaskRecoveryBlocked("immutable_task_payload_binding_mismatch")
    # Pre-v2 non-API tasks did not carry a credential-generation field.  An
    # empty value is backwards compatible for those paths; Bilibili API v2 has
    # a non-empty expected revision above and therefore still fails closed.
    if "execution_revision" not in payload and not expected["execution_revision"]:
        payload["execution_revision"] = ""
    if "weibo_rip_encrypted" not in payload:
        if execution_path_id == WEIBO_OAUTH_EXECUTION_PATH:
            raise TaskRecoveryBlocked("immutable_task_payload_incomplete")
        payload["weibo_rip_encrypted"] = ""
    if any(field not in payload for field in LOTTERY_TASK_FIELDS):
        raise TaskRecoveryBlocked("immutable_task_payload_incomplete")
    for field in ("selector_config", "action_plan"):
        parsed = _parse_json_value(payload.get(field))
        if not isinstance(parsed, dict):
            raise TaskRecoveryBlocked(f"immutable_task_{field}_invalid")
    return {field: str(payload[field]) for field in LOTTERY_TASK_FIELDS}


def _parse_json_value(value):
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
        return None


def _parse_outbox_payload(value) -> dict:
    parsed = _parse_json_value(value)
    if not isinstance(parsed, dict):
        raise TaskRecoveryBlocked("immutable_task_payload_invalid")
    return parsed
