"""Recovery daemon: re-dispatch tasks whose stream message went stale.

A Redis pending entry only says a message has not been acknowledged; it does not
identify whether the owning worker is still making progress. Recovery therefore
uses the authoritative task row: terminal tasks are acked, running tasks are
skipped only while their owning worker heartbeat is fresh and their lease has not
expired, and all other stale messages are rebuilt from database state before
being re-enqueued.
"""

import asyncio
import hashlib
import ipaddress
import json
import re

from app.action_plan import (
    ActionPlanV2Error,
    BILIBILI_API_EXECUTION_PATH,
    WEIBO_OAUTH_EXECUTION_PATH,
    WEIBO_RIP_ACTIONS,
    canonical_json_bytes,
    compute_bilibili_api_config_hash,
    compute_config_hash,
)
from app.config import settings
from app.db import database, redis
from app.services.execution_intents import (
    ExecutionIntentError,
    TaskExecutionIntentBinding,
    build_task_execution_intent_binding,
    coerce_frozen_execution_intent,
)
from app.services.outbox import LOTTERY_TASK_FIELDS
from app.services.real_run_gate import evaluate_real_run_decision
from app.task_streams import (
    LEGACY_TASK_GROUP_NAME,
    LEGACY_TASK_FANOUT_CONSUMER_NAME,
    LEGACY_TASK_STREAM_BINDING,
    LEGACY_TASK_STREAM_KEY,
    SAFE_FANOUT_RECOVERY_REENQUEUE_LUA,
    SAFE_TERMINAL_FANOUT_ACK_DELETE_LUA,
    SAFE_TERMINAL_TASK_ACK_DELETE_LUA,
    TaskStreamBinding,
    task_stream_binding_for_key,
    task_stream_binding_for_platform,
    task_stream_bindings,
    validate_task_stream_message,
)
from app.utils.crypto import decrypt_weibo_rip, weibo_rip_hmac
from app.utils.log import structured_log
from shared.platform_scope import normalize_platform_scope
from shared.execution_contracts import (
    LEGACY_FULL_EXECUTION_INTENT_KIND,
    lease_operation_kind_for_execution_intent,
)
from shared.redis_consumer_groups import verify_redis_consumer_group


# Compatibility aliases retained for existing callers and legacy-drain tests.
STREAM_KEY = LEGACY_TASK_STREAM_KEY
GROUP_NAME = LEGACY_TASK_GROUP_NAME
RECOVERY_CONSUMER = "recovery-daemon"
MAX_RECOVERY_COUNT = 3
IDLE_THRESHOLD_MS = 120_000
RECOVERY_POLL_SECONDS = 60
TERMINAL_TASK_STATUSES = {"succeeded", "failed"}
SAFE_REPLAY_TASK_MODES = frozenset({"dry_run", "shadow_run"})
STALE_RUNNING_SCAN_LIMIT = 32
STALE_RUNNING_SCAN_SECONDS = 30
LEGACY_FANOUT_READ_COUNT = 50
LEGACY_FANOUT_BLOCK_MS = 1000
LEGACY_FANOUT_ENTRY_TIMEOUT_SECONDS = 30
LEGACY_SOURCE_STREAM_FIELD = "legacy_source_stream"
LEGACY_SOURCE_MESSAGE_ID_FIELD = "legacy_source_message_id"
EXECUTION_INTENT_MESSAGE_FIELDS = (
    "execution_intent_id",
    "execution_intent_hash",
    "execution_intent_kind",
    "execution_intent_binding_hash",
    "requested_actions",
    "requested_actions_hash",
    "requested_action_plan_hash",
    "execution_evidence_kind",
    "exact_execution_evidence_id",
    "oauth_calibration_id",
)
LEGACY_EXECUTION_INTENT_DEFAULTS = {
    "execution_intent_id": "",
    "execution_intent_hash": "",
    "execution_intent_kind": "",
    "execution_intent_binding_hash": "",
    "requested_actions": "[]",
    "requested_actions_hash": "",
    "requested_action_plan_hash": "",
    "execution_evidence_kind": "",
    "exact_execution_evidence_id": "",
    "oauth_calibration_id": "",
}
_REDIS_STREAM_ID_RE = re.compile(r"^[0-9]+-[0-9]+$")

# The immutable DB outbox is validated before this script is called. Redis then
# records the dedup marker, appends the exact payload to its platform stream,
# and acknowledges the legacy group entry as one atomic operation. A retry
# observes the marker and only repeats the XACK.
LEGACY_FANOUT_LUA = """
local existing = redis.call('SMEMBERS', KEYS[3])
if #existing > 0 then
  for _, member in ipairs(existing) do
    if string.sub(member, 1, string.len(ARGV[3])) == ARGV[3] then
      redis.call('XACK', KEYS[1], ARGV[1], ARGV[2])
      return string.sub(member, string.len(ARGV[3]) + 1)
    end
  end
  return redis.error_reply('legacy fanout marker binding mismatch')
end
local fields = {}
for index = 4, #ARGV do
  fields[#fields + 1] = ARGV[index]
end
local target_id = redis.call('XADD', KEYS[2], '*', unpack(fields))
redis.call('SADD', KEYS[3], ARGV[3] .. target_id)
redis.call('XACK', KEYS[1], ARGV[1], ARGV[2])
return target_id
"""

class TaskRecoveryBlocked(Exception):
    """Raised when a recovered task cannot be replayed from authoritative state."""


class RealRunRecoveryBlocked(TaskRecoveryBlocked):
    """Raised specifically when current real-run gates reject replay."""


def pending_idle_ms(entry: dict) -> int:
    return int(entry.get("time_since_delivered") or 0)


async def _ack_converged_stream_message(
    message_id: str,
    fields: dict,
    *,
    stream_key: str = STREAM_KEY,
    group_name: str = GROUP_NAME,
) -> None:
    """Remove legacy plaintext only after authoritative task convergence."""

    if "weibo_rip" in fields:
        # XACK only clears the PEL; it does not remove stream entry bytes.
        # Delete the exact legacy entry first so an XACK failure cannot leave
        # plaintext resident indefinitely. The authoritative task has already
        # converged at every call site below.
        await redis.xdel(stream_key, message_id)
    binding = task_stream_binding_for_key(stream_key)
    if (
        binding is not None
        and _has_valid_legacy_fanout_source(binding, fields)
    ):
        task_id = str(fields.get("task_id") or "").strip()
        terminal = await database.fetch_one(
            "SELECT status FROM task_runs WHERE task_id = :task_id",
            {"task_id": task_id},
        )
        if (
            terminal
            and str(terminal["status"] or "").strip().casefold()
            in TERMINAL_TASK_STATUSES
        ):
            source_message_id = str(
                fields.get(LEGACY_SOURCE_MESSAGE_ID_FIELD) or ""
            ).strip()
            await redis.eval(
                SAFE_TERMINAL_FANOUT_ACK_DELETE_LUA,
                2,
                stream_key,
                _legacy_fanout_marker_key(source_message_id),
                group_name,
                str(message_id),
                _legacy_fanout_marker_member(
                    stream_key,
                    str(message_id),
                    task_id,
                ),
            )
            return
    if stream_key == LEGACY_TASK_STREAM_KEY:
        # The compatibility stream is retained as the source provenance for
        # fan-out/recovery.  Only isolated platform lanes use bounded
        # terminal-entry retention.
        await redis.xack(stream_key, group_name, str(message_id))
    else:
        await redis.eval(
            SAFE_TERMINAL_TASK_ACK_DELETE_LUA,
            1,
            stream_key,
            group_name,
            str(message_id),
        )


async def start_recovery_daemon(
    *,
    platforms=None,
    include_shared: bool = True,
    fail_closed: bool = False,
):
    """Run independent Redis-PEL and authoritative DB recovery loops."""

    if platforms is None:
        selected_platforms = frozenset(
            normalize_platform_scope("all")
        )
    else:
        platform_values = (
            (platforms,) if isinstance(platforms, str) else tuple(platforms)
        )
        selected_platforms = (
            frozenset(normalize_platform_scope(platform_values))
            if platform_values
            else frozenset()
        )
    bindings = tuple(
        binding
        for binding in task_stream_bindings(
            include_legacy=(
                include_shared
                and settings.legacy_task_stream_drain_enabled
            )
        )
        if (
            binding.platform in selected_platforms
            or (include_shared and binding.platform is None)
        )
    )
    tasks = [
        asyncio.create_task(
            _recovery_stream_loop(
                binding,
                fail_closed=fail_closed,
            ),
            name=f"task-recovery:{binding.stream_key}",
        )
        for binding in bindings
    ]
    # Redis is only a delivery substrate.  A FLUSHDB removes the PEL entry that
    # used to drive recovery, so every platform also owns one DB-authoritative
    # sweep.  Deduplicate standard/repair bindings while retaining platform
    # isolation: one broken platform scan cannot stop another platform's loop.
    recovery_platforms = tuple(
        dict.fromkeys(
            binding.platform
            for binding in bindings
            if binding.platform is not None
        )
    )
    tasks.extend(
        asyncio.create_task(
            _stale_running_recovery_loop(
                platform,
                fail_closed=fail_closed,
            ),
            name=f"task-db-recovery:{platform}",
        )
        for platform in recovery_platforms
    )
    if include_shared and settings.legacy_task_stream_drain_enabled:
        tasks.append(
            asyncio.create_task(
                _legacy_fanout_loop(),
                name="task-recovery-legacy-fanout",
            )
        )
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _stale_running_recovery_loop(
    platform: str,
    *,
    fail_closed: bool = False,
) -> None:
    """Continuously close expired DB owners for one platform."""

    while True:
        try:
            summary = await _recover_stale_running_tasks_for_platform(platform)
            if fail_closed and summary["errors"]:
                raise RuntimeError(
                    "isolated_stale_running_recovery_incomplete"
                )
            if summary["examined"]:
                structured_log(
                    "warning",
                    "stale_running_task_database_recovery",
                    platform=platform,
                    **summary,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            structured_log(
                "error",
                "stale_running_task_database_recovery_error",
                platform=platform,
                exception=exc,
            )
            if fail_closed:
                raise
        await asyncio.sleep(STALE_RUNNING_SCAN_SECONDS)


async def _recover_stale_running_tasks_for_platform(
    platform: str,
    *,
    limit: int = STALE_RUNNING_SCAN_LIMIT,
) -> dict[str, int]:
    """Recover at most ``limit`` expired running tasks for one platform.

    The platform/status lottery index selects only this business lane before
    joining its active task rows. Rows are processed sequentially inside a
    platform lane so four concurrent sweeps do not multiply into an unbounded
    database connection burst.
    """

    normalized_platform = str(platform or "").strip().casefold()
    normalized_limit = int(limit)
    if not normalized_platform:
        raise ValueError("stale_running_recovery_platform_required")
    if normalized_limit <= 0 or normalized_limit > STALE_RUNNING_SCAN_LIMIT:
        raise ValueError("stale_running_recovery_limit_invalid")

    candidates = await database.fetch_all(
        """SELECT tr.task_id
           FROM lotteries AS l
             FORCE INDEX (idx_lottery_platform_recovery)
           STRAIGHT_JOIN task_runs AS tr
             FORCE INDEX (idx_task_run_lottery_stale)
             ON tr.lottery_id = l.id
           WHERE l.platform = :platform
             AND l.status = 'running'
             AND tr.status = 'running'
             AND (
               tr.lease_expires_at IS NULL
               OR tr.lease_expires_at <= NOW()
             )
           ORDER BY tr.lease_expires_at, tr.task_id
           LIMIT :limit""",
        {"platform": normalized_platform, "limit": normalized_limit},
    )
    summary = {
        "examined": 0,
        "requeued_safe": 0,
        "quarantined_real": 0,
        "failed_safe": 0,
        "skipped_race": 0,
        "errors": 0,
    }
    for candidate in candidates or []:
        task_id = str(candidate["task_id"] or "").strip()
        if not task_id:
            summary["errors"] += 1
            continue
        summary["examined"] += 1
        try:
            outcome = await _recover_stale_running_task_from_database(
                task_id,
                expected_platform=normalized_platform,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            summary["errors"] += 1
            structured_log(
                "error",
                "stale_running_task_database_recovery_row_error",
                platform=normalized_platform,
                task_id=task_id,
                exception=exc,
            )
            continue
        if outcome == "requeued_safe":
            summary["requeued_safe"] += 1
        elif outcome == "real_run_reconciliation_required":
            summary["quarantined_real"] += 1
        elif outcome == "safe_mode_recovery_failed":
            summary["failed_safe"] += 1
        else:
            summary["skipped_race"] += 1
    return summary


async def _recovery_stream_loop(
    binding: TaskStreamBinding,
    *,
    fail_closed: bool = False,
) -> None:
    group_ready = False
    while True:
        try:
            if not group_ready:
                await _ensure_task_stream_group(binding)
                group_ready = True
            await _recover_stream_once(binding)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # NOGROUP, Redis restart, and transient connectivity failures all
            # retry only this lane. The next iteration recreates the exact
            # stream/group before touching its PEL.
            group_ready = False
            structured_log(
                "error",
                "recovery_daemon_error",
                stream=binding.stream_key,
                platform=binding.platform or "legacy",
                exception=e,
            )
            if fail_closed:
                raise
        await asyncio.sleep(RECOVERY_POLL_SECONDS)


async def _legacy_fanout_loop() -> None:
    """Move never-delivered legacy entries into their isolated platform lane."""

    group_ready = False
    while True:
        try:
            if not group_ready:
                await _ensure_task_stream_group(
                    LEGACY_TASK_STREAM_BINDING
                )
                group_ready = True
            messages = await redis.xreadgroup(
                LEGACY_TASK_GROUP_NAME,
                LEGACY_TASK_FANOUT_CONSUMER_NAME,
                {LEGACY_TASK_STREAM_KEY: ">"},
                count=LEGACY_FANOUT_READ_COUNT,
                block=LEGACY_FANOUT_BLOCK_MS,
            )
            for stream_name, entries in messages or []:
                if str(stream_name) != LEGACY_TASK_STREAM_KEY:
                    structured_log(
                        "error",
                        "legacy_task_fanout_stream_mismatch",
                        actual_stream=str(stream_name),
                    )
                    continue
                # All entries were already moved to the PEL by XREADGROUP.
                # Validate/transfer the batch concurrently so one malformed or
                # slow row cannot head-of-line block another platform.
                await asyncio.gather(
                    *(
                        _process_legacy_fanout_entry(
                            str(message_id),
                            dict(fields or {}),
                        )
                        for message_id, fields in entries
                    )
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            group_ready = False
            structured_log(
                "error",
                "legacy_task_fanout_loop_error",
                exception=exc,
            )
            await asyncio.sleep(5)


async def _ensure_task_stream_group(
    binding: TaskStreamBinding,
) -> None:
    """Fail closed unless bootstrap established the exact fixed group."""

    await verify_redis_consumer_group(
        redis,
        stream_key=binding.stream_key,
        group_name=binding.group_name,
    )


async def _process_legacy_fanout_entry(
    message_id: str,
    fields: dict,
) -> None:
    try:
        await asyncio.wait_for(
            _process_legacy_claimed_message(message_id, fields),
            timeout=LEGACY_FANOUT_ENTRY_TIMEOUT_SECONDS,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # The entry remains in the PEL. If cancellation races the atomic Redis
        # transfer, its marker makes the stale-recovery retry idempotent.
        structured_log(
            "error",
            "legacy_task_fanout_failed",
            message_id=message_id,
            exception=exc,
        )


async def _process_legacy_claimed_message(
    message_id: str,
    fields: dict,
) -> bool:
    """Converge or atomically fan out one entry already in the legacy PEL."""

    task_id = str(fields.get("task_id") or message_id)
    decision = await _recovery_decision(task_id)
    if decision == "skip_owned_running_task":
        structured_log(
            "info",
            "legacy_task_fanout_skipped_owned_running_task",
            task_id=task_id,
            message_id=message_id,
        )
        return False
    if decision == "ack_terminal_task":
        await _ack_converged_stream_message(message_id, fields)
        await redis.delete(f"recovery_count:{task_id}")
        return True

    authority = await _recovery_stream_authority(
        task_id,
        LEGACY_TASK_STREAM_BINDING,
        fields,
    )
    if authority == "foreign":
        await _ack_converged_stream_message(message_id, fields)
        return True
    if authority != "exact":
        structured_log(
            "error",
            "legacy_task_fanout_authority_unverified",
            task_id=task_id,
            message_id=message_id,
        )
        return False

    try:
        target_message_id = await _fanout_legacy_claimed_message(
            message_id,
            fields,
        )
    except TaskRecoveryBlocked as exc:
        structured_log(
            "error",
            "legacy_task_fanout_blocked",
            task_id=task_id,
            message_id=message_id,
            reason=str(exc),
        )
        return False
    await redis.delete(f"recovery_count:{task_id}")
    structured_log(
        "info",
        "legacy_task_fanout_completed",
        task_id=task_id,
        legacy_message_id=message_id,
        target_message_id=target_message_id,
        platform=str(fields.get("platform") or "").strip().casefold(),
    )
    return True


async def _fanout_legacy_claimed_message(
    message_id: str,
    claimed_fields: dict,
) -> str:
    """Atomically append an authoritative legacy payload and ACK its source."""

    task_id = str(claimed_fields.get("task_id") or "").strip()
    outbox = await database.fetch_one(
        """SELECT stream_key, payload
           FROM outbox_events
           WHERE dedup_key = :task_id
           LIMIT 1""",
        {"task_id": task_id},
    )
    if (
        not outbox
        or str(outbox["stream_key"] or "").strip()
        != LEGACY_TASK_STREAM_KEY
    ):
        raise TaskRecoveryBlocked("legacy_fanout_immutable_payload_missing")
    payload = _with_legacy_execution_intent_defaults(
        _parse_outbox_payload(outbox["payload"])
    )
    try:
        validate_task_stream_message(
            LEGACY_TASK_STREAM_BINDING,
            payload,
        )
    except ValueError as exc:
        if str(exc).startswith("task_stream_platform_unsupported:"):
            raise TaskRecoveryBlocked(
                "legacy_fanout_platform_unsupported"
            ) from exc
        raise TaskRecoveryBlocked(str(exc)) from exc
    if "weibo_rip" in payload:
        raise TaskRecoveryBlocked("legacy_plaintext_weibo_rip_forbidden")
    if str(payload.get("task_id") or "").strip() != task_id:
        raise TaskRecoveryBlocked("legacy_fanout_task_binding_mismatch")

    normalized_claimed_fields = _with_legacy_execution_intent_defaults(
        claimed_fields
    )
    non_platform_fields = tuple(
        field
        for field in LOTTERY_TASK_FIELDS
        if field != "platform"
    )
    if any(field not in payload for field in non_platform_fields):
        raise TaskRecoveryBlocked("legacy_fanout_payload_incomplete")
    if any(
        str(payload.get(field, ""))
        != str(normalized_claimed_fields.get(field, ""))
        for field in non_platform_fields
    ):
        raise TaskRecoveryBlocked("legacy_fanout_payload_binding_mismatch")
    if (
        str(payload.get("platform") or "").strip()
        and str(payload.get("platform") or "").strip().casefold()
        != str(
            normalized_claimed_fields.get("platform") or ""
        ).strip().casefold()
    ):
        raise TaskRecoveryBlocked("legacy_fanout_payload_binding_mismatch")
    task = await database.fetch_one(
        """SELECT tr.task_id, tr.account_id, tr.lottery_id,
                  tr.task_mode, tr.status,
                  l.platform AS lottery_platform,
                  a.platform AS account_platform
           FROM task_runs tr
           JOIN lotteries l ON l.id = tr.lottery_id
           JOIN accounts a ON a.id = tr.account_id
           WHERE tr.task_id = :task_id""",
        {"task_id": task_id},
    )
    task_data = dict(task) if task else {}
    if (
        not task
        or str(task_data.get("task_id") or "").strip() != task_id
        or str(task_data.get("account_id") or "").strip()
        != str(payload.get("account_id") or "").strip()
        or str(task_data.get("lottery_id") or "").strip()
        != str(payload.get("lottery_id") or "").strip()
        or str(task_data.get("task_mode") or "").strip().casefold()
        != str(payload.get("mode") or "").strip().casefold()
        or str(task_data.get("status") or "").strip().casefold()
        not in {"queued", "running", "succeeded", "failed"}
    ):
        raise TaskRecoveryBlocked("legacy_fanout_task_authority_mismatch")

    platform = str(payload.get("platform") or "").strip().casefold()
    lottery_platform = str(
        task_data.get("lottery_platform") or ""
    ).strip().casefold()
    account_platform = str(
        task_data.get("account_platform") or ""
    ).strip().casefold()
    if not platform:
        # The historical pre-platform envelope existed only for Bilibili.
        # Recover it from both locked DB authorities; do not generalize a
        # malformed modern platform-less message to another module.
        if (
            str(normalized_claimed_fields.get("platform") or "").strip()
            or lottery_platform != "bilibili"
            or account_platform != "bilibili"
        ):
            raise TaskRecoveryBlocked(
                "legacy_fanout_platform_authority_missing"
            )
        platform = "bilibili"
        payload["platform"] = platform
        normalized_claimed_fields["platform"] = platform
    if lottery_platform != platform or account_platform != platform:
        raise TaskRecoveryBlocked("legacy_fanout_platform_authority_mismatch")
    try:
        target_binding = task_stream_binding_for_platform(platform)
    except ValueError as exc:
        raise TaskRecoveryBlocked("legacy_fanout_platform_unsupported") from exc
    try:
        validate_task_stream_message(target_binding, payload)
    except ValueError as exc:
        raise TaskRecoveryBlocked(str(exc)) from exc
    if any(field not in payload for field in LOTTERY_TASK_FIELDS):
        raise TaskRecoveryBlocked("legacy_fanout_payload_incomplete")
    if any(
        str(payload.get(field, ""))
        != str(normalized_claimed_fields.get(field, ""))
        for field in LOTTERY_TASK_FIELDS
    ):
        raise TaskRecoveryBlocked("legacy_fanout_payload_binding_mismatch")
    await _validate_recovery_execution_intent_authority(payload)

    forwarded = {
        field: str(payload[field])
        for field in LOTTERY_TASK_FIELDS
    }
    forwarded[LEGACY_SOURCE_STREAM_FIELD] = LEGACY_TASK_STREAM_KEY
    forwarded[LEGACY_SOURCE_MESSAGE_ID_FIELD] = str(message_id)
    marker_key = _legacy_fanout_marker_key(str(message_id))
    field_args = [
        value
        for field, field_value in forwarded.items()
        for value in (field, field_value)
    ]
    target_message_id = await redis.eval(
        LEGACY_FANOUT_LUA,
        3,
        LEGACY_TASK_STREAM_KEY,
        target_binding.stream_key,
        marker_key,
        LEGACY_TASK_GROUP_NAME,
        str(message_id),
        _legacy_fanout_marker_prefix(
            target_binding.stream_key,
            task_id,
        ),
        *field_args,
    )
    if not target_message_id:
        raise RuntimeError("legacy_task_fanout_atomic_transfer_failed")
    return str(target_message_id)


def _legacy_fanout_marker_key(source_message_id: str) -> str:
    marker_digest = hashlib.sha256(
        (
            f"{LEGACY_TASK_STREAM_KEY}:{str(source_message_id)}"
        ).encode("utf-8")
    ).hexdigest()
    return f"legacy_task_fanout:{marker_digest}"


def _legacy_fanout_marker_prefix(
    stream_key: str,
    task_id: str,
) -> str:
    return f"{str(stream_key)}|{str(task_id).strip()}|"


def _legacy_fanout_marker_member(
    stream_key: str,
    message_id: str,
    task_id: str,
) -> str:
    return (
        _legacy_fanout_marker_prefix(stream_key, task_id)
        + str(message_id)
    )


def _with_legacy_execution_intent_defaults(payload: dict) -> dict:
    """Fill only fields introduced by the execution-intent migration."""

    normalized = dict(payload)
    for field, default in LEGACY_EXECUTION_INTENT_DEFAULTS.items():
        normalized.setdefault(field, default)
    return normalized


def _execution_intent_message_is_empty(payload: dict) -> bool:
    if any(field not in payload for field in EXECUTION_INTENT_MESSAGE_FIELDS):
        return False
    scalar_fields = (
        "execution_intent_id",
        "execution_intent_hash",
        "execution_intent_kind",
        "execution_intent_binding_hash",
        "requested_actions_hash",
        "requested_action_plan_hash",
        "execution_evidence_kind",
        "exact_execution_evidence_id",
        "oauth_calibration_id",
    )
    if any(str(payload.get(field) or "").strip() for field in scalar_fields):
        return False
    return _parse_json_value(payload.get("requested_actions")) == []


async def _validate_recovery_execution_intent_authority(
    payload: dict,
) -> TaskExecutionIntentBinding | None:
    """Rebuild a real-run intent binding from DB before replay or fan-out.

    The outbox proves what Core originally emitted, but it cannot prove that
    the payload was correctly bound to the frozen intent tables. Recovery
    independently rebuilds the hash contract. Only pre-contract real-run tasks
    with no root/binding rows and explicit empty intent fields retain the
    legacy-full compatibility path.
    """

    task_mode = str(payload.get("mode") or "").strip().casefold()
    if task_mode != "real_run":
        if not _execution_intent_message_is_empty(payload):
            raise TaskRecoveryBlocked(
                "execution_intent_not_applicable_to_non_real_task"
            )
        return None

    task_id = str(payload.get("task_id") or "").strip()
    context = await database.fetch_one(
        """SELECT tr.task_id AS bound_task_id,
                  tr.lottery_id AS bound_lottery_id,
                  tr.account_id AS bound_account_id,
                  tr.task_mode AS bound_task_mode,
                  root.contract_version AS root_contract_version,
                  root.intent_id AS root_intent_id,
                  root.intent_hash AS root_intent_hash,
                  root.lottery_id AS root_lottery_id,
                  root.source_task_id AS root_source_task_id,
                  root.source_account_id AS root_source_account_id,
                  root.platform AS root_platform,
                  root.raw_url AS root_raw_url,
                  root.canonical_url AS root_canonical_url,
                  root.full_action_plan AS root_full_action_plan,
                  root.full_action_plan_hash AS root_full_action_plan_hash,
                  root.full_required_actions AS root_full_required_actions,
                  root.full_required_actions_hash AS root_full_required_actions_hash,
                  root.rule_snapshot_id AS root_rule_snapshot_id,
                  root.rule_hash AS root_rule_hash,
                  root.execution_path_id AS root_execution_path_id,
                  root.target_hash AS root_target_hash,
                  binding.contract_version AS binding_contract_version,
                  binding.task_id AS binding_task_id,
                  binding.intent_id AS binding_intent_id,
                  binding.lottery_id AS binding_lottery_id,
                  binding.account_id AS binding_account_id,
                  binding.binding_kind AS binding_kind,
                  binding.requested_actions AS binding_requested_actions,
                  binding.requested_actions_hash AS binding_requested_actions_hash,
                  binding.bound_action_plan AS binding_bound_action_plan,
                  binding.bound_action_plan_hash AS binding_bound_action_plan_hash,
                  binding.evidence_action_plan_hash AS binding_evidence_action_plan_hash,
                  binding.rule_snapshot_id AS binding_rule_snapshot_id,
                  binding.rule_hash AS binding_rule_hash,
                  binding.execution_evidence_id AS binding_execution_evidence_id,
                  binding.execution_evidence_kind AS binding_execution_evidence_kind,
                  binding.exact_execution_evidence_id AS binding_exact_execution_evidence_id,
                  binding.oauth_calibration_id AS binding_oauth_calibration_id,
                  binding.execution_path_id AS binding_execution_path_id,
                  binding.target_hash AS binding_target_hash,
                  binding.config_hash AS binding_config_hash,
                  binding.execution_revision AS binding_execution_revision,
                  binding.account_lease_id AS binding_account_lease_id,
                  binding.account_lease_generation AS binding_account_lease_generation,
                  binding.binding_hash AS binding_hash
           FROM task_runs tr
           LEFT JOIN task_execution_intent_bindings binding
             ON binding.task_id = tr.task_id
           LEFT JOIN lottery_execution_intents root
             ON root.lottery_id = binding.lottery_id
            AND root.intent_id = binding.intent_id
           WHERE tr.task_id = :task_id""",
        {"task_id": task_id},
    )
    if not context:
        raise TaskRecoveryBlocked("execution_intent_task_binding_missing")
    data = dict(context)
    if (
        str(data.get("bound_task_id") or "").strip() != task_id
        or str(data.get("bound_lottery_id") or "").strip()
        != str(payload.get("lottery_id") or "").strip()
        or str(data.get("bound_account_id") or "").strip()
        != str(payload.get("account_id") or "").strip()
        or str(data.get("bound_task_mode") or "").strip().casefold()
        != "real_run"
    ):
        raise TaskRecoveryBlocked("execution_intent_task_binding_mismatch")

    root_present = any(
        data.get(field) is not None
        for field in (
            "root_contract_version",
            "root_intent_id",
            "root_intent_hash",
        )
    )
    binding_present = any(
        data.get(field) is not None
        for field in (
            "binding_contract_version",
            "binding_task_id",
            "binding_intent_id",
            "binding_hash",
        )
    )
    message_empty = _execution_intent_message_is_empty(payload)
    if not root_present and not binding_present:
        if not message_empty:
            raise TaskRecoveryBlocked("execution_intent_database_root_missing")
        return None
    if not root_present:
        raise TaskRecoveryBlocked("execution_intent_database_root_missing")
    if not binding_present:
        raise TaskRecoveryBlocked("execution_intent_database_binding_missing")
    if message_empty:
        raise TaskRecoveryBlocked("execution_intent_message_binding_missing")

    root_row = {
        "contract_version": data.get("root_contract_version"),
        "intent_id": data.get("root_intent_id"),
        "intent_hash": data.get("root_intent_hash"),
        "lottery_id": data.get("root_lottery_id"),
        "source_task_id": data.get("root_source_task_id"),
        "source_account_id": data.get("root_source_account_id"),
        "platform": data.get("root_platform"),
        "raw_url": data.get("root_raw_url"),
        "canonical_url": data.get("root_canonical_url"),
        "full_action_plan": data.get("root_full_action_plan"),
        "full_action_plan_hash": data.get("root_full_action_plan_hash"),
        "full_required_actions": data.get("root_full_required_actions"),
        "full_required_actions_hash": data.get(
            "root_full_required_actions_hash"
        ),
        "rule_snapshot_id": data.get("root_rule_snapshot_id"),
        "rule_hash": data.get("root_rule_hash"),
        "execution_path_id": data.get("root_execution_path_id"),
        "target_hash": data.get("root_target_hash"),
    }
    requested_actions = _parse_json_value(
        data.get("binding_requested_actions")
    )
    bound_action_plan = _parse_json_value(
        data.get("binding_bound_action_plan")
    )
    if not isinstance(requested_actions, list) or not isinstance(
        bound_action_plan,
        dict,
    ):
        raise TaskRecoveryBlocked("execution_intent_database_payload_invalid")
    try:
        frozen = coerce_frozen_execution_intent(root_row)
        expected = build_task_execution_intent_binding(
            frozen,
            task_id=str(data.get("binding_task_id") or ""),
            account_id=data.get("binding_account_id"),
            binding_kind=str(data.get("binding_kind") or ""),
            requested_actions=requested_actions,
            bound_action_plan=bound_action_plan,
            execution_evidence_id=str(
                data.get("binding_execution_evidence_id") or ""
            ),
            execution_path_id=str(
                data.get("binding_execution_path_id") or ""
            ),
            target_hash=str(data.get("binding_target_hash") or ""),
            config_hash=str(data.get("binding_config_hash") or ""),
            execution_revision=data.get("binding_execution_revision"),
            account_lease_id=str(
                data.get("binding_account_lease_id") or ""
            ),
            account_lease_generation=data.get(
                "binding_account_lease_generation"
            ),
        )
        stored_matches = (
            data.get("binding_contract_version") == expected.contract_version
            and str(data.get("binding_task_id") or "") == expected.task_id
            and str(data.get("binding_intent_id") or "") == expected.intent_id
            and data.get("binding_lottery_id") == expected.lottery_id
            and data.get("binding_account_id") == expected.account_id
            and str(data.get("binding_kind") or "") == expected.binding_kind
            and tuple(requested_actions) == expected.requested_actions
            and str(data.get("binding_requested_actions_hash") or "")
            == expected.requested_actions_hash
            and canonical_json_bytes(bound_action_plan)
            == canonical_json_bytes(expected.bound_action_plan)
            and str(data.get("binding_bound_action_plan_hash") or "")
            == expected.bound_action_plan_hash
            and str(data.get("binding_evidence_action_plan_hash") or "")
            == expected.evidence_action_plan_hash
            and data.get("binding_rule_snapshot_id")
            == expected.rule_snapshot_id
            and str(data.get("binding_rule_hash") or "")
            == expected.rule_hash
            and str(data.get("binding_execution_evidence_id") or "")
            == expected.execution_evidence_id
            and str(data.get("binding_execution_evidence_kind") or "")
            == expected.execution_evidence_kind
            and (
                str(data.get("binding_exact_execution_evidence_id") or "")
                == str(expected.exact_execution_evidence_id or "")
            )
            and (
                str(data.get("binding_oauth_calibration_id") or "")
                == str(expected.oauth_calibration_id or "")
            )
            and str(data.get("binding_execution_path_id") or "")
            == expected.execution_path_id
            and str(data.get("binding_target_hash") or "")
            == expected.target_hash
            and str(data.get("binding_config_hash") or "")
            == expected.config_hash
            and data.get("binding_execution_revision")
            == expected.execution_revision
            and str(data.get("binding_account_lease_id") or "")
            == expected.account_lease_id
            and data.get("binding_account_lease_generation")
            == expected.account_lease_generation
            and str(data.get("binding_hash") or "") == expected.binding_hash
        )
        message_actions = _parse_json_value(payload.get("requested_actions"))
        message_plan = _parse_json_value(payload.get("action_plan"))
        message_matches = (
            str(payload.get("execution_intent_id") or "")
            == expected.intent_id
            and str(payload.get("execution_intent_hash") or "")
            == expected.intent_hash
            and str(payload.get("execution_intent_kind") or "")
            == expected.binding_kind
            and str(payload.get("execution_intent_binding_hash") or "")
            == expected.binding_hash
            and isinstance(message_actions, list)
            and tuple(message_actions) == expected.requested_actions
            and str(payload.get("requested_actions_hash") or "")
            == expected.requested_actions_hash
            and str(payload.get("requested_action_plan_hash") or "")
            == expected.bound_action_plan_hash
            and str(payload.get("execution_evidence_kind") or "")
            == expected.execution_evidence_kind
            and str(payload.get("exact_execution_evidence_id") or "")
            == str(expected.exact_execution_evidence_id or "")
            and str(payload.get("oauth_calibration_id") or "")
            == str(expected.oauth_calibration_id or "")
            and isinstance(message_plan, dict)
            and canonical_json_bytes(message_plan)
            == canonical_json_bytes(frozen.full_action_plan)
            and str(payload.get("action_plan_hash") or "")
            == frozen.full_action_plan_hash
        )
    except (
        ActionPlanV2Error,
        ExecutionIntentError,
        TypeError,
        ValueError,
    ) as exc:
        code = getattr(exc, "code", type(exc).__name__)
        raise TaskRecoveryBlocked(
            f"execution_intent_authority_invalid:{code}"
        ) from exc
    if not stored_matches:
        raise TaskRecoveryBlocked("execution_intent_database_binding_mismatch")
    if not message_matches:
        raise TaskRecoveryBlocked("execution_intent_message_binding_mismatch")
    return expected


async def _recover_stream_once(binding: TaskStreamBinding) -> None:
    pending = await redis.xpending_range(
        binding.stream_key,
        binding.group_name,
        min="-",
        max="+",
        count=50,
        idle=IDLE_THRESHOLD_MS,
    )
    if binding.legacy:
        await asyncio.gather(
            *(
                _recover_legacy_pending_entry(binding, message)
                for message in pending
            )
        )
        return
    for msg in pending:
        idle_ms = pending_idle_ms(msg)
        if idle_ms < IDLE_THRESHOLD_MS:
            continue

        message_id = msg["message_id"]
        fields = await _read_stream_fields(
            message_id,
            stream_key=binding.stream_key,
        )
        task_id = fields.get("task_id", message_id)

        decision = await _recovery_decision(task_id)
        if decision == "skip_owned_running_task":
            structured_log(
                "info",
                "recovery_skipped_owned_running_task",
                task_id=task_id,
                message_id=message_id,
                idle_ms=idle_ms,
                stream=binding.stream_key,
            )
            continue
        if decision == "ack_terminal_task":
            structured_log(
                "info",
                "recovery_ack_terminal_task",
                task_id=task_id,
                message_id=message_id,
                stream=binding.stream_key,
            )
            await _ack_converged_stream_message(
                message_id,
                fields,
                stream_key=binding.stream_key,
                group_name=binding.group_name,
            )
            await redis.delete(f"recovery_count:{task_id}")
            continue

        claimed = await redis.xclaim(
            binding.stream_key,
            binding.group_name,
            RECOVERY_CONSUMER,
            min_idle_time=IDLE_THRESHOLD_MS,
            message_ids=[message_id],
        )
        if not claimed:
            continue

        _claimed_id, claimed_fields = claimed[0]
        task_id = claimed_fields.get("task_id", task_id)
        authority = await _recovery_stream_authority(
            task_id,
            binding,
            claimed_fields,
            message_id=str(message_id),
        )
        if authority == "foreign":
            # The immutable outbox points at another stream, so this delivery
            # cannot be the canonical trigger for that task.
            await _ack_converged_stream_message(
                message_id,
                claimed_fields,
                stream_key=binding.stream_key,
                group_name=binding.group_name,
            )
            continue
        if authority != "exact":
            structured_log(
                "error",
                "recovery_stream_authority_unverified",
                task_id=task_id,
                message_id=message_id,
                stream=binding.stream_key,
            )
            continue

        recovery_state = await _prepare_task_for_recovery(task_id)
        if recovery_state == "skip_owned_running_task":
            structured_log(
                "info",
                "recovery_claim_recheck_skipped",
                task_id=task_id,
                message_id=message_id,
                stream=binding.stream_key,
            )
            continue
        if recovery_state in {"ack_terminal_task", "task_missing"}:
            await _ack_converged_stream_message(
                message_id,
                claimed_fields,
                stream_key=binding.stream_key,
                group_name=binding.group_name,
            )
            await redis.delete(f"recovery_count:{task_id}")
            continue
        if recovery_state == "real_run_reconciliation_required":
            structured_log(
                "error",
                "recovery_real_run_quarantined",
                task_id=task_id,
                message_id=message_id,
                stream=binding.stream_key,
            )
            await _ack_converged_stream_message(
                message_id,
                claimed_fields,
                stream_key=binding.stream_key,
                group_name=binding.group_name,
            )
            await redis.delete(f"recovery_count:{task_id}")
            continue

        recovery_key = f"recovery_count:{task_id}"
        current_count = int(await redis.get(recovery_key) or 0)
        if current_count >= MAX_RECOVERY_COUNT:
            structured_log(
                "error",
                "task_permanent_failure",
                task_id=task_id,
                recovery_count=current_count,
                stream=binding.stream_key,
            )
            settled = await _mark_recovery_exhausted(task_id)
            if settled:
                await _ack_converged_stream_message(
                    message_id,
                    claimed_fields,
                    stream_key=binding.stream_key,
                    group_name=binding.group_name,
                )
                await redis.delete(recovery_key)
            else:
                structured_log(
                    "info",
                    "recovery_exhausted_cleanup_skipped",
                    task_id=task_id,
                    message_id=message_id,
                    stream=binding.stream_key,
                )
            continue

        try:
            payload = await _rebuild_task_payload(
                task_id,
                stream_key=binding.stream_key,
                claimed_fields=claimed_fields,
            )
        except TaskRecoveryBlocked as exc:
            structured_log(
                "warning",
                "recovery_task_replay_blocked",
                task_id=task_id,
                reason=str(exc),
                stream=binding.stream_key,
            )
            settled = await _mark_recovery_blocked(task_id, str(exc))
            if settled:
                await _ack_converged_stream_message(
                    message_id,
                    claimed_fields,
                    stream_key=binding.stream_key,
                    group_name=binding.group_name,
                )
                await redis.delete(recovery_key)
            else:
                structured_log(
                    "info",
                    "recovery_gate_block_cleanup_skipped",
                    task_id=task_id,
                    message_id=message_id,
                    stream=binding.stream_key,
                )
            continue
        if payload is None:
            structured_log(
                "error",
                "recovery_task_row_missing",
                task_id=task_id,
                stream=binding.stream_key,
            )
            await _ack_converged_stream_message(
                message_id,
                claimed_fields,
                stream_key=binding.stream_key,
                group_name=binding.group_name,
            )
            await redis.delete(recovery_key)
            continue

        new_count = await redis.incr(recovery_key)
        await redis.expire(recovery_key, 86400)
        payload["resume_from_phase"] = "latest"
        payload["recovery_generation"] = str(new_count)

        structured_log(
            "warning",
            "recovered_pending_task",
            task_id=task_id,
            recovery_count=new_count,
            mode=payload.get("mode"),
            stream=binding.stream_key,
        )
        if _has_valid_legacy_fanout_source(binding, claimed_fields):
            source_message_id = str(
                claimed_fields.get(LEGACY_SOURCE_MESSAGE_ID_FIELD) or ""
            ).strip()
            marker_key = _legacy_fanout_marker_key(source_message_id)
            marker_member = _legacy_fanout_marker_member(
                binding.stream_key,
                str(message_id),
                task_id,
            )
            if not await redis.sismember(marker_key, marker_member):
                structured_log(
                    "error",
                    "recovery_legacy_fanout_marker_lost",
                    task_id=task_id,
                    message_id=message_id,
                    stream=binding.stream_key,
                )
                continue
            retry_msg_id = await _reenqueue_fanned_out_task(
                binding,
                payload,
                message_id=str(message_id),
                marker_key=marker_key,
            )
        else:
            retry_msg_id = await redis.xadd(binding.stream_key, payload)
            if retry_msg_id:
                await _ack_converged_stream_message(
                    message_id,
                    claimed_fields,
                    stream_key=binding.stream_key,
                    group_name=binding.group_name,
                )
        if not retry_msg_id:
            structured_log(
                "error",
                "recovery_enqueue_failed",
                task_id=task_id,
                stream=binding.stream_key,
            )


async def _reenqueue_fanned_out_task(
    binding: TaskStreamBinding,
    payload: dict,
    *,
    message_id: str,
    marker_key: str,
) -> str:
    field_args = [
        value
        for field, field_value in payload.items()
        for value in (str(field), str(field_value))
    ]
    result = await redis.eval(
        SAFE_FANOUT_RECOVERY_REENQUEUE_LUA,
        2,
        binding.stream_key,
        marker_key,
        binding.group_name,
        message_id,
        _legacy_fanout_marker_prefix(
            binding.stream_key,
            str(payload.get("task_id") or ""),
        ),
        *field_args,
    )
    return str(result or "")


async def _recover_legacy_pending_entry(
    binding: TaskStreamBinding,
    message: dict,
) -> None:
    message_id = str(message.get("message_id") or "")

    async def recover() -> None:
        if pending_idle_ms(message) < IDLE_THRESHOLD_MS:
            return
        fields = await _read_stream_fields(
            message_id,
            stream_key=binding.stream_key,
        )
        task_id = str(fields.get("task_id") or message_id)
        decision = await _recovery_decision(task_id)
        if decision == "skip_owned_running_task":
            return
        if decision == "ack_terminal_task":
            await _ack_converged_stream_message(
                message_id,
                fields,
                stream_key=binding.stream_key,
                group_name=binding.group_name,
            )
            await redis.delete(f"recovery_count:{task_id}")
            return
        claimed = await redis.xclaim(
            binding.stream_key,
            binding.group_name,
            RECOVERY_CONSUMER,
            min_idle_time=IDLE_THRESHOLD_MS,
            message_ids=[message_id],
        )
        if not claimed:
            return
        _claimed_id, claimed_fields = claimed[0]
        await _process_legacy_claimed_message(
            message_id,
            dict(claimed_fields or {}),
        )

    try:
        await asyncio.wait_for(
            recover(),
            timeout=LEGACY_FANOUT_ENTRY_TIMEOUT_SECONDS,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        structured_log(
            "error",
            "legacy_task_pending_recovery_failed",
            message_id=message_id,
            exception=exc,
        )


async def _read_stream_fields(
    message_id,
    *,
    stream_key: str = STREAM_KEY,
) -> dict:
    try:
        rows = await redis.xrange(
            stream_key,
            min=message_id,
            max=message_id,
            count=1,
        )
    except Exception as exc:
        structured_log("warning", "recovery_read_stream_fields_failed", message_id=message_id, error=str(exc))
        return {}
    if not rows:
        return {}
    _mid, fields = rows[0]
    return dict(fields or {})


async def _recovery_stream_authority(
    task_id: str,
    binding: TaskStreamBinding,
    claimed_fields: dict,
    *,
    message_id: str | None = None,
) -> str:
    """Verify stream ownership before recovery mutates authoritative task rows."""

    outbox = await database.fetch_one(
        """SELECT stream_key, payload
           FROM outbox_events
           WHERE dedup_key = :task_id
           LIMIT 1""",
        {"task_id": task_id},
    )
    if not outbox:
        return "unverified"
    outbox_stream = str(outbox["stream_key"] or "").strip()
    legacy_fanout_metadata = bool(
        outbox_stream == LEGACY_TASK_STREAM_KEY
        and _has_valid_legacy_fanout_source(binding, claimed_fields)
    )
    legacy_fanout = False
    if legacy_fanout_metadata:
        source_message_id = str(
            claimed_fields.get(LEGACY_SOURCE_MESSAGE_ID_FIELD) or ""
        ).strip()
        if not message_id:
            return "unverified"
        marker_authorized = await redis.sismember(
            _legacy_fanout_marker_key(source_message_id),
            _legacy_fanout_marker_member(
                binding.stream_key,
                str(message_id),
                str(task_id),
            ),
        )
        if not marker_authorized:
            return "unverified"
        legacy_fanout = True
    if outbox_stream != binding.stream_key and not legacy_fanout:
        return "foreign"
    try:
        payload = _parse_outbox_payload(outbox["payload"])
    except TaskRecoveryBlocked:
        return "unverified"
    comparison_fields = claimed_fields
    if outbox_stream == LEGACY_TASK_STREAM_KEY:
        payload = _with_legacy_execution_intent_defaults(payload)
        comparison_fields = _with_legacy_execution_intent_defaults(
            claimed_fields
        )
    if str(payload.get("task_id") or "").strip() != str(task_id or "").strip():
        return "unverified"
    if "weibo_rip" in payload:
        return "unverified"

    try:
        validate_task_stream_message(binding, payload)
    except ValueError:
        return "unverified"

    # Recovery adds only resume metadata. Every base field present in either
    # the immutable payload or the claimed message must remain byte-equivalent.
    for field in LOTTERY_TASK_FIELDS:
        if field in payload or field in comparison_fields:
            if str(payload.get(field, "")) != str(
                comparison_fields.get(field, "")
            ):
                return "unverified"
    return "exact"


def _has_valid_legacy_fanout_source(
    binding: TaskStreamBinding,
    fields: dict | None,
) -> bool:
    if binding.legacy or not isinstance(fields, dict):
        return False
    source_stream = str(
        fields.get(LEGACY_SOURCE_STREAM_FIELD) or ""
    ).strip()
    source_message_id = str(
        fields.get(LEGACY_SOURCE_MESSAGE_ID_FIELD) or ""
    ).strip()
    return bool(
        source_stream == LEGACY_TASK_STREAM_KEY
        and _REDIS_STREAM_ID_RE.fullmatch(source_message_id)
    )


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


async def _quarantine_expired_real_run_locked(
    row,
    lottery,
) -> str:
    """Settle an expired mutating task into explicit unknown-outcome state.

    Caller holds task -> lottery -> account locks.  The lottery execution lock
    and account operation lease intentionally remain fenced for operator
    reconciliation; only the worker lease is expired.  No real-run task is ever
    placed back on a stream by recovery.
    """

    task_id = str(row["task_id"] or "").strip()
    platform = str(lottery["platform"] if lottery else "").strip().casefold()
    await database.execute(
        """UPDATE task_runs
           SET status = 'failed',
               reconciliation_required = 1,
               error_message = 'real-run lease expired; external outcome requires reconciliation',
               finished_at = NOW(), lease_expires_at = NULL
           WHERE task_id = :task_id AND status = 'running'""",
        {"task_id": task_id},
    )
    # ``started`` means the durable intent was committed before the network
    # mutation began, but no definitive settlement was recorded. Convert it to
    # explicit unknown so reconciliation never relies on parsing an error.
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
    await database.execute(
        """UPDATE accounts
           SET status = 'cooling', updated_at = NOW(), version = version + 1
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
            """SELECT status FROM circuit_breakers
               WHERE scope = :scope FOR UPDATE""",
            {"scope": f"platform:{platform}"},
        )
        if (
            not breaker
            or str(breaker["status"] or "").strip().casefold() != "open"
        ):
            raise RuntimeError("recovery_platform_breaker_not_persisted")
    return "real_run_reconciliation_required"


async def _settle_safe_mode_stale_recovery_blocked_locked(
    row,
    *,
    reason: str,
    outbox_id=None,
) -> str:
    """Fail a read-only task that cannot prove an exact safe replay."""

    task_id = str(row["task_id"] or "").strip()
    task_mode = str(row["task_mode"] or "").strip().casefold()
    error = f"stale running recovery blocked: {reason}"[:480]
    await database.execute(
        """UPDATE task_runs
           SET status = 'failed', error_message = :error, finished_at = NOW(),
               worker_id = NULL, stream_message_id = NULL,
               lease_expires_at = NULL
           WHERE task_id = :task_id AND status = 'running'""",
        {"task_id": task_id, "error": error},
    )
    await database.execute(
        """UPDATE lotteries
           SET status = 'pending', execution_lock = NULL, locked_at = NULL
           WHERE id = :lottery_id
             AND execution_lock = :task_id
             AND status = 'running'""",
        {"lottery_id": row["lottery_id"], "task_id": task_id},
    )
    await database.execute(
        """UPDATE account_operation_leases
           SET released_at = COALESCE(released_at, NOW())
           WHERE lease_id = :lease_id
             AND account_id = :account_id
             AND generation = :lease_generation
             AND operation_kind = :operation_kind
             AND owner_id = :task_id
             AND task_id = :task_id""",
        {
            "lease_id": row["account_lease_id"],
            "account_id": row["account_id"],
            "lease_generation": row["account_lease_generation"],
            "operation_kind": task_mode,
            "task_id": task_id,
        },
    )
    if outbox_id is not None:
        # Fence an in-flight relay receipt with the status transition. A stale
        # relayer may still have emitted an at-least-once duplicate, but the now
        # terminal task row prevents it from being claimed.
        await database.execute(
            """UPDATE outbox_events
               SET status = 'failed', last_error = :error
               WHERE id = :outbox_id""",
            {"outbox_id": outbox_id, "error": error},
        )
    return "safe_mode_recovery_failed"


async def _recover_stale_running_task_from_database(
    task_id: str,
    *,
    expected_platform: str,
) -> str:
    """Recover one expired DB owner without relying on Redis/Pending state."""

    normalized_task_id = str(task_id or "").strip()
    normalized_platform = str(expected_platform or "").strip().casefold()
    if not normalized_task_id or not normalized_platform:
        raise ValueError("stale_running_recovery_binding_invalid")

    async with database.transaction():
        # Existing terminal Outbox settlement locks outbox -> task. Match that
        # order here; taking task -> outbox would introduce a deadlock cycle.
        outbox = await database.fetch_one(
            """SELECT id, stream_key, payload, status, attempts, dedup_key
               FROM outbox_events
               WHERE dedup_key = :task_id
               FOR UPDATE""",
            {"task_id": normalized_task_id},
        )
        row = await database.fetch_one(
            """SELECT task_id, account_id, lottery_id, status, worker_id,
                      task_mode, reconciliation_required,
                      account_lease_id, account_lease_generation,
                      CASE
                        WHEN lease_expires_at IS NOT NULL
                         AND lease_expires_at > NOW()
                        THEN 1 ELSE 0
                      END AS lease_active
               FROM task_runs
               WHERE task_id = :task_id
               FOR UPDATE""",
            {"task_id": normalized_task_id},
        )
        if not row:
            return "task_missing"
        if str(row["status"] or "").strip().casefold() != "running":
            return "skip_changed_task"
        if int(row["lease_active"] or 0) == 1:
            return "skip_owned_running_task"

        # Preserve the Worker lock order after the task row. The Outbox lock is
        # not observed by Workers and only serialises relay/recovery ownership.
        lottery = await database.fetch_one(
            """SELECT id, platform, status, execution_lock
               FROM lotteries
               WHERE id = :lottery_id
               FOR UPDATE""",
            {"lottery_id": row["lottery_id"]},
        )
        account = await database.fetch_one(
            """SELECT id, status
               FROM accounts
               WHERE id = :account_id
               FOR UPDATE""",
            {"account_id": row["account_id"]},
        )
        actual_platform = str(
            lottery["platform"] if lottery else ""
        ).strip().casefold()
        if actual_platform != normalized_platform:
            # Platform may have changed between the bounded candidate read and
            # row lock. Let the matching independent scanner own it.
            return "skip_platform_changed"

        task_mode = str(row["task_mode"] or "").strip().casefold()
        if (
            task_mode not in SAFE_REPLAY_TASK_MODES
            or int(row["reconciliation_required"] or 0) != 0
        ):
            return await _quarantine_expired_real_run_locked(row, lottery)

        outbox_id = outbox["id"] if outbox else None
        lottery_claim_valid = bool(
            lottery
            and str(lottery["status"] or "").strip().casefold() == "running"
            and str(lottery["execution_lock"] or "").strip()
            == normalized_task_id
        )
        account_ready = bool(
            account
            and str(account["status"] or "").strip().casefold() == "ready"
        )
        if not lottery_claim_valid or not account_ready:
            return await _settle_safe_mode_stale_recovery_blocked_locked(
                row,
                reason="safe_mode_authority_changed",
                outbox_id=outbox_id,
            )
        if not outbox:
            return await _settle_safe_mode_stale_recovery_blocked_locked(
                row,
                reason="immutable_task_payload_missing",
            )

        outbox_stream = str(outbox["stream_key"] or "").strip()
        binding = task_stream_binding_for_key(outbox_stream)
        if (
            binding is None
            or (
                not binding.legacy
                and binding.platform != normalized_platform
            )
            or (
                binding.legacy
                and not settings.legacy_task_stream_drain_enabled
            )
        ):
            return await _settle_safe_mode_stale_recovery_blocked_locked(
                row,
                reason="immutable_task_stream_unsupported",
                outbox_id=outbox_id,
            )
        try:
            # Validate the complete immutable payload and execution-intent
            # authority while all mutable task/lottery/account rows are locked.
            await _rebuild_task_payload(
                normalized_task_id,
                stream_key=outbox_stream,
                claimed_fields={},
            )
        except (TaskRecoveryBlocked, TypeError, ValueError) as exc:
            return await _settle_safe_mode_stale_recovery_blocked_locked(
                row,
                reason=str(exc),
                outbox_id=outbox_id,
            )

        lease = await database.fetch_one(
            """SELECT lease_id, account_id, generation, operation_kind,
                      owner_id, task_id,
                      CASE WHEN expires_at > NOW() THEN 1 ELSE 0 END
                        AS lease_active,
                      CASE WHEN released_at IS NULL THEN 1 ELSE 0 END
                        AS lease_unreleased,
                      CASE WHEN generation = (
                        SELECT MAX(newest.generation)
                        FROM account_operation_leases AS newest
                        WHERE newest.account_id = :account_id
                      ) THEN 1 ELSE 0 END AS lease_latest_generation,
                      (
                        SELECT COUNT(*)
                        FROM account_operation_leases AS live
                        WHERE live.account_id = :account_id
                          AND live.released_at IS NULL
                          AND live.expires_at > NOW()
                      ) AS active_account_lease_count
               FROM account_operation_leases
               WHERE lease_id = :lease_id
                 AND account_id = :account_id
                 AND generation = :lease_generation
               FOR UPDATE""",
            {
                "lease_id": row["account_lease_id"],
                "account_id": row["account_id"],
                "lease_generation": row["account_lease_generation"],
            },
        )
        try:
            lease_valid = bool(
                lease
                and str(lease["lease_id"] or "").strip()
                == str(row["account_lease_id"] or "").strip()
                and int(lease["account_id"] or 0) == int(row["account_id"])
                and int(lease["generation"] or 0)
                == int(row["account_lease_generation"] or 0)
                and str(lease["operation_kind"] or "").strip().casefold()
                == task_mode
                and str(lease["owner_id"] or "").strip()
                == normalized_task_id
                and str(lease["task_id"] or "").strip()
                == normalized_task_id
                and int(lease["lease_active"] or 0) == 1
                and int(lease["lease_unreleased"] or 0) == 1
                and int(lease["lease_latest_generation"] or 0) == 1
                and int(lease["active_account_lease_count"] or 0) == 1
            )
        except (TypeError, ValueError):
            lease_valid = False
        if not lease_valid:
            return await _settle_safe_mode_stale_recovery_blocked_locked(
                row,
                reason="account_operation_lease_not_recoverable",
                outbox_id=outbox_id,
            )

        # Extend only a still-active, exact read-only lease. Never resurrect an
        # expired generation: a newer account operation may already own the
        # account, and changing the immutable message binding would be unsafe.
        await database.execute(
            """UPDATE account_operation_leases
               SET expires_at = DATE_ADD(NOW(), INTERVAL 30 MINUTE)
               WHERE lease_id = :lease_id
                 AND account_id = :account_id
                 AND generation = :lease_generation
                 AND operation_kind = :operation_kind
                 AND owner_id = :task_id
                 AND task_id = :task_id
                 AND released_at IS NULL
                 AND expires_at > NOW()""",
            {
                "lease_id": row["account_lease_id"],
                "account_id": row["account_id"],
                "lease_generation": row["account_lease_generation"],
                "operation_kind": task_mode,
                "task_id": normalized_task_id,
            },
        )
        renewed_lease = await database.fetch_one(
            """SELECT CASE WHEN expires_at > NOW() THEN 1 ELSE 0 END
                        AS lease_active,
                      CASE WHEN released_at IS NULL THEN 1 ELSE 0 END
                        AS lease_unreleased
               FROM account_operation_leases
               WHERE lease_id = :lease_id
                 AND account_id = :account_id
                 AND generation = :lease_generation
               FOR UPDATE""",
            {
                "lease_id": row["account_lease_id"],
                "account_id": row["account_id"],
                "lease_generation": row["account_lease_generation"],
            },
        )
        if (
            not renewed_lease
            or int(renewed_lease["lease_active"] or 0) != 1
            or int(renewed_lease["lease_unreleased"] or 0) != 1
        ):
            return await _settle_safe_mode_stale_recovery_blocked_locked(
                row,
                reason="account_operation_lease_renewal_failed",
                outbox_id=outbox_id,
            )
        await database.execute(
            """UPDATE task_runs
               SET status = 'queued', worker_id = NULL,
                   stream_message_id = NULL, lease_expires_at = NULL,
                   error_message = NULL
               WHERE task_id = :task_id
                 AND status = 'running'
                 AND (
                   lease_expires_at IS NULL
                   OR lease_expires_at <= NOW()
                 )""",
            {"task_id": normalized_task_id},
        )
        await database.execute(
            """UPDATE lotteries
               SET status = 'claimed'
               WHERE id = :lottery_id
                 AND execution_lock = :task_id
                 AND status = 'running'""",
            {
                "lottery_id": row["lottery_id"],
                "task_id": normalized_task_id,
            },
        )
        # Re-arm the exact immutable Outbox row in the same transaction as the
        # DB state transition. A process crash can therefore leave either the
        # old running state or a dispatchable queued state, never queued+sent
        # with no Redis message.
        await database.execute(
            """UPDATE outbox_events
               SET status = 'pending', attempts = 0, sent_at = NULL,
                   redis_delivery_epoch = NULL,
                   last_error = 'expired read-only task owner; scheduled for authoritative replay'
               WHERE id = :outbox_id
                 AND dedup_key = :task_id""",
            {
                "outbox_id": outbox_id,
                "task_id": normalized_task_id,
            },
        )
        return "requeued_safe"


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
                return await _quarantine_expired_real_run_locked(row, lottery)
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
            """SELECT tr.account_id, tr.lottery_id, tr.status,
                      tr.task_mode, tr.account_lease_id,
                      tr.account_lease_generation,
                      execution_binding.binding_kind
                        AS execution_intent_kind
               FROM task_runs tr
               LEFT JOIN task_execution_intent_bindings execution_binding
                 ON execution_binding.task_id = tr.task_id
               WHERE tr.task_id = :task_id
               FOR UPDATE""",
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
        await _release_recovery_account_lease(row, task_id=task_id)
    return True


async def _mark_recovery_exhausted(task_id: str) -> bool:
    try:
        async with database.transaction():
            row = await database.fetch_one(
                """SELECT tr.account_id, tr.lottery_id, tr.status,
                          tr.task_mode, tr.account_lease_id,
                          tr.account_lease_generation,
                          execution_binding.binding_kind
                            AS execution_intent_kind
                   FROM task_runs tr
                   LEFT JOIN task_execution_intent_bindings
                     execution_binding
                     ON execution_binding.task_id = tr.task_id
                   WHERE tr.task_id = :task_id
                   FOR UPDATE""",
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
            await _release_recovery_account_lease(
                row,
                task_id=task_id,
            )
        return True
    except Exception as exc:
        structured_log("error", "recovery_exhausted_mark_failed_failed", task_id=task_id, error=str(exc))
        return False


async def _release_recovery_account_lease(
    task,
    *,
    task_id: str,
) -> bool:
    """Release only the operation kind authorized by the task intent."""

    task_mode = str(task["task_mode"] or "").strip().casefold()
    try:
        if task_mode == "real_run":
            execution_intent_kind = str(
                task["execution_intent_kind"]
                or LEGACY_FULL_EXECUTION_INTENT_KIND
            )
            operation_kind = (
                lease_operation_kind_for_execution_intent(
                    execution_intent_kind
                )
            )
        elif task_mode in {"dry_run", "shadow_run"}:
            operation_kind = task_mode
        else:
            raise ValueError("task_mode_invalid")
    except ValueError as exc:
        structured_log(
            "error",
            "recovery_account_lease_binding_invalid",
            task_id=task_id,
            error=str(exc),
        )
        return False
    await database.execute(
        """UPDATE account_operation_leases
           SET released_at = COALESCE(released_at, NOW())
           WHERE lease_id = :lease_id
             AND account_id = :account_id
             AND generation = :lease_generation
             AND operation_kind = :operation_kind
             AND owner_id = :task_id
             AND released_at IS NULL""",
        {
            "lease_id": task["account_lease_id"],
            "account_id": task["account_id"],
            "lease_generation": task[
                "account_lease_generation"
            ],
            "operation_kind": operation_kind,
            "task_id": task_id,
        },
    )
    return True


async def _rebuild_task_payload(
    task_id: str,
    *,
    stream_key: str = STREAM_KEY,
    claimed_fields: dict | None = None,
) -> dict | None:
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
                  l.rule_hash, l.action_plan_hash,
                  intent_binding.binding_kind AS recovery_binding_kind,
                  intent_binding.requested_actions
                    AS recovery_requested_actions
           FROM task_runs tr
           JOIN lotteries l ON l.id = tr.lottery_id
           JOIN accounts a ON a.id = tr.account_id
           LEFT JOIN task_execution_intent_bindings intent_binding
             ON intent_binding.task_id = tr.task_id
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
        row_values = dict(row)
        execution_required_actions = None
        if (
            str(
                row_values.get("recovery_binding_kind") or ""
            ).strip()
            == "repair"
        ):
            requested = _parse_json_value(
                row_values.get("recovery_requested_actions")
            )
            if (
                not isinstance(requested, list)
                or not requested
                or any(
                    not isinstance(action, str) or not action
                    for action in requested
                )
            ):
                raise TaskRecoveryBlocked(
                    "execution_intent_requested_actions_invalid"
                )
            execution_required_actions = tuple(requested)
        decision = await evaluate_real_run_decision(
            row,
            account_id=row["account_id"],
            execution_required_actions=execution_required_actions,
            record=False,
        )
        # A policy/input mapping regression must never turn an authoritative
        # readiness blocker into a replay authorization. Recovery still
        # validates the immutable Outbox and complete intent binding before
        # any message can be re-enqueued.
        if not decision["allowed"] or decision.get("blockers"):
            blockers = ",".join(
                decision.get("failed_gates")
                or decision.get("blockers")
                or ["unknown"]
            )
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
    binding = task_stream_binding_for_key(stream_key)
    if binding is None:
        raise TaskRecoveryBlocked("immutable_task_stream_unsupported")
    if not outbox:
        raise TaskRecoveryBlocked("immutable_task_payload_missing")
    outbox_stream = str(outbox["stream_key"] or "").strip()
    legacy_fanout = bool(
        outbox_stream == LEGACY_TASK_STREAM_KEY
        and _has_valid_legacy_fanout_source(binding, claimed_fields)
    )
    if outbox_stream != stream_key and not legacy_fanout:
        raise TaskRecoveryBlocked("immutable_task_payload_missing")
    payload = _parse_outbox_payload(outbox["payload"])
    if outbox_stream == LEGACY_TASK_STREAM_KEY:
        payload = _with_legacy_execution_intent_defaults(payload)
    if not binding.legacy and binding.platform != platform:
        raise TaskRecoveryBlocked("immutable_task_stream_platform_mismatch")
    if "weibo_rip" in payload:
        raise TaskRecoveryBlocked("legacy_plaintext_weibo_rip_forbidden")
    try:
        validate_task_stream_message(binding, payload)
    except ValueError as exc:
        raise TaskRecoveryBlocked(str(exc)) from exc
    execution_intent_binding = (
        await _validate_recovery_execution_intent_authority(payload)
    )
    if execution_path_id == WEIBO_OAUTH_EXECUTION_PATH:
        parsed_plan = _parse_json_value(payload.get("action_plan"))
        if not isinstance(parsed_plan, dict):
            raise TaskRecoveryBlocked("immutable_task_action_plan_invalid")
        runtime_plan = (
            execution_intent_binding.bound_action_plan
            if (
                execution_intent_binding is not None
                and execution_intent_binding.binding_kind == "repair"
            )
            else parsed_plan
        )
        weibo_rip_encrypted = str(payload.get("weibo_rip_encrypted") or "")
        required_actions = set(runtime_plan.get("required_actions") or [])
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
                    runtime_plan.get("runtime_capability_requirements") or {}
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
    rebuilt = {
        field: str(payload[field])
        for field in LOTTERY_TASK_FIELDS
    }
    if legacy_fanout:
        rebuilt[LEGACY_SOURCE_STREAM_FIELD] = LEGACY_TASK_STREAM_KEY
        rebuilt[LEGACY_SOURCE_MESSAGE_ID_FIELD] = str(
            claimed_fields[LEGACY_SOURCE_MESSAGE_ID_FIELD]
        )
    return rebuilt


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
