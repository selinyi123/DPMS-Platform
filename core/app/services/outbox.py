"""Transactional outbox for Redis stream enqueues (P1-2).

The dispatch path previously did three independent writes with no shared
transaction:

    INSERT task_runs            (DB)
    UPDATE lotteries claimed    (DB)
    redis.xadd lottery_tasks:<platform>    (Redis)

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

The message builder and retry predicates remain pure for fast unit tests; the
real-storage contract suite separately covers MySQL and Redis behavior.
"""

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import re
import secrets
import socket
from collections.abc import Mapping

from app.config import settings
from app.db import database, execute_affected_rows, redis
from app.task_streams import (
    LEGACY_TASK_STREAM_KEY,
    is_task_stream,
    task_stream_binding_for_key,
    task_stream_bindings,
    validate_task_stream_message,
)
from app.adapter_probe_streams import (
    adapter_probe_stream_binding_for_key,
    adapter_probe_stream_bindings,
    is_adapter_probe_stream,
    validate_adapter_probe_stream_message,
)
from app.account_calibration_streams import (
    account_calibration_stream_binding_for_key,
    account_calibration_stream_bindings,
    is_account_calibration_stream,
    validate_account_calibration_stream_message,
)
from app.login_streams import (
    LOGIN_REQUEST_OUTBOX_DEDUP_PREFIX,
    LOGIN_REQUEST_STREAM_KEY,
    is_login_request_stream,
    validate_login_request_stream_message,
)
from app.utils.log import structured_log
from app.services.adapter_probe_outbox import (
    settle_terminal_adapter_probe_delivery_failure,
)
from app.services.account_calibration_outbox import (
    settle_terminal_account_calibration_delivery_failure,
)
from app.services.login_request_outbox import (
    settle_terminal_login_request_delivery_failure,
)
from shared.execution_contracts import (
    LEGACY_FULL_EXECUTION_INTENT_KIND,
    lease_operation_kind_for_execution_intent,
)
from shared.platform_scope import normalize_platform_scope


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
    "weibo_rip_encrypted",
)

OUTBOX_MAX_ATTEMPTS = 5
OUTBOX_BATCH = 50
OUTBOX_POLL_SECONDS = 5
# Reconciliation already runs after a synchronous startup continuity check, so
# delaying its first background pass by at most one normal poll interval is
# safe. A small deterministic per-instance/cycle offset keeps separately
# deployed platform runners from falling back into lockstep after the initial
# phase.
OUTBOX_RECONCILIATION_JITTER_SECONDS = 0.5
# Stale ``sending`` rows are global maintenance, not lane-local work.  A
# single low-frequency pass avoids starting roughly twenty identical UPDATEs
# every five seconds while still reclaiming before the 120-second readiness
# stall window.
OUTBOX_RECLAIM_POLL_SECONDS = 30
OUTBOX_RECLAIM_BATCH = 500
# A row claimed for delivery (``sending``) by a relayer that then crashed is
# reclaimed to ``pending`` after this window so it is not stuck forever.
OUTBOX_SENDING_RECLAIM_SECONDS = 60
OUTBOX_ARCHIVE_DEFAULT_RETENTION_SECONDS = 30 * 24 * 60 * 60
OUTBOX_ARCHIVE_DEFAULT_BATCH = 200
OUTBOX_ARCHIVE_STREAM_SOURCE_TABLE = "outbox_events"
# Lotteries left locked this long whose task already reached a terminal state
# (or never materialised) get their execution lock released by the reconciler.
ORPHAN_LOCK_GRACE_MINUTES = 15
REDIS_TASK_STREAM_EPOCH_SETTING = "redis_task_stream_epoch"
REDIS_TASK_STREAM_EPOCH_PREFIX = "redis:v2:"
REDIS_TASK_STREAM_LANE_EPOCH_PREFIX = "redis:v3:"
REDIS_TASK_STREAM_SENTINEL_KEY = "dpms:task-stream:continuity:v1"
REDIS_TASK_STREAM_LANE_TOKEN_PREFIX = (
    "dpms:task-stream:lane-continuity:v1:"
)
REDIS_TASK_STREAM_LANE_STATE_PREFIX = "dpms:task-stream:lane-state:v1:"
REDIS_RUN_ID_RE = re.compile(r"^[0-9a-fA-F]{40}$")
REDIS_CONTINUITY_TOKEN_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_OUTBOX_INSTANCE_IDENTITY_CACHE: tuple[int, str] | None = None
READ_OR_CREATE_REDIS_CONTINUITY_TOKEN_LUA = """
redis.call('SET', KEYS[1], ARGV[1], 'NX')
return redis.call('GET', KEYS[1])
"""
READ_REDIS_TASK_STREAM_LANE_CONTINUITY_LUA = """
local global_token = redis.call('GET', KEYS[1])
if not global_token then
  redis.call('SET', KEYS[1], ARGV[1], 'NX')
  global_token = redis.call('GET', KEYS[1])
end

local lane_token = redis.call('GET', KEYS[2])
local lane_token_preexisting = lane_token and 1 or 0
if not lane_token then
  redis.call('SET', KEYS[2], ARGV[2], 'NX')
  lane_token = redis.call('GET', KEYS[2])
end

local lane_length = redis.call('XLEN', KEYS[3])
local lane_state = redis.call('GET', KEYS[4])
if lane_length == 0 then
  if lane_state == 'nonempty'
     or (not lane_state and lane_token_preexisting == 1) then
    redis.call('SET', KEYS[2], ARGV[2])
    lane_token = ARGV[2]
  end
  if ARGV[3] == '1' then
    redis.call('SET', KEYS[4], 'nonempty')
  else
    redis.call('SET', KEYS[4], 'empty')
  end
else
  redis.call('SET', KEYS[4], 'nonempty')
end

return {global_token, lane_token, tostring(lane_length)}
"""


class RedisTaskStreamEpochUnavailable(RuntimeError):
    """Redis cannot prove process and dataset continuity for safe task relay."""


async def read_redis_task_stream_epoch(
    stream_key: str | None = None,
    *,
    prepare_delivery: bool = False,
) -> str:
    """Return Redis process and dataset continuity, failing closed on either.

    ``run_id`` detects process replacement. The Redis-local sentinel detects a
    same-process ``FLUSHDB``.  When ``stream_key`` is provided, an additional
    lane token changes exactly once when that stream transitions from observed
    non-empty to empty/missing. This detects deletion of one platform's
    standard or repair lane without replaying sibling lanes.

    ``prepare_delivery`` marks an empty lane as expected-nonempty before XADD.
    If the relayer dies after the append is acknowledged but before its DB
    receipt/confirmation, a later empty observation still rotates the lane
    token and safely re-arms only queued tasks for that lane.
    """

    normalized_stream_key = None
    if stream_key is not None:
        normalized_stream_key = str(stream_key or "").strip()
        if (
            task_stream_binding_for_key(normalized_stream_key) is None
            and adapter_probe_stream_binding_for_key(
                normalized_stream_key
            )
            is None
            and account_calibration_stream_binding_for_key(
                normalized_stream_key
            )
            is None
            and not is_login_request_stream(normalized_stream_key)
        ):
            raise RedisTaskStreamEpochUnavailable(
                "redis_task_stream_lane_unknown"
            )
    elif prepare_delivery:
        raise RedisTaskStreamEpochUnavailable(
            "redis_task_stream_lane_required"
        )

    try:
        info = await redis.info(section="server")
    except Exception as exc:
        raise RedisTaskStreamEpochUnavailable(
            "redis_server_info_unavailable"
        ) from exc
    if not isinstance(info, dict):
        raise RedisTaskStreamEpochUnavailable(
            "redis_server_info_invalid"
        )
    run_id = str(info.get("run_id") or "").strip()
    if not REDIS_RUN_ID_RE.fullmatch(run_id):
        raise RedisTaskStreamEpochUnavailable(
            "redis_server_run_id_unavailable"
        )
    try:
        candidate = secrets.token_hex(32)
    except Exception as exc:
        raise RedisTaskStreamEpochUnavailable(
            "redis_continuity_token_generation_unavailable"
        ) from exc
    try:
        if normalized_stream_key is None:
            token = await redis.eval(
                READ_OR_CREATE_REDIS_CONTINUITY_TOKEN_LUA,
                1,
                REDIS_TASK_STREAM_SENTINEL_KEY,
                candidate,
            )
            lane_token = None
        else:
            lane_candidate = secrets.token_hex(32)
            lane_result = await redis.eval(
                READ_REDIS_TASK_STREAM_LANE_CONTINUITY_LUA,
                4,
                REDIS_TASK_STREAM_SENTINEL_KEY,
                (
                    REDIS_TASK_STREAM_LANE_TOKEN_PREFIX
                    + normalized_stream_key
                ),
                normalized_stream_key,
                (
                    REDIS_TASK_STREAM_LANE_STATE_PREFIX
                    + normalized_stream_key
                ),
                candidate,
                lane_candidate,
                "1" if prepare_delivery else "0",
            )
            if (
                not isinstance(lane_result, (list, tuple))
                or len(lane_result) != 3
            ):
                raise RedisTaskStreamEpochUnavailable(
                    "redis_task_stream_lane_continuity_invalid"
                )
            token, lane_token, _lane_length = lane_result
    except Exception as exc:
        if isinstance(exc, RedisTaskStreamEpochUnavailable):
            raise
        raise RedisTaskStreamEpochUnavailable(
            "redis_continuity_sentinel_unavailable"
        ) from exc
    if isinstance(token, (bytes, bytearray)):
        try:
            token = token.decode("ascii")
        except UnicodeDecodeError as exc:
            raise RedisTaskStreamEpochUnavailable(
                "redis_continuity_sentinel_invalid"
            ) from exc
    token = str(token or "").strip()
    if not REDIS_CONTINUITY_TOKEN_RE.fullmatch(token):
        raise RedisTaskStreamEpochUnavailable(
            "redis_continuity_sentinel_invalid"
        )
    if lane_token is not None:
        if isinstance(lane_token, (bytes, bytearray)):
            try:
                lane_token = lane_token.decode("ascii")
            except UnicodeDecodeError as exc:
                raise RedisTaskStreamEpochUnavailable(
                    "redis_task_stream_lane_continuity_invalid"
                ) from exc
        lane_token = str(lane_token or "").strip()
        if not REDIS_CONTINUITY_TOKEN_RE.fullmatch(lane_token):
            raise RedisTaskStreamEpochUnavailable(
                "redis_task_stream_lane_continuity_invalid"
            )
        digest = hashlib.sha256(
            (
                f"{run_id.lower()}:{token.lower()}:"
                f"{normalized_stream_key}:{lane_token.lower()}"
            ).encode("utf-8")
        ).hexdigest()
        return f"{REDIS_TASK_STREAM_LANE_EPOCH_PREFIX}{digest}"
    return (
        f"{REDIS_TASK_STREAM_EPOCH_PREFIX}"
        f"{run_id.lower()}:{token.lower()}"
    )


def _owned_platforms(platforms) -> frozenset[str]:
    if platforms is None:
        return frozenset(normalize_platform_scope("all"))
    values = (platforms,) if isinstance(platforms, str) else tuple(platforms)
    return (
        frozenset(normalize_platform_scope(values))
        if values
        else frozenset()
    )


def _owned_bindings(
    bindings,
    *,
    platforms,
    include_shared: bool,
):
    selected = _owned_platforms(platforms)
    return tuple(
        binding
        for binding in bindings
        if (
            binding.platform in selected
            or (include_shared and binding.platform is None)
        )
    )


async def reconcile_redis_task_stream_epoch(
    *,
    platforms=None,
    include_shared: bool = True,
    require_all_owned_lanes: bool = False,
) -> int:
    """Replay queued task deliveries acknowledged by another Redis epoch.

    The runtime-setting row is the multi-Core coordination lock.  The UPDATE is
    deliberately restricted to immutable task Outbox authority, queued DB
    tasks, and the exact known task streams.  Running/terminal tasks and shared
    non-task streams are never replayed by this recovery path.
    """

    bindings = _owned_bindings(
        task_stream_bindings(
            include_legacy=(
                include_shared
                and settings.legacy_task_stream_drain_enabled
            )
        ),
        platforms=platforms,
        include_shared=include_shared,
    )
    if not bindings:
        return 0
    observed_epoch = await read_redis_task_stream_epoch()
    observed_lane_epochs: dict[str, str] = {}
    for binding in bindings:
        try:
            observed_lane_epochs[binding.stream_key] = (
                await read_redis_task_stream_epoch(binding.stream_key)
            )
        except Exception as exc:
            # A wrong-type/corrupt lane must fail closed locally without
            # suppressing deletion recovery for the other seven platform
            # lanes. Delivery to the unavailable lane independently fails its
            # own continuity check before XADD.
            structured_log(
                "error",
                "redis_task_stream_lane_epoch_unavailable",
                stream=binding.stream_key,
                platform=binding.platform or "legacy",
                repair=binding.repair,
                exception=exc,
            )
    if (
        not observed_lane_epochs
        or (
            require_all_owned_lanes
            and len(observed_lane_epochs) != len(bindings)
        )
    ):
        raise RedisTaskStreamEpochUnavailable(
            "redis_task_stream_lane_epochs_unavailable"
        )

    stream_values = {
        f"task_stream_{index}": stream_key
        for index, stream_key in enumerate(observed_lane_epochs)
    }
    lane_epoch_values = {
        f"task_epoch_{index}": observed_lane_epochs[stream_key]
        for index, stream_key in enumerate(observed_lane_epochs)
    }
    stream_placeholders = ", ".join(
        f":{name}" for name in stream_values
    )
    expected_lane_epoch = "CASE o.stream_key " + " ".join(
        (
            f"WHEN :task_stream_{index} "
            f"THEN :task_epoch_{index}"
        )
        for index in range(len(stream_values))
    ) + " END"
    previous_epoch = ""
    epoch_changed = False
    affected = 0
    async with database.transaction():
        # Insert an empty sentinel rather than the observed epoch so a rolling
        # upgrade conservatively replays pre-0016 sent+queued rows once.
        await database.execute(
            """INSERT INTO runtime_settings (setting_key, setting_value)
               VALUES (:setting_key, '')
               ON DUPLICATE KEY UPDATE setting_key = setting_key""",
            {"setting_key": REDIS_TASK_STREAM_EPOCH_SETTING},
        )
        row = await database.fetch_one(
            """SELECT setting_value
               FROM runtime_settings
               WHERE setting_key = :setting_key
               FOR UPDATE""",
            {"setting_key": REDIS_TASK_STREAM_EPOCH_SETTING},
        )
        if row is None:
            raise RuntimeError("redis_task_stream_epoch_lock_unavailable")
        previous_epoch = str(row["setting_value"] or "")
        epoch_changed = previous_epoch != observed_epoch

        # Run the mismatch check even when the global epoch is already current.
        # Drive from the small set of queued tasks, then point-lookup Outbox by
        # its unique dedup key.  Starting from historical sent rows would
        # rescan every terminal task whose old epoch can never be updated.
        await database.execute(
            f"""UPDATE task_runs AS tr FORCE INDEX (idx_task_run_status)
                STRAIGHT_JOIN outbox_events AS o
                  FORCE INDEX (uk_outbox_dedup)
                  ON o.dedup_key = tr.task_id
                SET o.status = 'pending',
                    o.attempts = 0,
                    o.sent_at = NULL,
                    o.redis_delivery_epoch = NULL,
                    o.last_error = :replay_reason
                WHERE tr.status = 'queued'
                  AND o.status = 'sent'
                  AND o.stream_key IN ({stream_placeholders})
                  AND (
                    o.redis_delivery_epoch IS NULL
                    OR o.redis_delivery_epoch <> ({expected_lane_epoch})
                  )""",
            {
                **stream_values,
                **lane_epoch_values,
                "replay_reason": (
                    "Redis task-stream lane epoch changed; queued delivery "
                    "scheduled for safe replay"
                ),
            },
        )
        count_row = await database.fetch_one(
            "SELECT ROW_COUNT() AS affected"
        )
        if count_row is None:
            raise RuntimeError(
                "redis_task_stream_epoch_replay_count_unavailable"
            )
        affected = int(count_row["affected"])
        if affected < 0:
            raise RuntimeError(
                "redis_task_stream_epoch_replay_count_invalid"
            )
        if epoch_changed:
            await database.execute(
                """UPDATE runtime_settings
                   SET setting_value = :observed_epoch, updated_at = NOW()
                   WHERE setting_key = :setting_key""",
                {
                    "observed_epoch": observed_epoch,
                    "setting_key": REDIS_TASK_STREAM_EPOCH_SETTING,
                },
            )

    if epoch_changed or affected:
        structured_log(
            "warning" if affected else "info",
            "redis_task_stream_epoch_reconciled",
            epoch_changed=epoch_changed,
            previous_epoch=previous_epoch or "unrecorded",
            replayed_queued_deliveries=affected,
        )
    return affected


async def _read_stream_lane_epochs(
    bindings,
    *,
    event_prefix: str,
    require_all_owned_lanes: bool = False,
) -> dict[str, str]:
    """Read continuity independently so one corrupt lane cannot mask peers."""

    observed: dict[str, str] = {}
    for binding in bindings:
        try:
            observed[binding.stream_key] = (
                await read_redis_task_stream_epoch(binding.stream_key)
            )
        except Exception as exc:
            structured_log(
                "error",
                f"redis_{event_prefix}_lane_epoch_unavailable",
                stream=binding.stream_key,
                platform=binding.platform or "legacy",
                exception=exc,
            )
    if (
        not observed
        or (
            require_all_owned_lanes
            and len(observed) != len(bindings)
        )
    ):
        raise RedisTaskStreamEpochUnavailable(
            f"redis_{event_prefix}_lane_epochs_unavailable"
        )
    return observed


async def reconcile_redis_adapter_probe_stream_epochs(
    *,
    platforms=None,
    include_shared: bool = True,
    require_all_owned_lanes: bool = False,
) -> int:
    """Replay only queued Probe deliveries whose exact Redis lane was lost."""

    bindings = _owned_bindings(
        adapter_probe_stream_bindings(
            include_legacy=(
                include_shared
                and settings.legacy_control_stream_drain_enabled
            )
        ),
        platforms=platforms,
        include_shared=include_shared,
    )
    if not bindings:
        return 0
    observed = await _read_stream_lane_epochs(
        bindings,
        event_prefix="adapter_probe_stream",
        require_all_owned_lanes=require_all_owned_lanes,
    )
    stream_values = {
        f"probe_stream_{index}": stream_key
        for index, stream_key in enumerate(observed)
    }
    epoch_values = {
        f"probe_epoch_{index}": observed[stream_key]
        for index, stream_key in enumerate(observed)
    }
    stream_placeholders = ", ".join(
        f":{name}" for name in stream_values
    )
    expected_epoch = "CASE o.stream_key " + " ".join(
        (
            f"WHEN :probe_stream_{index} "
            f"THEN :probe_epoch_{index}"
        )
        for index in range(len(stream_values))
    ) + " END"
    async with database.transaction():
        await database.execute(
            f"""UPDATE adapter_calibrations AS ac
                  FORCE INDEX (idx_adapter_probe_status)
                STRAIGHT_JOIN outbox_events AS o
                  FORCE INDEX (uk_outbox_dedup)
                  ON o.dedup_key = CONCAT('adapter-probe:', ac.probe_id)
                SET o.status = 'pending',
                    o.attempts = 0,
                    o.sent_at = NULL,
                    o.redis_delivery_epoch = NULL,
                    o.last_error = :replay_reason
                WHERE ac.status = 'queued'
                  AND o.status = 'sent'
                  AND o.stream_key IN ({stream_placeholders})
                  AND (
                    o.redis_delivery_epoch IS NULL
                    OR o.redis_delivery_epoch <> ({expected_epoch})
                  )""",
            {
                **stream_values,
                **epoch_values,
                "replay_reason": (
                    "Redis adapter-probe lane epoch changed; queued delivery "
                    "scheduled for safe replay"
                ),
            },
        )
        # ROW_COUNT is connection-local; the transaction pins this read to
        # the same connection as the UPDATE.
        row = await database.fetch_one("SELECT ROW_COUNT() AS affected")
    if row is None:
        raise RuntimeError(
            "redis_adapter_probe_epoch_replay_count_unavailable"
        )
    affected = int(row["affected"])
    if affected < 0:
        raise RuntimeError(
            "redis_adapter_probe_epoch_replay_count_invalid"
        )
    if affected:
        structured_log(
            "warning",
            "redis_adapter_probe_stream_epochs_reconciled",
            replayed_queued_deliveries=affected,
        )
    return affected


async def reconcile_redis_account_calibration_stream_epochs(
    *,
    platforms=None,
    include_shared: bool = True,
    require_all_owned_lanes: bool = False,
) -> int:
    """Replay only queued calibration deliveries whose Redis lane was lost."""

    bindings = _owned_bindings(
        account_calibration_stream_bindings(
            include_legacy=(
                include_shared
                and settings.legacy_control_stream_drain_enabled
            )
        ),
        platforms=platforms,
        include_shared=include_shared,
    )
    if not bindings:
        return 0
    observed = await _read_stream_lane_epochs(
        bindings,
        event_prefix="account_calibration_stream",
        require_all_owned_lanes=require_all_owned_lanes,
    )
    stream_values = {
        f"calibration_stream_{index}": stream_key
        for index, stream_key in enumerate(observed)
    }
    epoch_values = {
        f"calibration_epoch_{index}": observed[stream_key]
        for index, stream_key in enumerate(observed)
    }
    stream_placeholders = ", ".join(
        f":{name}" for name in stream_values
    )
    expected_epoch = "CASE o.stream_key " + " ".join(
        (
            f"WHEN :calibration_stream_{index} "
            f"THEN :calibration_epoch_{index}"
        )
        for index in range(len(stream_values))
    ) + " END"
    async with database.transaction():
        await database.execute(
            f"""UPDATE account_calibrations AS c
                  FORCE INDEX (idx_account_calibration_status)
                STRAIGHT_JOIN outbox_events AS o
                  FORCE INDEX (uk_outbox_dedup)
                  ON o.dedup_key = CONCAT(
                       'account-calibration:',
                       c.calibration_id
                     )
                SET o.status = 'pending',
                    o.attempts = 0,
                    o.sent_at = NULL,
                    o.redis_delivery_epoch = NULL,
                    o.last_error = :replay_reason
                WHERE c.status = 'queued'
                  AND o.status = 'sent'
                  AND o.stream_key IN ({stream_placeholders})
                  AND (
                    o.redis_delivery_epoch IS NULL
                    OR o.redis_delivery_epoch <> ({expected_epoch})
                  )""",
            {
                **stream_values,
                **epoch_values,
                "replay_reason": (
                    "Redis account-calibration lane epoch changed; queued "
                    "delivery scheduled for safe replay"
                ),
            },
        )
        # ROW_COUNT is connection-local; keep it on the UPDATE connection.
        row = await database.fetch_one("SELECT ROW_COUNT() AS affected")
    if row is None:
        raise RuntimeError(
            "redis_account_calibration_epoch_replay_count_unavailable"
        )
    affected = int(row["affected"])
    if affected < 0:
        raise RuntimeError(
            "redis_account_calibration_epoch_replay_count_invalid"
        )
    if affected:
        structured_log(
            "warning",
            "redis_account_calibration_stream_epochs_reconciled",
            replayed_queued_deliveries=affected,
        )
    return affected


async def reconcile_owned_stream_epochs(
    *,
    platforms=None,
    include_shared: bool = True,
    require_all_owned_lanes: bool = False,
) -> int:
    """Reconcile only the stream lanes assigned to the current process."""

    reconciled = [
        await reconcile_redis_task_stream_epoch(
                platforms=platforms,
                include_shared=include_shared,
                require_all_owned_lanes=require_all_owned_lanes,
            ),
        await reconcile_redis_adapter_probe_stream_epochs(
                platforms=platforms,
                include_shared=include_shared,
                require_all_owned_lanes=require_all_owned_lanes,
            ),
        await reconcile_redis_account_calibration_stream_epochs(
                platforms=platforms,
                include_shared=include_shared,
                require_all_owned_lanes=require_all_owned_lanes,
            ),
    ]
    if include_shared:
        reconciled.append(
            await reconcile_redis_login_request_stream_epoch()
        )
    return sum(reconciled)


async def reconcile_redis_login_request_stream_epoch() -> int:
    """Replay a queued login only when its exact Redis lane was replaced."""

    observed = await read_redis_task_stream_epoch(
        LOGIN_REQUEST_STREAM_KEY
    )
    async with database.transaction():
        await database.execute(
            """UPDATE login_sessions AS session_row
               STRAIGHT_JOIN outbox_events AS outbox_row
                 FORCE INDEX (uk_outbox_dedup)
                 ON outbox_row.dedup_key = CONCAT(
                      :dedup_prefix,
                      session_row.session_id
                    )
               SET outbox_row.status = 'pending',
                   outbox_row.attempts = 0,
                   outbox_row.sent_at = NULL,
                   outbox_row.redis_delivery_epoch = NULL,
                   outbox_row.last_error = :replay_reason
               WHERE session_row.status = 'queued'
                 AND outbox_row.status = 'sent'
                 AND outbox_row.stream_key = :stream_key
                 AND (
                   outbox_row.redis_delivery_epoch IS NULL
                   OR outbox_row.redis_delivery_epoch <> :observed
                 )""",
            {
                "dedup_prefix": LOGIN_REQUEST_OUTBOX_DEDUP_PREFIX,
                "stream_key": LOGIN_REQUEST_STREAM_KEY,
                "observed": observed,
                "replay_reason": (
                    "Redis login-request lane epoch changed; queued "
                    "delivery scheduled for safe replay"
                ),
            },
        )
        row = await database.fetch_one(
            "SELECT ROW_COUNT() AS affected"
        )
    if row is None:
        raise RuntimeError(
            "redis_login_request_epoch_replay_count_unavailable"
        )
    affected = int(row["affected"])
    if affected < 0:
        raise RuntimeError(
            "redis_login_request_epoch_replay_count_invalid"
        )
    if affected:
        structured_log(
            "warning",
            "redis_login_request_stream_epoch_reconciled",
            replayed_queued_deliveries=affected,
        )
    return affected


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
    execution_intent_id: str | None = None,
    execution_intent_hash: str | None = None,
    execution_intent_kind: str | None = None,
    execution_intent_binding_hash: str | None = None,
    requested_actions=None,
    requested_actions_hash: str | None = None,
    requested_action_plan_hash: str | None = None,
    execution_evidence_kind: str | None = None,
    exact_execution_evidence_id: str | None = None,
    oauth_calibration_id: str | None = None,
    weibo_rip_encrypted: str | None = None,
) -> dict[str, str]:
    """Build the Redis-stream message for a lottery task.

    Pure: every value is coerced to ``str`` (Redis stream fields must be
    str/bytes/int/float) and the field set is exactly ``LOTTERY_TASK_FIELDS``.
    """
    selectors = platform_selectors if isinstance(platform_selectors, dict) else {}
    plan = action_plan if isinstance(action_plan, (dict, list)) else {}
    exact_actions = (
        requested_actions
        if isinstance(requested_actions, (list, tuple))
        else []
    )
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
        "execution_intent_id": str(execution_intent_id or ""),
        "execution_intent_hash": str(execution_intent_hash or ""),
        "execution_intent_kind": str(execution_intent_kind or ""),
        "execution_intent_binding_hash": str(
            execution_intent_binding_hash or ""
        ),
        "requested_actions": json.dumps(
            list(exact_actions),
            ensure_ascii=False,
        ),
        "requested_actions_hash": str(requested_actions_hash or ""),
        "requested_action_plan_hash": str(
            requested_action_plan_hash or ""
        ),
        "execution_evidence_kind": str(execution_evidence_kind or ""),
        "exact_execution_evidence_id": str(
            exact_execution_evidence_id or ""
        ),
        "oauth_calibration_id": str(oauth_calibration_id or ""),
        "weibo_rip_encrypted": str(weibo_rip_encrypted or ""),
    }


def _reject_plaintext_weibo_rip(message: dict, stream_key: str) -> None:
    """Keep legacy/plaintext client IP fields out of every durable handoff."""

    if is_task_stream(stream_key) and "weibo_rip" in message:
        raise ValueError("plaintext_weibo_rip_forbidden")


def _validate_task_stream_binding(message: dict, stream_key: str) -> None:
    """Fail closed when a durable envelope violates its transport lane."""

    binding = task_stream_binding_for_key(stream_key)
    if binding is not None:
        _reject_plaintext_weibo_rip(message, stream_key)
        validate_task_stream_message(binding, message)
        return
    probe_binding = adapter_probe_stream_binding_for_key(stream_key)
    if probe_binding is not None:
        validate_adapter_probe_stream_message(probe_binding, message)
        return
    calibration_binding = account_calibration_stream_binding_for_key(
        stream_key
    )
    if calibration_binding is not None:
        validate_account_calibration_stream_message(
            calibration_binding,
            message,
        )
        return
    if is_login_request_stream(stream_key):
        validate_login_request_stream_message(message)


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
    _validate_task_stream_binding(message, stream_key)
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


def _archive_cutoff_datetime(retention_seconds: int) -> datetime:
    if retention_seconds < 3600:
        raise ValueError("outbox_archive_retention_too_short")
    return (
        datetime.now(timezone.utc) - timedelta(seconds=int(retention_seconds))
    ).replace(tzinfo=None)


async def set_outbox_archive_watermark(
    stream_key: str,
    safe_outbox_id: int,
    continuity_epoch: str,
) -> dict[str, object]:
    """Record an operator-observed contiguous Redis/Outbox boundary.

    ``continuity_epoch`` is the global Redis continuity epoch (not the
    platform-lane epoch). Lane epochs intentionally rotate when a stream is
    normally drained by terminal ACK/XDEL, so using one as an archive fence
    would make a healthy, empty lane impossible to archive. The global epoch
    still changes on Redis process replacement or a DB flush. A new epoch
    replaces the old boundary; a same-epoch update is monotonic.
    ``archive_sent_outbox_once`` locks this row before selecting source rows,
    so a concurrent watermark rotation cannot authorize a mixed-epoch batch.
    """

    normalized_stream = str(stream_key or "").strip()
    if not normalized_stream:
        raise ValueError("outbox_archive_stream_required")
    if int(safe_outbox_id) < 0:
        raise ValueError("outbox_archive_watermark_invalid")
    normalized_epoch = str(continuity_epoch or "").strip()
    if not normalized_epoch or len(normalized_epoch) > 128:
        raise ValueError("outbox_archive_epoch_invalid")
    async with database.transaction():
        current = await database.fetch_one(
            """SELECT stream_key, continuity_epoch, safe_outbox_id
                 FROM outbox_archive_watermarks
                WHERE stream_key = :stream_key
                FOR UPDATE""",
            {"stream_key": normalized_stream},
        )
        if current and str(current["continuity_epoch"]) == normalized_epoch:
            next_id = max(int(current["safe_outbox_id"] or 0), int(safe_outbox_id))
            await database.execute(
                """UPDATE outbox_archive_watermarks
                      SET safe_outbox_id = :safe_outbox_id,
                          updated_at = CURRENT_TIMESTAMP
                    WHERE stream_key = :stream_key
                      AND continuity_epoch = :continuity_epoch""",
                {
                    "stream_key": normalized_stream,
                    "continuity_epoch": normalized_epoch,
                    "safe_outbox_id": next_id,
                },
            )
        else:
            next_id = int(safe_outbox_id)
            # Use INSERT IGNORE plus an explicit UPDATE rather than MySQL's
            # deprecated VALUES(col) upsert helper. The row is re-read under
            # the transaction lock above, and the update keeps the watermark
            # replacement atomic for the caller.
            watermark_values = {
                "stream_key": normalized_stream,
                "continuity_epoch": normalized_epoch,
                "safe_outbox_id": next_id,
            }
            await database.execute(
                """INSERT IGNORE INTO outbox_archive_watermarks
                       (stream_key, continuity_epoch, safe_outbox_id)
                   VALUES (:stream_key, :continuity_epoch, :safe_outbox_id)""",
                watermark_values,
            )
            await database.execute(
                """UPDATE outbox_archive_watermarks
                      SET continuity_epoch = :continuity_epoch,
                          safe_outbox_id = :safe_outbox_id,
                          updated_at = CURRENT_TIMESTAMP
                    WHERE stream_key = :stream_key
                      AND continuity_epoch = :continuity_epoch""",
                watermark_values,
            )
    return {
        "stream_key": normalized_stream,
        "continuity_epoch": normalized_epoch,
        "safe_outbox_id": next_id,
    }


async def archive_sent_outbox_once(
    stream_key: str,
    *,
    limit: int = OUTBOX_ARCHIVE_DEFAULT_BATCH,
    retention_seconds: int = OUTBOX_ARCHIVE_DEFAULT_RETENTION_SECONDS,
) -> dict[str, int | str]:
    """Copy and mark one safe, sent Outbox prefix.

    No watermark, Redis epoch, or status mismatch means zero rows are changed.
    The operator-supplied id is a contiguous per-stream boundary; a pending,
    failed, or unsent row at or below that boundary blocks the pass rather
    than allowing a later row to be archived across a gap. Rows remain in
    ``outbox_events`` after copying; a separate purge operation can later
    remove only rows already present in ``outbox_event_archive``.
    """

    if limit <= 0 or limit > 5000:
        raise ValueError("outbox_archive_limit_invalid")
    normalized_stream = str(stream_key or "").strip()
    if not normalized_stream:
        raise ValueError("outbox_archive_stream_required")
    cutoff = _archive_cutoff_datetime(int(retention_seconds))
    # Use the global epoch here. The lane epoch is deliberately allowed to
    # rotate when normal terminal delivery drains a platform stream.
    observed_epoch = await read_redis_task_stream_epoch()
    archived = 0
    async with database.transaction():
        watermark = await database.fetch_one(
            """SELECT continuity_epoch, safe_outbox_id
                 FROM outbox_archive_watermarks
                WHERE stream_key = :stream_key
                FOR UPDATE""",
            {"stream_key": normalized_stream},
        )
        if not watermark:
            return {
                "stream_key": normalized_stream,
                "continuity_epoch": observed_epoch,
                "archived": 0,
            }
        if str(watermark["continuity_epoch"]) != observed_epoch:
            structured_log(
                "warning",
                "outbox_archive_epoch_mismatch",
                stream=normalized_stream,
                watermark_epoch=str(watermark["continuity_epoch"]),
                observed_epoch=observed_epoch,
            )
            return {
                "stream_key": normalized_stream,
                "continuity_epoch": observed_epoch,
                "archived": 0,
            }
        safe_id = int(watermark["safe_outbox_id"] or 0)
        if safe_id <= 0:
            return {
                "stream_key": normalized_stream,
                "continuity_epoch": observed_epoch,
                "archived": 0,
            }
        gap = await database.fetch_one(
            """SELECT COUNT(*) AS cnt
                 FROM outbox_events FORCE INDEX (idx_outbox_archive_ready)
                WHERE stream_key = :stream_key
                  AND id <= :safe_outbox_id
                  AND (status <> 'sent' OR sent_at IS NULL)""",
            {
                "stream_key": normalized_stream,
                "safe_outbox_id": safe_id,
            },
        )
        if int((gap or {}).get("cnt") or 0) > 0:
            structured_log(
                "warning",
                "outbox_archive_watermark_gap",
                stream=normalized_stream,
                continuity_epoch=observed_epoch,
                safe_outbox_id=safe_id,
                gap_count=int(gap["cnt"]),
            )
            return {
                "stream_key": normalized_stream,
                "continuity_epoch": observed_epoch,
                "archived": 0,
            }
        rows = await database.fetch_all(
            """SELECT id, stream_key, payload, dedup_key,
                          redis_delivery_epoch, created_at, sent_at
                     FROM outbox_events FORCE INDEX (idx_outbox_archive_ready)
                    WHERE stream_key = :stream_key
                      AND status = 'sent'
                      AND archived_at IS NULL
                      AND id <= :safe_outbox_id
                      AND sent_at IS NOT NULL
                      AND sent_at < :cutoff
                    ORDER BY id
                    LIMIT :limit
                    FOR UPDATE""",
            {
                "stream_key": normalized_stream,
                "safe_outbox_id": safe_id,
                "cutoff": cutoff,
                "limit": int(limit),
            },
        )
        for row in rows:
            source_id = int(row["id"])
            await database.execute(
                """INSERT INTO outbox_event_archive
                       (source_table, source_id, stream_key, payload,
                        dedup_key, delivery_epoch, created_at, sent_at)
                   VALUES ('outbox_events', :source_id, :stream_key, :payload,
                           :dedup_key, :delivery_epoch, :created_at, :sent_at)
                   ON DUPLICATE KEY UPDATE source_id = source_id""",
                {
                    "source_id": source_id,
                    "stream_key": row["stream_key"],
                    "payload": row["payload"],
                    "dedup_key": row["dedup_key"],
                    "delivery_epoch": row["redis_delivery_epoch"],
                    "created_at": row["created_at"],
                    "sent_at": row["sent_at"],
                },
            )
            await database.execute(
                """UPDATE outbox_events
                      SET archived_at = CURRENT_TIMESTAMP
                    WHERE id = :id
                      AND status = 'sent'
                      AND archived_at IS NULL""",
                {"id": source_id},
            )
            archived += 1
    return {
        "stream_key": normalized_stream,
        "continuity_epoch": observed_epoch,
        "archived": archived,
    }


async def purge_archived_outbox_once(
    stream_key: str,
    *,
    limit: int = OUTBOX_ARCHIVE_DEFAULT_BATCH,
    retention_seconds: int = 90 * 24 * 60 * 60,
) -> int:
    """Delete only rows already copied to the archive table.

    Purging is intentionally separate from archive and has a longer default
    retention.  The archive table remains the durable audit/backup source.
    """

    if limit <= 0 or limit > 5000:
        raise ValueError("outbox_purge_limit_invalid")
    cutoff = _archive_cutoff_datetime(int(retention_seconds))
    return int(
        await execute_affected_rows(
            """DELETE source
                 FROM outbox_events AS source
                 JOIN outbox_event_archive AS archive
                   ON archive.source_table = 'outbox_events'
                  AND archive.source_id = source.id
                WHERE source.stream_key = :stream_key
                  AND source.archived_at IS NOT NULL
                  AND source.archived_at < :cutoff
                ORDER BY source.id
                LIMIT :limit""",
            {
                "stream_key": str(stream_key or "").strip(),
                "cutoff": cutoff,
                "limit": int(limit),
            },
            db=database,
        )
        or 0
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
    stream_key = str(row["stream_key"])
    delivery_epoch = None
    marked_sent = False
    try:
        message = json.loads(row["payload"]) if isinstance(row["payload"], str) else dict(row["payload"])
        _validate_task_stream_binding(message, stream_key)
        if (
            is_task_stream(stream_key)
            or is_adapter_probe_stream(stream_key)
            or is_account_calibration_stream(stream_key)
            or is_login_request_stream(stream_key)
        ):
            delivery_epoch = await read_redis_task_stream_epoch(
                stream_key,
                prepare_delivery=True,
            )
        msg_id = await redis.xadd(stream_key, message)
        if not msg_id:
            raise RuntimeError("xadd returned no id")
        updated = await execute_affected_rows(
            """UPDATE outbox_events
               SET status = 'sent', sent_at = NOW(),
                   redis_delivery_epoch = :delivery_epoch
               WHERE id = :id AND status = 'sending' AND attempts = :attempts""",
            {
                "id": row["id"],
                "attempts": attempts,
                "delivery_epoch": delivery_epoch,
            },
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
        marked_sent = True
        if delivery_epoch is not None:
            # Close the race where Redis restarts after acknowledging XADD but
            # before the DB receipt becomes visible to the epoch reconciler.
            confirmed_epoch = await read_redis_task_stream_epoch(stream_key)
            if confirmed_epoch != delivery_epoch:
                await _reset_epoch_ambiguous_delivery(
                    row_id=row["id"],
                    attempts=attempts,
                    delivery_epoch=delivery_epoch,
                    reason="redis_epoch_changed_during_delivery",
                )
                return False
        return True
    except RedisTaskStreamEpochUnavailable as exc:
        if marked_sent and delivery_epoch is not None:
            await _reset_epoch_ambiguous_delivery(
                row_id=row["id"],
                attempts=attempts,
                delivery_epoch=delivery_epoch,
                reason=str(exc),
            )
        else:
            # Continuity proof failed before XADD, so this is an infrastructure
            # permission/availability problem, not a business delivery attempt.
            # Preserve the retry budget while keeping the row fail-closed.
            await database.execute(
                """UPDATE outbox_events
                   SET status = 'pending',
                       attempts = GREATEST(attempts - 1, 0),
                       last_error = :err
                   WHERE id = :id
                     AND status = 'sending'
                     AND attempts = :attempts""",
                {
                    "id": row["id"],
                    "attempts": attempts,
                    "err": str(exc)[:480],
                },
            )
        structured_log(
            "error",
            "outbox_task_stream_epoch_unavailable",
            outbox_id=row["id"],
            error=str(exc),
        )
        return False
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


async def _reset_epoch_ambiguous_delivery(
    *,
    row_id,
    attempts: int,
    delivery_epoch: str,
    reason: str,
) -> bool:
    """Return only this still-owned ambiguous receipt to pending."""

    updated = await execute_affected_rows(
        """UPDATE outbox_events
           SET status = 'pending',
               sent_at = NULL,
               redis_delivery_epoch = NULL,
               last_error = :reason
           WHERE id = :id
             AND status = 'sent'
             AND attempts = :attempts
             AND redis_delivery_epoch = :delivery_epoch""",
        {
            "id": row_id,
            "attempts": attempts,
            "delivery_epoch": delivery_epoch,
            "reason": reason[:480],
        },
        db=database,
    )
    if updated:
        structured_log(
            "warning",
            "outbox_delivery_requeued_after_redis_epoch_change",
            outbox_id=row_id,
            attempts=attempts,
        )
    return bool(updated)


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
        current_data = dict(current)
        if await settle_terminal_adapter_probe_delivery_failure(
            current_data,
            attempts,
            error,
            db=database,
        ):
            return
        if await settle_terminal_account_calibration_delivery_failure(
            current_data,
            attempts,
            error,
        ):
            return
        if await settle_terminal_login_request_delivery_failure(
            current_data,
            attempts,
            error,
            db=database,
        ):
            return
        stream_key = str(current["stream_key"] or "").strip()

        task_id = str(current["dedup_key"] or "").strip()
        if not is_task_stream(stream_key) or not task_id:
            return
        task = await database.fetch_one(
            """SELECT tr.task_id, tr.account_id, tr.lottery_id, tr.status,
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
        try:
            operation_kind = (
                _terminal_failure_lease_operation_kind(
                    task,
                    dict(current).get("payload"),
                    stream_key=stream_key,
                )
            )
        except ValueError as lease_exc:
            # The task and lottery can still converge to a safe non-executing
            # state, but an ambiguously typed lease must remain fenced until
            # expiry/operator reconciliation rather than being broadly
            # released.
            structured_log(
                "error",
                "outbox_terminal_failure_lease_binding_invalid",
                task_id=task_id,
                stream=stream_key,
                error=str(lease_exc),
            )
        else:
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
                    "lease_generation": task[
                        "account_lease_generation"
                    ],
                    "operation_kind": operation_kind,
                    "task_id": task_id,
                },
            )


def _terminal_failure_lease_operation_kind(
    task,
    payload,
    *,
    stream_key: str,
) -> str:
    """Derive the exact lease kind without trusting task_mode alone."""

    task_mode = str(task["task_mode"] or "").strip().casefold()
    if task_mode != "real_run":
        if task_mode not in {"dry_run", "shadow_run"}:
            raise ValueError("task_mode_invalid")
        return task_mode

    try:
        message = (
            json.loads(payload)
            if isinstance(payload, str)
            else dict(payload or {})
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("execution_intent_message_invalid") from exc
    message_kind = str(
        message.get("execution_intent_kind") or ""
    ).strip()
    binding_kind = str(
        task["execution_intent_kind"] or ""
    ).strip()
    if binding_kind:
        if message_kind != binding_kind:
            raise ValueError("execution_intent_kind_mismatch")
        execution_intent_kind = binding_kind
    else:
        if (
            str(stream_key) != LEGACY_TASK_STREAM_KEY
            or message_kind
        ):
            raise ValueError("execution_intent_kind_missing")
        execution_intent_kind = (
            LEGACY_FULL_EXECUTION_INTENT_KIND
        )
    return lease_operation_kind_for_execution_intent(
        execution_intent_kind
    )


async def _relay_by_id(row_id) -> bool:
    return await _relay_by_id_outcome(row_id) == "sent"


async def _relay_by_id_outcome(row_id) -> str:
    """Distinguish a harmless claim race from a failed owned delivery."""

    claimed = await _claim_row(row_id)
    if claimed is None:
        return "unclaimed"
    return "sent" if await _deliver_claimed(claimed) else "failed"


def _outbox_stream_scope(
    *,
    stream_key: str | None,
    stream_keys: tuple[str, ...] = (),
    exclude_stream_keys: tuple[str, ...],
) -> tuple[str, dict[str, str]]:
    """Build a parameterized stream predicate for one independent relay lane."""

    selected_scope_count = sum(
        (
            stream_key is not None,
            bool(stream_keys),
            bool(exclude_stream_keys),
        )
    )
    if selected_scope_count > 1:
        raise ValueError("outbox_stream_scope_is_ambiguous")
    if stream_key is not None:
        return " AND stream_key = :stream_key", {
            "stream_key": str(stream_key),
        }
    normalized_streams = tuple(
        dict.fromkeys(
            str(value).strip()
            for value in stream_keys
            if str(value).strip()
        )
    )
    if stream_keys and not normalized_streams:
        raise ValueError("outbox_stream_scope_is_empty")
    if normalized_streams:
        placeholders = []
        values = {}
        for index, value in enumerate(normalized_streams):
            name = f"selected_stream_{index}"
            placeholders.append(f":{name}")
            values[name] = value
        return (
            f" AND stream_key IN ({', '.join(placeholders)})",
            values,
        )
    normalized_exclusions = tuple(
        dict.fromkeys(
            str(value).strip()
            for value in exclude_stream_keys
            if str(value).strip()
        )
    )
    if not normalized_exclusions:
        return "", {}
    placeholders = []
    values = {}
    for index, value in enumerate(normalized_exclusions):
        name = f"excluded_stream_{index}"
        placeholders.append(f":{name}")
        values[name] = value
    return (
        f" AND stream_key NOT IN ({', '.join(placeholders)})",
        values,
    )


async def reclaim_stale_sending(
    threshold_seconds: int = OUTBOX_SENDING_RECLAIM_SECONDS,
    *,
    stream_key: str | None = None,
    stream_keys: tuple[str, ...] = (),
    exclude_stream_keys: tuple[str, ...] = (),
    limit: int = OUTBOX_RECLAIM_BATCH,
) -> int:
    """Return rows stuck in ``sending`` (claimer crashed) to ``pending``."""
    if limit <= 0:
        raise ValueError("outbox_reclaim_limit_invalid")
    stream_clause, stream_values = _outbox_stream_scope(
        stream_key=stream_key,
        stream_keys=stream_keys,
        exclude_stream_keys=exclude_stream_keys,
    )
    result = await execute_affected_rows(
        """UPDATE outbox_events SET status = 'pending'
           WHERE status = 'sending'
             AND updated_at < (NOW() - INTERVAL :sec SECOND)"""
        + stream_clause
        + " ORDER BY id LIMIT :limit",
        {"sec": threshold_seconds, "limit": int(limit), **stream_values},
        db=database,
    )
    return int(result or 0)


async def flush_pending_outbox(
    limit: int = OUTBOX_BATCH,
    *,
    stream_key: str | None = None,
    exclude_stream_keys: tuple[str, ...] = (),
    include_delivery_failures: bool = False,
) -> dict[str, int]:
    """Relay one bounded lane of pending rows, preserving the legacy API."""

    stream_clause, stream_values = _outbox_stream_scope(
        stream_key=stream_key,
        exclude_stream_keys=exclude_stream_keys,
    )
    rows = await database.fetch_all(
        """SELECT id FROM outbox_events
           WHERE status = 'pending'"""
        + stream_clause
        + " ORDER BY id LIMIT :limit",
        {"limit": limit, **stream_values},
    )
    sent = 0
    delivery_failures = 0
    for row in rows:
        outcome = await _relay_by_id_outcome(row["id"])
        if outcome == "sent":
            sent += 1
        elif outcome == "failed":
            delivery_failures += 1
    result = {"scanned": len(rows), "sent": sent}
    if include_delivery_failures:
        result["delivery_failures"] = delivery_failures
    return result


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


async def start_outbox_dispatcher(
    *,
    platforms=None,
    include_shared: bool = True,
):
    """Run only the durable lanes assigned to this Core process.

    Shared ownership includes legacy delivery, non-platform events, stale
    claim recovery and epoch reconciliation. Platform runners must leave that
    ownership with the shared Core API process.
    """

    isolated_ownership = platforms is not None
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

    all_task_bindings = task_stream_bindings(include_legacy=True)
    delivery_bindings = task_stream_bindings(
        include_legacy=settings.legacy_task_stream_drain_enabled
    )
    all_probe_bindings = adapter_probe_stream_bindings(
        include_legacy=True
    )
    probe_delivery_bindings = adapter_probe_stream_bindings(
        include_legacy=settings.legacy_control_stream_drain_enabled
    )
    all_calibration_bindings = account_calibration_stream_bindings(
        include_legacy=True
    )
    calibration_delivery_bindings = (
        account_calibration_stream_bindings(
            include_legacy=settings.legacy_control_stream_drain_enabled
        )
    )
    all_durable_bindings = (
        *all_task_bindings,
        *all_probe_bindings,
        *all_calibration_bindings,
    )
    all_delivery_bindings = tuple(
        binding
        for binding in (
            *delivery_bindings,
            *probe_delivery_bindings,
            *calibration_delivery_bindings,
        )
        if (
            binding.platform in selected_platforms
            or (include_shared and binding.platform is None)
        )
    )
    excluded_durable_streams = tuple(
        binding.stream_key
        for binding in all_durable_bindings
    ) + (LOGIN_REQUEST_STREAM_KEY,)
    platform_delivery_streams = tuple(
        dict.fromkeys(
            binding.stream_key
            for binding in all_delivery_bindings
            if binding.platform is not None
        )
    )
    all_platform_durable_streams = tuple(
        dict.fromkeys(
            binding.stream_key
            for binding in all_durable_bindings
            if binding.platform is not None
        )
    )
    tasks = [
        asyncio.create_task(
            _outbox_delivery_loop(
                lane=binding.stream_key,
                stream_key=binding.stream_key,
                initial_delay_seconds=_outbox_lane_initial_delay(
                    binding.stream_key
                ),
                fail_closed=isolated_ownership,
            ),
            name=f"outbox-relay:{binding.stream_key}",
        )
        for binding in all_delivery_bindings
    ]
    for reclaim_spec in _outbox_reclaim_lane_specs(
        platform_lane=(
            "platform:" + ",".join(sorted(selected_platforms))
        ),
        platform_stream_keys=platform_delivery_streams,
        all_platform_stream_keys=all_platform_durable_streams,
        include_shared=include_shared,
        isolated_ownership=isolated_ownership,
    ):
        tasks.append(
            asyncio.create_task(
                _outbox_reclaim_loop(
                    lane=str(reclaim_spec["lane"]),
                    stream_keys=tuple(reclaim_spec["stream_keys"]),
                    exclude_stream_keys=tuple(
                        reclaim_spec["exclude_stream_keys"]
                    ),
                    initial_delay_seconds=_outbox_lane_initial_delay(
                        f"reclaim:{reclaim_spec['lane']}",
                        window_seconds=OUTBOX_RECLAIM_POLL_SECONDS,
                    ),
                    fail_closed=bool(reclaim_spec["fail_closed"]),
                ),
                name=f"outbox-reclaim:{reclaim_spec['lane']}",
            )
        )
    if include_shared:
        tasks.extend(
            (
            asyncio.create_task(
                _outbox_delivery_loop(
                    lane=LOGIN_REQUEST_STREAM_KEY,
                    stream_key=LOGIN_REQUEST_STREAM_KEY,
                    initial_delay_seconds=_outbox_lane_initial_delay(
                        LOGIN_REQUEST_STREAM_KEY
                    ),
                ),
                name=f"outbox-relay:{LOGIN_REQUEST_STREAM_KEY}",
            ),
            asyncio.create_task(
                _outbox_delivery_loop(
                    lane="shared-non-durable-control",
                    exclude_stream_keys=excluded_durable_streams,
                    initial_delay_seconds=_outbox_lane_initial_delay(
                        "shared-non-durable-control"
                    ),
                ),
                name="outbox-relay:shared-non-durable-control",
            ),
            )
        )
    tasks.append(
        asyncio.create_task(
            _outbox_reconciliation_loop(
                platforms=tuple(selected_platforms),
                include_shared=include_shared,
                fail_closed=isolated_ownership,
            ),
            name="outbox-reconciliation",
        )
    )
    if settings.outbox_archive_enabled and not isolated_ownership:
        archive_streams = tuple(
            dict.fromkeys(
                binding.stream_key
                for binding in all_durable_bindings
                if binding.platform is None or binding.platform in selected_platforms
            )
        )
        tasks.append(
            asyncio.create_task(
                _outbox_archive_loop(stream_keys=archive_streams),
                name="outbox-archive",
            )
        )
    if not tasks:
        raise ValueError("outbox_dispatcher_has_no_owned_lanes")
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _outbox_delivery_loop(
    *,
    lane: str,
    stream_key: str | None = None,
    exclude_stream_keys: tuple[str, ...] = (),
    initial_delay_seconds: float = 0.0,
    fail_closed: bool = False,
) -> None:
    """Relay one DB outbox lane without waiting on another platform."""

    if initial_delay_seconds < 0 or initial_delay_seconds >= OUTBOX_POLL_SECONDS:
        raise ValueError("outbox_lane_initial_delay_out_of_range")
    if initial_delay_seconds:
        await asyncio.sleep(initial_delay_seconds)
    while True:
        try:
            result = await flush_pending_outbox(
                stream_key=stream_key,
                exclude_stream_keys=exclude_stream_keys,
                include_delivery_failures=fail_closed,
            )
            if (
                fail_closed
                and int(result.get("delivery_failures") or 0) > 0
            ):
                raise RuntimeError(
                    "isolated_outbox_delivery_incomplete"
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            structured_log(
                "error",
                "outbox_lane_dispatcher_error",
                lane=lane,
                stream=stream_key or "non-task",
                exception=exc,
            )
            if fail_closed:
                raise
        await asyncio.sleep(OUTBOX_POLL_SECONDS)


async def _outbox_archive_loop(*, stream_keys: tuple[str, ...]) -> None:
    """Run opt-in, watermark-gated archive passes from the shared Core."""

    if not stream_keys:
        return
    cycle_index = 0
    await asyncio.sleep(
        _outbox_lane_initial_delay(
            "outbox-archive",
            window_seconds=min(
                float(settings.outbox_archive_interval_seconds),
                60.0,
            ),
        )
    )
    while True:
        for stream_key in stream_keys:
            try:
                await archive_sent_outbox_once(
                    stream_key,
                    limit=settings.outbox_archive_batch,
                    retention_seconds=settings.outbox_archive_retention_seconds,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                structured_log(
                    "error",
                    "outbox_archive_lane_error",
                    stream=stream_key,
                    exception=exc,
                )
        sleep_seconds = _outbox_reconciliation_period_seconds(
            "outbox-archive",
            cycle_index,
            base_seconds=float(settings.outbox_archive_interval_seconds),
            jitter_seconds=min(
                30.0,
                float(settings.outbox_archive_interval_seconds) / 5.0,
            ),
        )
        cycle_index += 1
        await asyncio.sleep(sleep_seconds)


def _resolve_outbox_instance_identity(
    *,
    environment: Mapping[str, str] | None = None,
    hostname: str | None = None,
    process_id: int | None = None,
) -> str:
    """Resolve a non-secret identity that is stable for this process.

    Operators may provide a replica-unique ``DPMS_OUTBOX_INSTANCE_ID``. The
    existing Core runner identity is a lower-priority compatibility source.
    Otherwise hostname plus PID separates colocated processes without reading
    credentials or other secret-bearing configuration.
    """

    values = os.environ if environment is None else environment
    for variable, source in (
        ("DPMS_OUTBOX_INSTANCE_ID", "outbox"),
        ("DPMS_CORE_RUNNER_INSTANCE_ID", "core-runner"),
    ):
        configured = str(values.get(variable, "") or "").strip()
        if configured:
            return f"{source}:{configured}"

    resolved_hostname = str(
        socket.gethostname() if hostname is None else hostname
    ).strip()
    resolved_process_id = (
        os.getpid() if process_id is None else int(process_id)
    )
    return (
        f"process:{resolved_hostname or 'unknown-host'}:"
        f"{resolved_process_id}"
    )


def _current_outbox_instance_identity() -> str:
    """Return one process-lifetime identity, refreshing after a fork."""

    global _OUTBOX_INSTANCE_IDENTITY_CACHE

    process_id = os.getpid()
    cached = _OUTBOX_INSTANCE_IDENTITY_CACHE
    if cached is None or cached[0] != process_id:
        cached = (
            process_id,
            _resolve_outbox_instance_identity(process_id=process_id),
        )
        _OUTBOX_INSTANCE_IDENTITY_CACHE = cached
    return cached[1]


def _outbox_lane_initial_delay(
    lane: str,
    *,
    window_seconds: float = OUTBOX_POLL_SECONDS,
    instance_identity: str | None = None,
) -> float:
    """Return a stable sub-interval phase for one instance-owned relay lane.

    Python's process-randomized ``hash`` cannot be used here: it would make
    tests and phase placement unpredictable. A bounded SHA-256-derived
    millisecond phase spreads startup queries without delaying any lane beyond
    one normal poll interval. Including the process/replica identity prevents
    two replicas of the same lane from sharing the same schedule by design.
    """

    if window_seconds <= 0:
        raise ValueError("outbox_lane_jitter_window_invalid")
    identity = str(
        _current_outbox_instance_identity()
        if instance_identity is None
        else instance_identity
    ).strip()
    if not identity:
        raise ValueError("outbox_instance_identity_invalid")
    window_milliseconds = max(1, int(window_seconds * 1000))
    digest = hashlib.sha256(
        f"{identity}\0{lane}".encode("utf-8")
    ).digest()
    phase_milliseconds = (
        int.from_bytes(digest[:8], byteorder="big")
        % window_milliseconds
    )
    return phase_milliseconds / 1000.0


def outbox_reconciliation_startup_phase_seconds(
    platform: str,
    *,
    window_seconds: float = OUTBOX_POLL_SECONDS,
) -> float:
    """Return the stable startup phase for one exact platform runner."""

    selected = tuple(normalize_platform_scope((platform,)))
    if len(selected) != 1:
        raise ValueError(
            "outbox_reconciliation_startup_platform_must_be_exact"
        )
    return _outbox_lane_initial_delay(
        f"startup-reconciliation:{selected[0]}",
        window_seconds=window_seconds,
    )


def _outbox_reconciliation_period_seconds(
    lane: str,
    cycle_index: int,
    *,
    base_seconds: float = OUTBOX_POLL_SECONDS,
    jitter_seconds: float = OUTBOX_RECONCILIATION_JITTER_SECONDS,
    instance_identity: str | None = None,
) -> float:
    """Return one process-stable, bounded reconciliation period.

    Hashing the instance identity, immutable lane name and cycle number gives
    each replica a reproducible in-process sequence while preventing same-lane
    replicas and different platform/family loops from converging.
    """

    if cycle_index < 0:
        raise ValueError("outbox_reconciliation_cycle_invalid")
    if base_seconds <= 0:
        raise ValueError("outbox_reconciliation_period_invalid")
    if jitter_seconds < 0 or jitter_seconds >= base_seconds:
        raise ValueError("outbox_reconciliation_jitter_invalid")
    jitter_milliseconds = int(jitter_seconds * 1000)
    if jitter_milliseconds == 0:
        return float(base_seconds)
    identity = str(
        _current_outbox_instance_identity()
        if instance_identity is None
        else instance_identity
    ).strip()
    if not identity:
        raise ValueError("outbox_instance_identity_invalid")
    digest = hashlib.sha256(
        f"{identity}\0{lane}\0{cycle_index}".encode("utf-8")
    ).digest()
    offset_milliseconds = (
        int.from_bytes(digest[:8], byteorder="big")
        % (2 * jitter_milliseconds + 1)
    ) - jitter_milliseconds
    return float(base_seconds) + (offset_milliseconds / 1000.0)


def _outbox_reclaim_lane_specs(
    *,
    platform_lane: str = "platform",
    platform_stream_keys: tuple[str, ...],
    all_platform_stream_keys: tuple[str, ...],
    include_shared: bool,
    isolated_ownership: bool,
) -> tuple[dict[str, object], ...]:
    """Describe non-overlapping stale-claim recovery ownership."""

    owned_platform_streams = tuple(dict.fromkeys(platform_stream_keys))
    every_platform_stream = tuple(
        dict.fromkeys(all_platform_stream_keys)
    )
    specs: list[dict[str, object]] = []
    if isolated_ownership and owned_platform_streams:
        specs.append(
            {
                "lane": platform_lane,
                "stream_keys": owned_platform_streams,
                "exclude_stream_keys": (),
                "fail_closed": True,
            }
        )
    if include_shared:
        specs.append(
            {
                "lane": "shared",
                "stream_keys": (),
                "exclude_stream_keys": (
                    every_platform_stream if isolated_ownership else ()
                ),
                "fail_closed": False,
            }
        )
    return tuple(specs)


async def _outbox_reclaim_loop(
    *,
    lane: str = "global",
    stream_keys: tuple[str, ...] = (),
    exclude_stream_keys: tuple[str, ...] = (),
    initial_delay_seconds: float = 0.0,
    fail_closed: bool = False,
) -> None:
    """Reclaim stale claims only for the lane owned by this Core process."""

    if (
        initial_delay_seconds < 0
        or initial_delay_seconds >= OUTBOX_RECLAIM_POLL_SECONDS
    ):
        raise ValueError("outbox_reclaim_initial_delay_out_of_range")
    if initial_delay_seconds:
        await asyncio.sleep(initial_delay_seconds)

    reclaim_scope = {}
    if stream_keys:
        reclaim_scope["stream_keys"] = stream_keys
    if exclude_stream_keys:
        reclaim_scope["exclude_stream_keys"] = exclude_stream_keys
    while True:
        try:
            await reclaim_stale_sending(**reclaim_scope)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            structured_log(
                "error",
                "outbox_reclaim_loop_error",
                lane=lane,
                exception=exc,
            )
            if fail_closed:
                raise
        await asyncio.sleep(OUTBOX_RECLAIM_POLL_SECONDS)


def _outbox_reconciliation_lane_specs(
    *,
    platforms,
    include_shared: bool,
) -> tuple[dict[str, object], ...]:
    """Describe independently scheduled reconciliation ownership.

    Platform scopes are deliberately expanded to one exact platform per spec.
    That keeps both the stable phase and a failure local to the owning
    platform/family instead of recreating a synchronized multi-platform batch.
    """

    specs: list[dict[str, object]] = []
    for platform in sorted(_owned_platforms(platforms)):
        for family in (
            "task-stream",
            "adapter-probe",
            "account-calibration",
        ):
            specs.append(
                {
                    "lane": f"{platform}:{family}",
                    "family": family,
                    "platforms": (platform,),
                    "include_shared": False,
                }
            )
    if include_shared:
        if settings.legacy_task_stream_drain_enabled:
            specs.append(
                {
                    "lane": "shared:legacy-task-stream",
                    "family": "task-stream",
                    "platforms": (),
                    "include_shared": True,
                }
            )
        if settings.legacy_control_stream_drain_enabled:
            for family in ("adapter-probe", "account-calibration"):
                specs.append(
                    {
                        "lane": f"shared:legacy-{family}",
                        "family": family,
                        "platforms": (),
                        "include_shared": True,
                    }
                )
        specs.extend(
            (
                {
                    "lane": "shared:login-request",
                    "family": "login-request",
                    "platforms": (),
                    "include_shared": True,
                },
                {
                    "lane": "shared:orphan-lock",
                    "family": "orphan-lock",
                    "platforms": (),
                    "include_shared": True,
                },
            )
        )
    return tuple(specs)


async def _reconcile_outbox_lane_once(
    *,
    family: str,
    platforms,
    include_shared: bool,
    require_all_owned_lanes: bool,
) -> None:
    if family == "task-stream":
        await reconcile_redis_task_stream_epoch(
            platforms=platforms,
            include_shared=include_shared,
            require_all_owned_lanes=require_all_owned_lanes,
        )
        return
    if family == "adapter-probe":
        await reconcile_redis_adapter_probe_stream_epochs(
            platforms=platforms,
            include_shared=include_shared,
            require_all_owned_lanes=require_all_owned_lanes,
        )
        return
    if family == "account-calibration":
        await reconcile_redis_account_calibration_stream_epochs(
            platforms=platforms,
            include_shared=include_shared,
            require_all_owned_lanes=require_all_owned_lanes,
        )
        return
    if family == "login-request":
        await reconcile_redis_login_request_stream_epoch()
        return
    if family == "orphan-lock":
        # This reconciliation is DB-only and must keep running through a Redis
        # outage or an INFO ACL regression.
        await reconcile_orphaned_locks()
        return
    raise ValueError("outbox_reconciliation_family_unknown")


def _outbox_reconciliation_error_event(family: str) -> str:
    return {
        "task-stream": "redis_task_stream_epoch_reconciliation_error",
        "adapter-probe": (
            "redis_adapter_probe_stream_epoch_reconciliation_error"
        ),
        "account-calibration": (
            "redis_account_calibration_stream_epoch_reconciliation_error"
        ),
        "login-request": (
            "redis_login_request_stream_epoch_reconciliation_error"
        ),
        "orphan-lock": "outbox_orphan_lock_reconciliation_error",
    }.get(family, "outbox_reconciliation_lane_error")


async def _outbox_reconciliation_lane_loop(
    *,
    lane: str,
    family: str,
    platforms,
    include_shared: bool,
    require_all_owned_lanes: bool,
    initial_delay_seconds: float,
) -> None:
    """Run one reconciliation family without coupling sibling lane progress."""

    if (
        initial_delay_seconds < 0
        or initial_delay_seconds >= OUTBOX_POLL_SECONDS
    ):
        raise ValueError(
            "outbox_reconciliation_initial_delay_out_of_range"
        )
    if initial_delay_seconds:
        await asyncio.sleep(initial_delay_seconds)
    cycle_index = 0
    while True:
        try:
            await _reconcile_outbox_lane_once(
                family=family,
                platforms=platforms,
                include_shared=include_shared,
                require_all_owned_lanes=require_all_owned_lanes,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Reconciliation is conservative recovery: a failed pass never
            # authorizes delivery. Shared/control ownership keeps this lane
            # retrying so siblings progress; an isolated platform runner
            # propagates the failure so its health marker is withdrawn.
            structured_log(
                "error",
                _outbox_reconciliation_error_event(family),
                lane=lane,
                platform=(
                    platforms[0]
                    if len(platforms) == 1
                    else "shared"
                ),
                exception=exc,
            )
            if require_all_owned_lanes:
                raise
        delay = _outbox_reconciliation_period_seconds(
            lane,
            cycle_index,
        )
        cycle_index += 1
        await asyncio.sleep(delay)


async def _outbox_reconciliation_loop(
    *,
    platforms=None,
    include_shared: bool = True,
    fail_closed: bool = False,
) -> None:
    """Supervise independently phased reconciliation lanes.

    ``fail_closed`` keeps strict all-owned-stream validation and propagates a
    failed platform pass so that runner becomes unhealthy. Shared/control
    lanes retry locally because delivery performs its own continuity proof
    and fails before XADD.
    """

    specs = _outbox_reconciliation_lane_specs(
        platforms=platforms,
        include_shared=include_shared,
    )
    if not specs:
        raise ValueError("outbox_reconciliation_has_no_owned_lanes")
    tasks = tuple(
        asyncio.create_task(
            _outbox_reconciliation_lane_loop(
                lane=str(spec["lane"]),
                family=str(spec["family"]),
                platforms=tuple(spec["platforms"]),
                include_shared=bool(spec["include_shared"]),
                require_all_owned_lanes=(
                    fail_closed and bool(spec["platforms"])
                ),
                initial_delay_seconds=_outbox_lane_initial_delay(
                    f"reconciliation:{spec['lane']}"
                ),
            ),
            name=f"outbox-reconcile:{spec['lane']}",
        )
        for spec in specs
    )
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
