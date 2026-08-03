import asyncio
import hashlib
import importlib
import json
import os
import re
import stat
import threading
import time
import unicodedata
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from app.adapters.registry import get_adapter
from app.adapter_config import load_selector_config
from app.action_plan import (
    BILIBILI_ACTION_ORDER,
    BILIBILI_API_EXECUTION_PATH,
    DOUYIN_ACTION_ORDER,
    WEIBO_ACTION_ORDER,
    WEIBO_MAX_UNIQUE_HANDLES,
    WEIBO_OAUTH_EXECUTION_PATH,
    XIAOHONGSHU_REQUIRED_ACTIONS,
    ActionPlanV2Error,
    canonical_json_bytes,
    compute_bilibili_api_config_hash,
    compute_target_hash,
    validate_action_plan_v2,
)
from app.browser_pool import BrowserPool
from app.db import database, execute_affected_rows, redis
from app.event_store.service import record_event
from app.evidence_storage import (
    SHADOW_EVIDENCE_DIR,
    TASK_FAILURE_EVIDENCE_DIR,
)
from app.platform_modules.base import PlatformRoutingError
from app.platform_modules.errors import (
    BilibiliActionSettlementFailed,
    BilibiliForwardedTargetRequiresReview,
    ExternalActionOutcomeUnknown,
)
from app.platform_modules.registry import get_platform_module, registered_platforms
from app.platform_modules.services import TaskExecutionServices
from app.real_run_gate import (
    RealRunGateBlocked,
    enforce_real_run_gate,
    open_unknown_outcome_breaker,
    platform_real_run_block_reason,
)
from app.services.external_action_intents import (
    ExternalActionIntentBlocked,
    StartedActionIntent,
    mark_action_intent_unknown,
    prepare_and_start_action_intent,
    renew_account_operation_lease,
    settle_action_intent,
)
from app.safety import (
    AccountStatusPersistenceFailed,
    detect_page_risk,
    ensure_account_can_run,
    set_account_status,
)
from app.task_streams import (
    LEGACY_TASK_GROUP_NAME,
    LEGACY_TASK_STREAM_KEY,
    SAFE_TERMINAL_FANOUT_ACK_DELETE_LUA,
    SAFE_TERMINAL_TASK_ACK_DELETE_LUA,
    TaskStreamBinding,
    task_stream_bindings,
    validate_task_stream_message,
)
from app.utils.log import structured_log
from shared.redis_consumer_groups import verify_redis_consumer_group
from app.worker_identity import WORKER_ID
from app.utils.navigation_safety import (
    install_main_frame_navigation_guard,
    validated_platform_canonical_uri,
    validated_platform_content_url,
    validated_platform_navigation_url,
)
from app.utils.cookies import credential_to_cookie_header, inject_account_cookies
from app.utils.crypto import CREDENTIAL_AAD, cookie_vault
from shared.execution_contracts import (
    LEGACY_FULL_EXECUTION_INTENT_KIND,
    lease_operation_kind_for_execution_intent,
)
from shared.platform_scope import normalize_platform_scope


try:
    import fcntl
except ImportError:  # pragma: no cover - browser workers are deployed on Linux
    fcntl = None


# Compatibility names stay available to tests/operational tooling, but no
# platform runtime is imported while the Worker process boots.  A name is
# resolved only when its platform path is selected (or a caller explicitly
# asks for that legacy attribute).
_LAZY_PLATFORM_SYMBOLS = {
    "API_PREFLIGHT_KIND": ("app.bilibili.preflight", "API_PREFLIGHT_KIND"),
    "bilibili_author_handle": (
        "app.bilibili.preflight",
        "bilibili_author_handle",
    ),
    "run_readonly_api_preflight": (
        "app.bilibili.preflight",
        "run_readonly_api_preflight",
    ),
    "BilibiliApiActionOutcomeUnknown": (
        "app.bilibili.client",
        "BilibiliApiActionOutcomeUnknown",
    ),
    "BilibiliApiClient": ("app.bilibili.client", "BilibiliApiClient"),
    "BiliEngineConfig": ("app.bilibili.config", "BiliEngineConfig"),
    "BilibiliApiExecutor": ("app.bilibili.executor", "BilibiliApiExecutor"),
    "API_TO_DPMS_PHASE": ("app.bilibili.runtime", "API_TO_DPMS_PHASE"),
    "account_status_for_results": (
        "app.bilibili.runtime",
        "account_status_for_results",
    ),
    "dpms_phases_to_api_actions": (
        "app.bilibili.runtime",
        "dpms_phases_to_api_actions",
    ),
    "extract_bilibili_dynamic_id": (
        "app.bilibili.runtime",
        "extract_bilibili_dynamic_id",
    ),
    "parse_detail_card": ("app.bilibili.runtime", "parse_detail_card"),
    "validate_card_for_actions": (
        "app.bilibili.runtime",
        "validate_card_for_actions",
    ),
    "WeiboApiActionOutcomeUnknown": (
        "app.weibo.client",
        "WeiboApiActionOutcomeUnknown",
    ),
    "WeiboApiClient": ("app.weibo.client", "WeiboApiClient"),
    "WeiboApiRejected": ("app.weibo.client", "WeiboApiRejected"),
    "build_weibo_mutation_request": (
        "app.weibo.client",
        "build_weibo_mutation_request",
    ),
    "status_identifier_from_canonical_uri": (
        "app.weibo.client",
        "status_identifier_from_canonical_uri",
    ),
    "WeiboOAuthCredentialError": (
        "app.weibo.credentials",
        "WeiboOAuthCredentialError",
    ),
    "decrypt_weibo_rip": ("app.weibo.credentials", "decrypt_weibo_rip"),
    "parse_weibo_oauth_credential": (
        "app.weibo.credentials",
        "parse_weibo_oauth_credential",
    ),
    "weibo_rip_required": (
        "app.weibo.credentials",
        "weibo_rip_required",
    ),
    "WeiboExecutionOutcomeUnknown": (
        "app.weibo.executor",
        "WeiboExecutionOutcomeUnknown",
    ),
    "WeiboOAuthExecutor": ("app.weibo.executor", "WeiboOAuthExecutor"),
    "materialize_for_shadow_task": (
        "app.services.execution_evidence",
        "materialize_for_shadow_task",
    ),
}


def _platform_runtime_symbol(name: str):
    cached = globals().get(name)
    if cached is not None:
        return cached
    try:
        module_name, export_name = _LAZY_PLATFORM_SYMBOLS[name]
    except KeyError as exc:
        raise AttributeError(name) from exc
    value = getattr(importlib.import_module(module_name), export_name)
    globals()[name] = value
    return value


def __getattr__(name: str):
    return _platform_runtime_symbol(name)


def _task_execution_services() -> TaskExecutionServices:
    """Build a narrow, immutable facade from the current shared services.

    Constructing this at dispatch time preserves existing test/operations
    monkey-patching of platform-neutral infrastructure without exposing the
    rest of this orchestrator module to a platform implementation.
    """

    return TaskExecutionServices(
        database=database,
        WORKER_ID=WORKER_ID,
        ActionPlanV2Error=ActionPlanV2Error,
        AccountStatusPersistenceFailed=AccountStatusPersistenceFailed,
        RealRunGateBlocked=RealRunGateBlocked,
        StartedActionIntent=StartedActionIntent,
        TaskClaimConflict=TaskClaimConflict,
        TaskOwnershipLost=TaskOwnershipLost,
        TaskSettlementUnconfirmed=TaskSettlementUnconfirmed,
        _claim_positive_int=_claim_positive_int,
        await_safety_settlement=await_safety_settlement,
        canonical_json_bytes=canonical_json_bytes,
        compute_target_hash=compute_target_hash,
        credential_to_cookie_header=credential_to_cookie_header,
        emergency_stop_real_runs_and_revoke_lease=(
            emergency_stop_real_runs_and_revoke_lease
        ),
        enforce_task_real_run_gate=enforce_task_real_run_gate,
        execute_browser_observation_shadow=(
            execute_browser_observation_shadow
        ),
        execute_real_task=execute_real_task,
        gate_execution_action_plan=gate_execution_action_plan,
        gate_requested_actions=gate_requested_actions,
        get_latest_phase=get_latest_phase,
        load_account_credential=load_account_credential,
        mark_action_intent_unknown=mark_action_intent_unknown,
        open_unknown_outcome_breaker=open_unknown_outcome_breaker,
        parse_json_field=parse_json_field,
        prepare_and_start_action_intent=prepare_and_start_action_intent,
        quarantine_external_action_outcome=(
            quarantine_external_action_outcome
        ),
        record_event=record_event,
        refresh_task_lease=refresh_task_lease,
        renew_account_operation_lease=renew_account_operation_lease,
        row_get=row_get,
        save_phase=save_phase,
        set_account_status=set_account_status,
        settle_action_intent=settle_action_intent,
        structured_log=structured_log,
        validate_action_plan_v2=validate_action_plan_v2,
        cookie_vault=cookie_vault,
        CREDENTIAL_AAD=CREDENTIAL_AAD,
    )


# Compatibility aliases for tests and operational tooling that still inspect
# the historical queue during the migration drain.
STREAM_KEY = LEGACY_TASK_STREAM_KEY
GROUP_NAME = LEGACY_TASK_GROUP_NAME
CONSUMER_NAME = WORKER_ID
# Every platform stream has its own bounded local/Pending Entry List footprint.
# The per-platform limits are intentionally independent, so saturation or a
# slow task in one durable lane cannot consume another platform's read budget.
TASK_DISPATCH_MAX_INFLIGHT = 32
TASK_STREAM_READ_COUNT = 8
# Core recovery examines entries after 120 seconds of PEL idleness.  Refresh
# only entries which are waiting for a platform lane; running entries must keep
# using the authoritative DB lease/worker-heartbeat recovery semantics.
TASK_PENDING_REFRESH_SECONDS = 30
TASK_LANE_HEALTH_CONTRACT_VERSION = 2
TASK_LANE_HEALTH_RECENT_SECONDS = 45
TASK_LANE_HEALTH_PROGRESS_INTERVAL_SECONDS = 10
TASK_LANE_HEALTH_MAX_REPORTED_AGE_SECONDS = 86_400
TASK_LANE_HEALTH_MAX_CONSECUTIVE_FAILURES = 1_000_000
TASK_LANE_LOOP_PROGRESS_OPERATIONS = frozenset(
    {"capacity_wait", "capacity_available", "shutdown"}
)
LEGACY_SOURCE_STREAM_FIELD = "legacy_source_stream"
LEGACY_SOURCE_MESSAGE_ID_FIELD = "legacy_source_message_id"
_REDIS_STREAM_ID_RE = re.compile(r"^[0-9]+-[0-9]+$")
# Compatibility aliases for existing recovery/tests. The authoritative orders
# now belong to the independent platform modules.
PHASE_ORDER = list(BILIBILI_ACTION_ORDER)
DOUYIN_PHASE_ORDER = list(DOUYIN_ACTION_ORDER)
WEIBO_PHASE_ORDER = list(WEIBO_ACTION_ORDER)
XIAOHONGSHU_PHASE_ORDER = list(XIAOHONGSHU_REQUIRED_ACTIONS)
TERMINAL_TASK_STATUSES = {"succeeded", "failed"}
TASK_LEASE_SECONDS = 900
WEIBO_PREFLIGHT_TIMEOUT_SECONDS = 300
WEIBO_ACTION_HTTP_BUDGET_SECONDS = 20
WEIBO_ACTION_SETTLEMENT_BUFFER_SECONDS = 120
SCREENSHOT_DIR = TASK_FAILURE_EVIDENCE_DIR
SHADOW_SCREENSHOT_DIR = SHADOW_EVIDENCE_DIR
EVIDENCE_HASH_CHUNK_SIZE = 1024 * 1024
MAX_EVIDENCE_SCREENSHOT_BYTES = 32 * 1024 * 1024
MAX_EVIDENCE_SCREENSHOT_WIDTH = 8192
MAX_EVIDENCE_SCREENSHOT_HEIGHT = 20000
MAX_EVIDENCE_SCREENSHOT_PIXELS = 40_000_000


@dataclass
class _TaskLaneHealthState:
    binding: TaskStreamBinding
    last_success_monotonic: float | None = None
    last_success_operation: str | None = None
    last_loop_progress_monotonic: float | None = None
    last_loop_progress_operation: str | None = None
    inflight_count: int = 0
    last_error_monotonic: float | None = None
    last_error_operation: str | None = None
    last_error_type: str | None = None
    consecutive_failures: int = 0


def _new_task_lane_health_states() -> dict[str, _TaskLaneHealthState]:
    return {
        binding.stream_key: _TaskLaneHealthState(binding=binding)
        for binding in task_stream_bindings(include_legacy=False)
    }


_TASK_LANE_HEALTH = _new_task_lane_health_states()


def _safe_lane_health_label(value, *, maximum: int) -> str:
    normalized = re.sub(
        r"[^A-Za-z0-9_.:-]",
        "_",
        str(value or ""),
    )
    return normalized[:maximum]


def _task_lane_state(binding: TaskStreamBinding) -> _TaskLaneHealthState:
    state = _TASK_LANE_HEALTH.get(binding.stream_key)
    if state is None or state.binding != binding:
        state = _TaskLaneHealthState(binding=binding)
        _TASK_LANE_HEALTH[binding.stream_key] = state
    return state


def _record_task_lane_success(
    binding: TaskStreamBinding,
    operation: str,
) -> None:
    """Record a successful Redis lane operation without retaining payloads."""

    state = _task_lane_state(binding)
    state.last_success_monotonic = time.monotonic()
    state.last_success_operation = _safe_lane_health_label(
        operation,
        maximum=32,
    )
    state.consecutive_failures = 0


def _record_task_lane_failure(
    binding: TaskStreamBinding,
    operation: str,
    exc: BaseException,
) -> None:
    """Record bounded, non-secret failure metadata for one exact task lane."""

    state = _task_lane_state(binding)
    state.last_error_monotonic = time.monotonic()
    state.last_error_operation = _safe_lane_health_label(
        operation,
        maximum=32,
    )
    state.last_error_type = _safe_lane_health_label(
        type(exc).__name__ or "Exception",
        maximum=64,
    )
    state.consecutive_failures = min(
        state.consecutive_failures + 1,
        TASK_LANE_HEALTH_MAX_CONSECUTIVE_FAILURES,
    )


def _record_task_lane_loop_progress(
    binding: TaskStreamBinding,
    operation: str,
    *,
    inflight_count: int,
) -> None:
    """Record bounded loop-liveness evidence without retaining task data."""

    if operation not in TASK_LANE_LOOP_PROGRESS_OPERATIONS:
        raise ValueError("invalid_task_lane_loop_progress_operation")
    if (
        isinstance(inflight_count, bool)
        or not isinstance(inflight_count, int)
        or not 0 <= inflight_count <= TASK_DISPATCH_MAX_INFLIGHT
    ):
        raise ValueError("invalid_task_lane_inflight_count")
    state = _task_lane_state(binding)
    state.last_loop_progress_monotonic = time.monotonic()
    state.last_loop_progress_operation = operation
    state.inflight_count = inflight_count


def _reported_lane_age(
    now: float,
    observed_at: float | None,
) -> int | None:
    if observed_at is None:
        return None
    return min(
        max(0, int(now - observed_at)),
        TASK_LANE_HEALTH_MAX_REPORTED_AGE_SECONDS,
    )


def task_lane_health_snapshot(platforms=None) -> dict:
    """Return a JSON-safe, bounded snapshot of every durable task lane.

    A lane becomes dispatch-ready only after its own ``XREADGROUP`` succeeds.
    Merely creating the group is insufficient because Redis can retain a
    consumer with the same hostname across a Worker restart.  Contract v2
    distinguishes that Redis evidence from a bounded, periodically refreshed
    capacity-wait observation.  The latter keeps a genuinely saturated lane
    healthy without pretending that another Redis read occurred.
    """

    if platforms is None:
        selected_platforms = None
    elif isinstance(platforms, str):
        selected_platforms = frozenset(
            normalize_platform_scope(platforms)
        )
    else:
        platform_values = tuple(platforms)
        selected_platforms = (
            frozenset(normalize_platform_scope(platform_values))
            if platform_values
            else frozenset()
        )
    now = time.monotonic()
    lanes = []
    for stream_key in sorted(_TASK_LANE_HEALTH):
        state = _TASK_LANE_HEALTH[stream_key]
        binding = state.binding
        if (
            selected_platforms is not None
            and binding.platform not in selected_platforms
        ):
            continue
        success_age = _reported_lane_age(
            now,
            state.last_success_monotonic,
        )
        error_age = _reported_lane_age(
            now,
            state.last_error_monotonic,
        )
        loop_progress_age = _reported_lane_age(
            now,
            state.last_loop_progress_monotonic,
        )
        recent_read = bool(
            state.last_success_operation == "xreadgroup"
            and success_age is not None
            and success_age <= TASK_LANE_HEALTH_RECENT_SECONDS
        )
        saturated_progress = bool(
            state.last_success_operation == "xreadgroup"
            and success_age is not None
            and state.last_loop_progress_operation == "capacity_wait"
            and loop_progress_age is not None
            and loop_progress_age <= TASK_LANE_HEALTH_RECENT_SECONDS
            and state.inflight_count == TASK_DISPATCH_MAX_INFLIGHT
        )
        healthy = bool(
            state.consecutive_failures == 0
            and (recent_read or saturated_progress)
        )
        status = (
            "healthy"
            if healthy
            else (
                "degraded"
                if state.last_error_monotonic is not None
                or state.last_success_operation == "xreadgroup"
                else "starting"
            )
        )
        lanes.append(
            {
                "stream": binding.stream_key,
                "group": binding.group_name,
                "platform": binding.platform,
                "repair": bool(binding.repair),
                "protocol_version": binding.protocol_version,
                "status": status,
                "last_success_operation": (
                    state.last_success_operation
                ),
                "last_success_age_seconds": success_age,
                "last_loop_progress_operation": (
                    state.last_loop_progress_operation
                ),
                "last_loop_progress_age_seconds": loop_progress_age,
                "inflight_count": state.inflight_count,
                "inflight_limit": TASK_DISPATCH_MAX_INFLIGHT,
                "saturated": (
                    state.inflight_count
                    == TASK_DISPATCH_MAX_INFLIGHT
                ),
                "last_error_operation": state.last_error_operation,
                "last_error_type": state.last_error_type,
                "last_error_age_seconds": error_age,
                "consecutive_failures": state.consecutive_failures,
            }
        )
    return {
        "contract_version": TASK_LANE_HEALTH_CONTRACT_VERSION,
        "lanes": lanes,
    }


def _reset_task_lane_health_for_tests() -> None:
    """Reset process-local observations; production never calls this helper."""

    _TASK_LANE_HEALTH.clear()
    _TASK_LANE_HEALTH.update(_new_task_lane_health_states())


class TaskAlreadyTerminal(Exception):
    pass


class SelectorMutationPreconditionFailed(RuntimeError):
    """A selector click was rejected before any remote mutation started."""

    def __init__(self, event: str, cause: Exception) -> None:
        self.event = str(event or "unknown")
        super().__init__(
            f"selector_mutation_precondition_failed:{self.event}:{type(cause).__name__}"
        )


class TaskClaimConflict(Exception):
    pass


class TaskAlreadyClaimed(TaskClaimConflict):
    pass


class TaskOwnershipLost(Exception):
    pass


class InvalidTaskMessage(Exception):
    pass


class TaskSettlementUnconfirmed(RuntimeError):
    """The task did not reach a confirmed terminal database state."""

    def __init__(self, task_id: str, cause: BaseException) -> None:
        self.task_id = str(task_id or "").strip()
        super().__init__(f"task_settlement_unconfirmed:{self.task_id}:{type(cause).__name__}")


async def await_safety_settlement(awaitable):
    """Finish a fail-closed settlement despite repeated parent cancellation."""

    settlement_task = asyncio.create_task(awaitable)
    while True:
        try:
            return await asyncio.shield(settlement_task)
        except asyncio.CancelledError:
            if settlement_task.done():
                return settlement_task.result()
            # Later shutdown signals must not strand a breaker, ledger, or
            # account quarantine half-written. The caller re-raises its
            # original failure/cancellation after this settlement completes.
            continue


@dataclass(frozen=True)
class CanonicalTaskBinding:
    task_id: str
    account_id: int
    lottery_id: int
    task_mode: str


@dataclass
class EvidenceWriteHandoff:
    """Thread-owned completion state that survives asyncio Task cancellation."""

    cancellation_requested: threading.Event = field(default_factory=threading.Event)
    started: threading.Event = field(default_factory=threading.Event)
    finished: threading.Event = field(default_factory=threading.Event)
    result: tuple[str, tuple[int, int, int, int]] | None = None
    error: BaseException | None = None

    def publish_result(self, result: tuple[str, tuple[int, int, int, int]]) -> None:
        self.result = result


async def enforce_task_real_run_gate(task: dict, *, require_running: bool = False):
    snapshot = await enforce_real_run_gate(task, db=database, worker_id=WORKER_ID)
    if require_running and snapshot.stage != "running":
        raise RealRunGateBlocked("task_ownership_lost")
    return snapshot


def gate_execution_action_plan(snapshot):
    """Return the exact task subset from a validated gate snapshot.

    The fallback keeps older test doubles and third-party read-only
    instrumentation compatible.  Production snapshots always carry the
    independently validated ``execution_action_plan`` field.
    """

    return getattr(snapshot, "execution_action_plan", snapshot.action_plan)


def gate_requested_actions(snapshot) -> tuple[str, ...]:
    plan = gate_execution_action_plan(snapshot)
    return tuple(getattr(snapshot, "requested_actions", plan.required_actions))


def row_get(row, key: str, default=None):
    if row is None:
        return default
    if hasattr(row, "get"):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, TypeError):
        return default


async def get_latest_phase(task_id: str) -> str:
    row = await database.fetch_one(
        "SELECT phase FROM task_phases WHERE task_id = :tid ORDER BY id DESC LIMIT 1",
        {"tid": task_id},
    )
    return row["phase"] if row else "init"


async def get_completed_bilibili_phases(
    task_id: str,
    *,
    account_id: int,
    lottery_id: int,
    dynamic_id: str,
) -> set[str]:
    """Compatibility facade into Bilibili-owned ledger validation."""

    from app.platform_modules.bilibili import (
        _get_completed_bilibili_phases_owned as owned_loader,
    )

    return await owned_loader(
        task_id,
        account_id=account_id,
        lottery_id=lottery_id,
        dynamic_id=dynamic_id,
        runtime=_task_execution_services(),
    )


async def refresh_task_lease(task_id: str):
    async with database.transaction():
        row = await database.fetch_one(
            "SELECT status, worker_id FROM task_runs WHERE task_id = :task_id FOR UPDATE",
            {"task_id": task_id},
        )
        if (
            not row
            or str(row_get(row, "status") or "").strip().lower() != "running"
            or str(row_get(row, "worker_id") or "").strip() != WORKER_ID
        ):
            raise TaskOwnershipLost(f"Task {task_id} lease owner changed")
        await database.execute(
            """UPDATE task_runs
               SET lease_expires_at = DATE_ADD(NOW(), INTERVAL 900 SECOND)
               WHERE task_id = :task_id""",
            {"task_id": task_id},
        )


async def save_phase(task_id: str, account_id: int, lottery_id: int, phase: str):
    await refresh_task_lease(task_id)
    await database.execute(
        """INSERT INTO task_phases (task_id, account_id, lottery_id, phase)
           VALUES (:tid, :aid, :lid, :phase)
           ON DUPLICATE KEY UPDATE phase = :phase, updated_at = NOW()""",
        {"tid": task_id, "aid": account_id, "lid": lottery_id, "phase": phase},
    )
    event_id = await record_event(
        aggregate="task",
        aggregate_id=task_id,
        event_type="TaskPhaseCompleted" if phase != "completed" else "TaskPhasesCompleted",
        payload={"account_id": account_id, "lottery_id": lottery_id, "phase": phase},
        correlation_id=task_id,
    )
    if not event_id:
        raise RuntimeError("task_phase_event_persistence_failed")
    structured_log("info", "phase_saved", task_id=task_id, phase=phase)


async def save_bilibili_action_ledger(
    *,
    task_id: str,
    account_id: int,
    lottery_id: int,
    dynamic_id: str,
    action: str,
    phase: str | None,
    code: int | None,
    outcome: str,
    message: str | None,
    ok: bool,
):
    """Compatibility facade into Bilibili-owned ledger persistence."""

    from app.platform_modules.bilibili import (
        _save_bilibili_action_ledger_owned as owned_writer,
    )

    return await owned_writer(
        task_id=task_id,
        account_id=account_id,
        lottery_id=lottery_id,
        dynamic_id=dynamic_id,
        action=action,
        phase=phase,
        code=code,
        outcome=outcome,
        message=message,
        ok=ok,
        runtime=_task_execution_services(),
    )


async def persist_bilibili_action_result(
    *,
    task_id: str,
    account_id: int,
    lottery_id: int,
    dynamic_id: str,
    action: str,
    action_result,
) -> None:
    """Compatibility facade into Bilibili-owned result settlement."""

    from app.platform_modules.bilibili import (
        _persist_bilibili_action_result_owned as owned_settlement,
    )

    await owned_settlement(
        task_id=task_id,
        account_id=account_id,
        lottery_id=lottery_id,
        dynamic_id=dynamic_id,
        action=action,
        action_result=action_result,
        runtime=_task_execution_services(),
    )


async def mark_task_started(
    task_id: str,
    account_id: int,
    lottery_id: int,
    task_mode: str,
    stream_message_id: str | None = None,
    task_message: dict | None = None,
) -> CanonicalTaskBinding:
    async with database.transaction():
        row = await database.fetch_one(
            """SELECT task_id, account_id, lottery_id, status, task_mode, worker_id, started_at,
                      rule_snapshot_id, rule_hash, action_plan_hash, execution_path_id,
                      target_hash, config_hash, account_lease_id, account_lease_generation,
                      reconciliation_required
               FROM task_runs WHERE task_id = :task_id FOR UPDATE""",
            {"task_id": task_id},
        )
        if not row:
            raise RuntimeError(f"Task row not found: {task_id}")
        status = str(row_get(row, "status", "") or "").strip().lower()
        if status in TERMINAL_TASK_STATUSES:
            raise TaskAlreadyTerminal(f"Task {task_id} is already terminal: {status}")
        if status != "queued" or row_get(row, "worker_id") is not None:
            raise TaskAlreadyClaimed(f"Task {task_id} is already claimed")

        binding = CanonicalTaskBinding(
            task_id=str(row_get(row, "task_id") or "").strip(),
            account_id=int(row_get(row, "account_id")),
            lottery_id=int(row_get(row, "lottery_id")),
            task_mode=str(row_get(row, "task_mode") or "").strip().lower(),
        )
        if (
            binding.task_id != task_id
            or binding.account_id != account_id
            or binding.lottery_id != lottery_id
            or binding.task_mode != task_mode
        ):
            raise RuntimeError("task_binding_mismatch")

        lottery = await database.fetch_one(
            """SELECT id, status, execution_lock, platform, raw_url, canonical_url, action_plan,
                      authoritative_rule_snapshot_id, rule_hash, action_plan_hash
               FROM lotteries WHERE id = :lottery_id FOR UPDATE""",
            {"lottery_id": binding.lottery_id},
        )
        account = await database.fetch_one(
            "SELECT id, platform, status, execution_revision FROM accounts WHERE id = :account_id FOR UPDATE",
            {"account_id": binding.account_id},
        )
        if (
            not lottery
            or int(row_get(lottery, "id", 0) or 0) != binding.lottery_id
            or str(row_get(lottery, "status") or "").strip().lower() != "claimed"
            or str(row_get(lottery, "execution_lock") or "").strip() != task_id
        ):
            raise TaskClaimConflict(f"Lottery {binding.lottery_id} is not claimable by {task_id}")
        platform = str(row_get(lottery, "platform") or "").strip().lower()
        if isinstance(task_message, dict):
            message_platform = str(
                task_message.get("platform") or ""
            ).strip().lower()
            if not message_platform or message_platform != platform:
                # Redis is a delivery channel, not the platform authority.  In
                # particular, a dry-run message must not be able to select a
                # peer module and then settle the locked lottery/account rows.
                raise TaskClaimConflict("task_message_platform_mismatch")
        # Selector-driven paths bind the untrusted delivery message to the
        # current authoritative selector snapshot before account state can be
        # changed. Official OAuth/API paths opt out through their path metadata.
        try:
            platform_module = get_platform_module(platform)
            execution_path, _ = platform_module.route(
                task_mode, row_get(lottery, "action_plan")
            )
            needs_selector_binding = platform_module.requires_selector_binding(
                task_mode, row_get(lottery, "action_plan")
            )
        except PlatformRoutingError as exc:
            raise TaskClaimConflict(exc.code) from exc
        if task_mode == "shadow_run":
            await execution_path.validate_shadow_claim(
                runtime=_task_execution_services(),
                task_message=task_message,
                task_row=row,
                lottery=lottery,
                account=account,
            )
        if needs_selector_binding:
            selector_config = load_selector_config()
            authoritative_selectors = (
                selector_config.get(platform, {}) if isinstance(selector_config, dict) else {}
            )
            selector_row = await database.fetch_one(
                "SELECT config_json FROM adapter_selector_configs WHERE platform = :platform FOR UPDATE",
                {"platform": platform},
            )
            if selector_row:
                persisted_selectors = parse_json_field(row_get(selector_row, "config_json"))
                if not isinstance(persisted_selectors, dict):
                    raise TaskClaimConflict(
                        "shadow_task_selector_config_invalid"
                        if task_mode == "shadow_run"
                        else "task_selector_config_invalid"
                    )
                authoritative_selectors = persisted_selectors
            if task_mode == "shadow_run":
                validate_shadow_task_binding(task_message, lottery)
            validate_task_selector_binding(task_message, authoritative_selectors)
        if (
            not account
            or int(row_get(account, "id", 0) or 0) != binding.account_id
            or str(row_get(account, "status") or "").strip().lower() != "ready"
        ):
            raise TaskClaimConflict(f"Account {binding.account_id} is not ready")
        account_platform = str(row_get(account, "platform") or "").strip().lower()
        if account_platform != platform:
            # The queue message is not authoritative, and even a valid
            # task_run can be backed by historic/corrupt cross-platform data.
            # Re-check the locked account and lottery rows before changing
            # either status so dry/shadow tasks cannot cross platform bounds.
            raise TaskClaimConflict("task_account_platform_mismatch")

        await database.execute(
            """UPDATE task_runs
               SET status = 'running', task_mode = :task_mode,
                   started_at = COALESCE(started_at, NOW()),
                    worker_id = :worker_id,
                    stream_message_id = :stream_message_id,
                    lease_expires_at = DATE_ADD(NOW(), INTERVAL 900 SECOND)
               WHERE task_id = :task_id""",
            {
                "task_id": task_id,
                "task_mode": task_mode,
                "worker_id": WORKER_ID,
                "stream_message_id": stream_message_id,
            },
        )
        await database.execute(
            """UPDATE lotteries SET status = 'running'
               WHERE id = :lottery_id""",
            {"lottery_id": binding.lottery_id},
        )
        if task_mode == "real_run":
            if row_get(row, "started_at") is None:
                await database.execute(
                    """UPDATE accounts
                       SET status = 'executing', daily_task_count = daily_task_count + 1,
                           last_active_at = NOW(), version = version + 1
                       WHERE id = :account_id""",
                    {"account_id": binding.account_id},
                )
            else:
                await database.execute(
                    """UPDATE accounts SET status = 'executing', last_active_at = NOW(), version = version + 1
                       WHERE id = :account_id""",
                    {"account_id": binding.account_id},
                )
        else:
            await database.execute(
                "UPDATE accounts SET last_active_at = NOW() WHERE id = :account_id AND status = 'ready'",
                {"account_id": binding.account_id},
            )
    await record_event(
        aggregate="task",
        aggregate_id=task_id,
        event_type="TaskStarted",
        payload={"account_id": binding.account_id, "lottery_id": binding.lottery_id, "mode": binding.task_mode, "worker_id": WORKER_ID},
        correlation_id=task_id,
    )
    if task_mode == "real_run":
        await record_event(
            aggregate="account",
            aggregate_id=binding.account_id,
            event_type="AccountExecutionStarted",
            payload={"task_id": task_id, "lottery_id": binding.lottery_id},
            correlation_id=task_id,
        )
    return binding


def _claim_positive_int(value, code: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise TaskClaimConflict(code) from exc
    if result <= 0:
        raise TaskClaimConflict(code)
    return result


async def validate_bilibili_api_shadow_claim(
    *,
    task_message: dict | None,
    task_row,
    lottery,
    account,
) -> None:
    """Compatibility facade into Bilibili-owned claim validation."""

    from app.platform_modules.bilibili import (
        validate_bilibili_api_shadow_claim as owned_validator,
    )

    await owned_validator(
        runtime=_task_execution_services(),
        task_message=task_message,
        task_row=task_row,
        lottery=lottery,
        account=account,
    )


def validate_shadow_task_binding(task: dict | None, lottery) -> None:
    """Bind read-only Shadow evidence to the current authoritative target."""
    if not isinstance(task, dict):
        raise TaskClaimConflict("shadow_task_message_missing")

    bindings = (
        ("platform", str(task.get("platform") or "").strip().lower(), str(row_get(lottery, "platform") or "").strip().lower()),
        ("raw_url", str(task.get("raw_url") or "").strip(), str(row_get(lottery, "raw_url") or "").strip()),
        ("canonical_url", str(task.get("canonical_url") or "").strip(), str(row_get(lottery, "canonical_url") or "").strip()),
    )
    for field, message_value, authoritative_value in bindings:
        if not message_value or message_value != authoritative_value:
            raise TaskClaimConflict(f"shadow_task_{field}_mismatch")

    message_plan = parse_json_field(task.get("action_plan"))
    authoritative_plan = parse_json_field(row_get(lottery, "action_plan"))
    if not isinstance(message_plan, dict) or not isinstance(authoritative_plan, dict):
        raise TaskClaimConflict("shadow_task_action_plan_invalid")
    if message_plan != authoritative_plan:
        raise TaskClaimConflict("shadow_task_action_plan_mismatch")

    manual_platform = str(row_get(lottery, "platform") or "").strip().lower()
    try:
        platform_module = get_platform_module(manual_platform)
        platform_module.route("shadow_run", authoritative_plan)
    except PlatformRoutingError as exc:
        raise TaskClaimConflict(exc.code) from exc
    if platform_module.requires_selector_binding(
        "shadow_run",
        authoritative_plan,
    ):
        if any(
            str(plan.get("platform") or "").strip().lower() != manual_platform
            for plan in (authoritative_plan, message_plan)
        ):
            raise TaskClaimConflict(
                "shadow_task_action_plan_platform_mismatch"
            )
        try:
            validate_action_plan_v2(
                authoritative_plan,
                require_executable=False,
            )
            validate_action_plan_v2(
                message_plan,
                require_executable=False,
            )
        except ActionPlanV2Error as exc:
            raise TaskClaimConflict(f"shadow_task_{exc.code}") from exc


def validate_task_selector_binding(task: dict | None, authoritative_selectors: dict) -> None:
    if not isinstance(task, dict):
        raise TaskClaimConflict("task_selector_message_missing")
    message_selectors = parse_json_field(task.get("selector_config"))
    if not isinstance(message_selectors, dict) or message_selectors != authoritative_selectors:
        raise TaskClaimConflict("task_selector_config_mismatch")


def _settlement_requested_actions(plan, execution_binding) -> tuple[str, ...] | None:
    """Resolve the exact DB-bound action set whose effects close this task."""

    if execution_binding is None:
        # Narrow compatibility for a pre-contract full task.  The real-run gate
        # has already required its trusted legacy fanout + Outbox authority.
        return tuple(plan.required_actions)
    kind = str(row_get(execution_binding, "binding_kind") or "").strip().lower()
    requested = parse_json_field(
        row_get(execution_binding, "requested_actions")
    )
    if (
        not isinstance(requested, list)
        or not requested
        or any(not isinstance(action, str) for action in requested)
        or len(requested) != len(set(requested))
        or str(
            row_get(execution_binding, "evidence_action_plan_hash") or ""
        ).strip()
        != plan.plan_hash
    ):
        return None
    selected = set(requested)
    ordered = tuple(
        action for action in plan.required_actions if action in selected
    )
    if tuple(requested) != ordered or not selected.issubset(
        set(plan.required_actions)
    ):
        return None
    if kind == "full":
        return ordered if ordered == tuple(plan.required_actions) else None
    if kind == "repair":
        return ordered if len(ordered) < len(plan.required_actions) else None
    return None


def _real_success_intents_match_reviewed_plan(
    *, task_row, lottery, intent_rows, execution_binding=None
) -> bool:
    """Prove every reviewed API mutation has one confirmed durable intent.

    A caller saying ``success=True`` is not authority.  Settlement revalidates
    the locked lottery's immutable Action Plan and requires a one-to-one set of
    confirmed external intents before it may release the real-run fence.
    """

    platform = str(row_get(lottery, "platform") or "").strip().lower()
    try:
        platform_module = get_platform_module(platform)
    except PlatformRoutingError:
        return False
    try:
        plan = validate_action_plan_v2(
            row_get(lottery, "action_plan"), reject_media=True
        )
    except (ActionPlanV2Error, TypeError, ValueError):
        return False
    task_plan_hash = str(row_get(task_row, "action_plan_hash") or "").strip()
    lottery_plan_hash = str(row_get(lottery, "action_plan_hash") or "").strip()
    try:
        execution_path, _ = platform_module.route("real_run", plan.plan)
    except PlatformRoutingError:
        return False
    if not execution_path.confirmed_intent_settlement:
        return False
    if (
        plan.execution_path_id != execution_path.path_id
        or str(plan.plan.get("platform") or "").strip().lower() != platform
        or not task_plan_hash
        or task_plan_hash != plan.plan_hash
        or lottery_plan_hash != plan.plan_hash
    ):
        return False

    requested_actions = _settlement_requested_actions(
        plan,
        execution_binding,
    )
    if requested_actions is None:
        return False
    try:
        expected_actions = platform_module.expected_intent_actions(
            list(requested_actions)
        )
    except (TypeError, ValueError):
        return False
    if not expected_actions or len(expected_actions) != len(requested_actions):
        return False
    observed_actions: list[str] = []
    observed_intent_ids: list[str] = []
    for intent in intent_rows or []:
        raw_action = row_get(intent, "action")
        action = str(raw_action or "").strip().lower()
        intent_id = str(row_get(intent, "intent_id") or "").strip()
        if (
            not isinstance(raw_action, str)
            or raw_action != action
            or not action
            or not intent_id
            or str(row_get(intent, "status") or "").strip().lower()
            != "succeeded"
            or str(row_get(intent, "effect_certainty") or "").strip().lower()
            != "confirmed_effect"
        ):
            return False
        observed_actions.append(action)
        observed_intent_ids.append(intent_id)
    return bool(
        len(observed_actions) == len(expected_actions)
        and len(observed_actions) == len(set(observed_actions))
        and len(observed_intent_ids) == len(set(observed_intent_ids))
        and set(observed_actions) == set(expected_actions)
    )


async def mark_task_finished(
    task_id: str,
    success: bool,
    error: str | None = None,
    screenshot_path: str | None = None,
    quarantine_account: bool = False,
    account_failure_status: str | None = None,
):
    async with database.transaction():
        row = await database.fetch_one(
            """SELECT task_id, account_id, lottery_id, status, task_mode, worker_id,
                      action_plan_hash, account_lease_id,
                      account_lease_generation, reconciliation_required
               FROM task_runs WHERE task_id = :task_id FOR UPDATE""",
            {"task_id": task_id},
        )
        if not row:
            raise RuntimeError(f"Task row not found: {task_id}")
        status = str(row_get(row, "status", "") or "").strip().lower()
        if status in TERMINAL_TASK_STATUSES:
            return False
        binding = CanonicalTaskBinding(
            task_id=str(row_get(row, "task_id") or "").strip(),
            account_id=int(row_get(row, "account_id")),
            lottery_id=int(row_get(row, "lottery_id")),
            task_mode=str(row_get(row, "task_mode") or "").strip().lower(),
        )
        normalized_failure_status = (
            str(account_failure_status or "").strip().lower() or None
        )
        if normalized_failure_status not in {
            None,
            "login_required",
            "warming",
            "cooling",
        }:
            raise ValueError("account_failure_status_invalid")
        if normalized_failure_status is not None and (
            success or binding.task_mode != "real_run"
        ):
            raise ValueError("account_failure_status_not_applicable")
        if binding.task_id != task_id:
            raise RuntimeError("task_binding_mismatch")
        release_account = False
        if status == "running":
            if str(row_get(row, "worker_id") or "").strip() != WORKER_ID:
                raise TaskOwnershipLost(f"Task {task_id} is owned by another worker")
            # Only real-run claims transition the account to `executing`.
            # A dry/shadow task must not release an account that a different
            # real task acquired after the read-only task started.
            release_account = binding.task_mode == "real_run"
        elif status == "queued":
            raise TaskOwnershipLost(f"Task {task_id} was not claimed by this worker")
        else:
            raise RuntimeError(f"Task {task_id} has invalid status: {status}")

        lottery = await database.fetch_one(
            """SELECT id, status, execution_lock, platform, action_plan,
                      action_plan_hash
               FROM lotteries WHERE id = :lottery_id FOR UPDATE""",
            {"lottery_id": binding.lottery_id},
        )
        if (
            not lottery
            or str(row_get(lottery, "status") or "").strip().lower() != "running"
            or str(row_get(lottery, "execution_lock") or "").strip() != task_id
        ):
            raise TaskOwnershipLost(f"Lottery {binding.lottery_id} settlement ownership changed")

        intent_rows = []
        intent_statuses: list[tuple[str, str]] = []
        execution_binding = None
        lease_operation_kind = binding.task_mode
        if binding.task_mode == "real_run":
            execution_binding = await database.fetch_one(
                """SELECT binding_kind, requested_actions,
                          evidence_action_plan_hash
                   FROM task_execution_intent_bindings
                   WHERE task_id = :task_id
                   FOR UPDATE""",
                {"task_id": task_id},
            )
            execution_intent_kind = str(
                row_get(execution_binding, "binding_kind")
                or LEGACY_FULL_EXECUTION_INTENT_KIND
            )
            try:
                lease_operation_kind = (
                    lease_operation_kind_for_execution_intent(
                        execution_intent_kind
                    )
                )
            except ValueError as exc:
                raise TaskSettlementUnconfirmed(
                    task_id,
                    RuntimeError(
                        "execution_intent_lease_operation_kind_invalid"
                    ),
                ) from exc
            intent_rows = await database.fetch_all(
                """SELECT intent_id, action, status, effect_certainty
                   FROM external_action_intents
                   WHERE task_id = :task_id
                   FOR UPDATE""",
                {"task_id": task_id},
            )
            intent_statuses = [
                (
                    str(row_get(intent, "status") or "").strip().lower(),
                    str(row_get(intent, "effect_certainty") or "").strip().lower(),
                )
                for intent in (intent_rows or [])
            ]
        expected_certainty = {
            "prepared": "not_started",
            "started": "unknown",
            "succeeded": "confirmed_effect",
            "failed": "confirmed_no_effect",
            "unknown": "unknown",
        }
        invalid_intent_state = any(
            expected_certainty.get(status) != certainty
            for status, certainty in intent_statuses
        )
        any_started_or_unknown = any(
            certainty == "unknown" for _status, certainty in intent_statuses
        )
        ambiguous_real_failure = (
            binding.task_mode == "real_run"
            and not success
            and any_started_or_unknown
        )
        inconsistent_real_success = bool(
            binding.task_mode == "real_run"
            and success
            and not _real_success_intents_match_reviewed_plan(
                task_row=row,
                lottery=lottery,
                intent_rows=intent_rows,
                execution_binding=execution_binding,
            )
        )
        real_reconciliation_required = bool(
            quarantine_account and not success and binding.task_mode == "real_run"
        ) or ambiguous_real_failure or inconsistent_real_success or invalid_intent_state or int(
            row_get(row, "reconciliation_required", 0) or 0
        ) == 1
        if inconsistent_real_success:
            success = False
            error = error or "real_run_success_intent_closure_invalid"
        lottery_status = "participated" if success and binding.task_mode == "real_run" else "pending"
        await database.execute(
            """UPDATE task_runs
               SET status = :status, error_message = :error, screenshot_path = :screenshot_path,
                   finished_at = NOW(), lease_expires_at = NULL,
                   reconciliation_required = CASE
                     WHEN :reconciliation_required = 1 THEN 1
                     ELSE reconciliation_required
                   END
               WHERE task_id = :task_id""",
            {
                "task_id": task_id,
                "status": "succeeded" if success else "failed",
                "error": error,
                "screenshot_path": screenshot_path,
                "reconciliation_required": 1 if real_reconciliation_required else 0,
            },
        )
        if real_reconciliation_required:
            # There is no persisted reconciliation state yet. Retain the lock
            # so a breaker reset cannot silently replay this target before an
            # operator reconciles the external outcome. The task itself is
            # terminal failed and explicitly marked reconciliation-required.
            await database.execute(
                "UPDATE lotteries SET status = 'running' WHERE id = :lottery_id AND execution_lock = :task_id AND status = 'running'",
                {"lottery_id": binding.lottery_id, "task_id": task_id},
            )
        else:
            await database.execute(
                "UPDATE lotteries SET status = :status, execution_lock = NULL, locked_at = NULL WHERE id = :lottery_id AND execution_lock = :task_id AND status = 'running'",
                {"lottery_id": binding.lottery_id, "status": lottery_status, "task_id": task_id},
            )
        if release_account:
            account = await database.fetch_one(
                "SELECT status FROM accounts WHERE id = :account_id FOR UPDATE",
                {"account_id": binding.account_id},
            )
            current_account_status = str(
                row_get(account, "status", "") or ""
            ).strip().lower()
            if current_account_status not in {
                "executing",
                "login_required",
                "warming",
                "cooling",
            }:
                raise AccountStatusPersistenceFailed(
                    binding.account_id,
                    normalized_failure_status or "ready",
                    "task_terminal_account_status_invalid",
                    RuntimeError("account_status_invalid"),
                )
            # A risk handler may already have moved the account out of
            # `executing`. Preserve that stricter state; otherwise apply this
            # task's classified terminal target.
            target_account_status = (
                current_account_status
                if current_account_status != "executing"
                else (
                    "cooling"
                    if quarantine_account or real_reconciliation_required
                    else normalized_failure_status or "ready"
                )
            )
            account_updated = await execute_affected_rows(
                "UPDATE accounts SET status = :account_status, updated_at = NOW(), version = version + 1 WHERE id = :account_id AND status = :expected_account_status",
                {
                    "account_id": binding.account_id,
                    "account_status": target_account_status,
                    "expected_account_status": current_account_status,
                },
                db=database,
            )
            if account_updated != 1:
                raise AccountStatusPersistenceFailed(
                    binding.account_id,
                    target_account_status,
                    "task_terminal_account_status_cas_lost",
                    RuntimeError("account_status_cas_lost"),
                )
        lease_id = str(row_get(row, "account_lease_id") or "").strip()
        try:
            lease_generation = int(row_get(row, "account_lease_generation"))
        except (TypeError, ValueError) as exc:
            raise TaskSettlementUnconfirmed(task_id, exc) from exc
        if not lease_id or lease_generation <= 0:
            raise TaskSettlementUnconfirmed(
                task_id, RuntimeError("account_operation_lease_binding_missing")
            )
        # Read-only tasks and known real terminal outcomes release precisely the
        # generation they owned. A confirmed partial result is safe to expose
        # to the missing-action Repair path; only ambiguous or inconsistent
        # real outcomes retain the append-only lease and lottery lock.
        if not (binding.task_mode == "real_run" and real_reconciliation_required):
            await database.execute(
                """UPDATE account_operation_leases
                   SET released_at = NOW()
                   WHERE account_id = :account_id AND lease_id = :lease_id
                      AND generation = :generation AND task_id = :task_id
                      AND owner_id = :task_id AND operation_kind = :operation_kind
                      AND released_at IS NULL""",
                {
                    "account_id": binding.account_id,
                    "lease_id": lease_id,
                    "generation": lease_generation,
                    "task_id": task_id,
                    "operation_kind": lease_operation_kind,
                },
            )
            lease = await database.fetch_one(
                """SELECT released_at FROM account_operation_leases
                   WHERE account_id = :account_id AND lease_id = :lease_id
                     AND generation = :generation""",
                {
                    "account_id": binding.account_id,
                    "lease_id": lease_id,
                    "generation": lease_generation,
                },
            )
            if not lease or row_get(lease, "released_at") is None:
                raise TaskSettlementUnconfirmed(
                    task_id, RuntimeError("account_operation_lease_release_failed")
                )
    # Notification delivery is not part of canonical task settlement. A
    # transient notify_logs failure must not roll back a completed task and turn
    # it into an artificial unknown external outcome/platform quarantine.
    try:
        await database.execute(
            """INSERT INTO notify_logs (channel, title, content, success)
               VALUES ('system', :title, :content, :success)""",
            {
                "title": f"Task {task_id[:8]} {binding.task_mode} {'succeeded' if success else 'failed'}",
                "content": error or f"Lottery {binding.lottery_id} handled by account {binding.account_id} in {binding.task_mode}",
                "success": 1 if success else 0,
            },
        )
    except Exception as exc:
        structured_log("warning", "task_notification_write_failed", task_id=task_id, error=str(exc))
    try:
        await record_event(
            aggregate="task",
            aggregate_id=task_id,
            event_type="TaskFinished" if success else "TaskFailed",
            payload={
                "account_id": binding.account_id,
                "lottery_id": binding.lottery_id,
                "success": success,
                "mode": binding.task_mode,
                "error": error,
                "screenshot_path": screenshot_path,
                "worker_id": WORKER_ID,
            },
            correlation_id=task_id,
        )
    except Exception as exc:
        structured_log("error", "task_finish_event_failed", task_id=task_id, exception=exc)
    try:
        await record_event(
            aggregate="account",
            aggregate_id=binding.account_id,
            event_type="AccountExecutionFinished",
            payload={"task_id": task_id, "lottery_id": binding.lottery_id, "success": success, "mode": binding.task_mode},
            correlation_id=task_id,
        )
    except Exception as exc:
        structured_log("error", "account_finish_event_failed", task_id=task_id, exception=exc)
    try:
        await redis.xadd(
            "notify_events",
            {
                "title": f"Task {task_id[:8]} {binding.task_mode} {'succeeded' if success else 'failed'}",
                "content": error or f"Lottery {binding.lottery_id} handled by account {binding.account_id} in {binding.task_mode}",
                "task_id": task_id,
                "account_id": str(binding.account_id),
                "lottery_id": str(binding.lottery_id),
                "status": "succeeded" if success else "failed",
                "mode": binding.task_mode,
                "channels": "all",
            },
        )
    except Exception as exc:
        structured_log("warning", "notify_enqueue_failed", task_id=task_id, error=str(exc))
    return True


async def execute_dry_run(
    task_id: str,
    account_id: int,
    lottery_id: int,
    phases: list[str],
    *,
    platform: str = "",
    action_plan=None,
):
    normalized_platform = str(platform or "bilibili").strip().lower()
    try:
        platform_module = get_platform_module(normalized_platform)
        execution_path, _ = platform_module.route("dry_run", action_plan)
    except PlatformRoutingError as exc:
        raise RuntimeError(exc.code) from exc
    if execution_path.dry_run_requires_executable_plan:
        try:
            validated_plan = validate_action_plan_v2(
                action_plan,
                reject_media=True,
            )
        except ActionPlanV2Error as exc:
            raise RuntimeError(exc.code) from exc
        if validated_plan.execution_path_id != execution_path.path_id:
            raise RuntimeError(f"{normalized_platform}_execution_path_not_supported")
        if tuple(phases) != validated_plan.required_actions:
            raise RuntimeError(f"{normalized_platform}_dry_run_phase_binding_mismatch")
    for phase_name in phases:
        await asyncio.sleep(0.2)
        await save_phase(task_id, account_id, lottery_id, phase_name)
    await save_phase(task_id, account_id, lottery_id, "completed")
    structured_log("info", "dry_run_task_completed", task_id=task_id, account_id=account_id, lottery_id=lottery_id)


async def execute_bilibili_api_shadow(task: dict) -> None:
    """Compatibility facade into Bilibili-owned API observation."""

    from app.platform_modules.bilibili import (
        execute_bilibili_api_shadow as owned_shadow,
    )

    await owned_shadow(
        task,
        None,
        None,
        runtime=_task_execution_services(),
    )


async def execute_shadow_run(task: dict, adapter, pool):
    """Route a read-only run through the selected platform-owned handler."""

    platform = str(task.get("platform") or "bilibili").strip().lower()
    try:
        platform_module = get_platform_module(platform)
        execution_path, _ = platform_module.route(
            "shadow_run",
            task.get("action_plan"),
        )
    except PlatformRoutingError as exc:
        raise RuntimeError(exc.code) from exc
    return await execution_path.execute(
        "shadow_run",
        task,
        adapter,
        pool,
        runtime=_task_execution_services(),
    )


async def execute_browser_observation_shadow(task: dict, adapter, pool):
    """Shared browser-observation infrastructure selected by platform paths."""

    task_id = task.get("task_id")
    account_id = int(task.get("account_id"))
    lottery_id = int(task.get("lottery_id"))
    lottery_url = task.get("raw_url") or task.get("canonical_url")
    platform = str(task.get("platform") or "bilibili").strip().lower()
    try:
        platform_module = get_platform_module(platform)
        execution_path, _ = platform_module.route(
            "shadow_run", task.get("action_plan")
        )
    except PlatformRoutingError as exc:
        raise RuntimeError(exc.code) from exc
    canonical_uri = validated_platform_canonical_uri(platform, task.get("canonical_url"))
    profile_dir = f"/profiles/{platform}/account_{account_id}"
    proxy = None
    phases = requested_phases(task, require_plan=False)
    ctx = await pool.get_account_context(
        account_id,
        profile_dir,
        proxy,
        platform=platform,
    )
    await prepare_account_login(ctx, account_id, platform)
    page = await ctx.new_page()
    try:
        lottery_url = validated_platform_navigation_url(platform, lottery_url)
        await install_main_frame_navigation_guard(page, platform)
        await page.goto(lottery_url, wait_until="domcontentloaded", timeout=30000)
        validated_platform_content_url(platform, page.url, canonical_uri)
        await install_main_frame_navigation_guard(page, platform, canonical_uri)
        await refresh_task_lease(task_id)
        await page.wait_for_timeout(2000)
        validated_platform_content_url(platform, page.url, canonical_uri)
        await detect_page_risk(page, account_id, platform)
        visible_phases = {}
        phase_readiness = {}
        selector_probes = getattr(adapter, "SELECTOR_PROBES", {}) or {}
        for phase_name in phases:
            validated_platform_content_url(platform, page.url, canonical_uri)
            observation = await observe_shadow_phase(page, adapter, selector_probes, phase_name)
            visible_phases[phase_name] = observation
            phase_readiness[phase_name] = shadow_phase_is_ready(phase_name, observation)
            await refresh_task_lease(task_id)
        validated_platform_content_url(platform, page.url, canonical_uri)
        screenshot_path = await capture_shadow_screenshot(
            page,
            task_id,
            account_id,
            lottery_id,
            visible_phases,
            validate_content=lambda: validated_platform_content_url(
                platform,
                page.url,
                canonical_uri,
            ),
        )
        validated_platform_content_url(platform, page.url, canonical_uri)
        missing_phases = [phase for phase in phases if not phase_readiness.get(phase)]
        selector_observation_complete = (
            bool(phases) and not missing_phases and bool(screenshot_path)
        )
        setattr(
            adapter,
            "_last_shadow_observation_context",
            {
                "capability_checks": {
                    phase: phase_readiness.get(phase) is True
                    for phase in phases
                },
                "account_authenticated": selector_observation_complete,
                "target_identity_verified": True,
                "selector_observation_complete": (
                    selector_observation_complete
                ),
            },
        )
        path_supports_real_run = "real_run" in execution_path.supported_modes
        manual_confirmation_required = bool(
            getattr(adapter, "MANUAL_CONFIRMATION_REQUIRED", False)
            and not path_supports_real_run
        )
        capability_block_reason = (
            getattr(adapter, "CAPABILITY_BLOCK_REASON", None)
            or platform_module.capability_block_reason
            or platform_real_run_block_reason(platform)
            if manual_confirmation_required
            else None
        )
        supports_actions = getattr(adapter, "supports_actions", None)
        adapter_supports_phases = (
            supports_actions(phases)
            if callable(supports_actions)
            else bool(getattr(adapter, "REAL_ACTIONS", False))
        )
        real_run_capable = (
            capability_block_reason is None
            and path_supports_real_run
            and adapter_supports_phases
        )
        qualified = selector_observation_complete and real_run_capable
        observation_event_id = await record_event(
            aggregate="task",
            aggregate_id=task_id,
            event_type="TaskShadowRunObserved",
            payload={
                "account_id": account_id,
                "lottery_id": lottery_id,
                "platform": platform,
                "required_phases": phases,
                "visible_phases": visible_phases,
                "screenshot_path": screenshot_path,
                "qualified": qualified,
                "selector_observation_complete": selector_observation_complete,
                "manual_confirmation_required": manual_confirmation_required,
                "real_run_capable": real_run_capable,
                "capability_block_reason": capability_block_reason,
                "side_effects": False,
            },
            correlation_id=task_id,
        )
        if not screenshot_path:
            raise RuntimeError("shadow_run_evidence_capture_failed")
        if not observation_event_id:
            raise RuntimeError("shadow_run_observation_persistence_failed")
        if missing_phases:
            raise RuntimeError(f"shadow_run_required_phases_not_visible: {','.join(missing_phases)}")
        await save_phase(task_id, account_id, lottery_id, "completed")
        structured_log(
            "info",
            "shadow_run_task_completed",
            task_id=task_id,
            account_id=account_id,
            lottery_id=lottery_id,
            visible_phase_count=sum(1 for value in phase_readiness.values() if value),
        )
        return screenshot_path
    except Exception:
        await capture_failure_screenshot(page, task_id)
        raise
    finally:
        try:
            await page.close()
        except Exception as exc:
            # Closing the local browser page is cleanup, not evidence that a
            # previously settled external mutation failed.
            structured_log("warning", "task_page_close_failed", task_id=task_id, exception=exc)


async def first_visible_selector(page, selectors: list[str]) -> str | None:
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if await locator.is_visible(timeout=1000):
                return selector
        except Exception:
            continue
    return None


def shadow_selector_list(value) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


async def observe_shadow_phase(page, adapter, selector_probes: dict, phase_name: str):
    if phase_name != "commented":
        return await first_visible_selector(page, selector_probes.get(phase_name, []))

    configured = getattr(adapter, "configured_selectors", {}) or {}
    comment = configured.get("commented") if isinstance(configured, dict) else None
    if not isinstance(comment, dict):
        return {"input": None, "submit": None}
    input_selectors = shadow_selector_list(comment.get("input") or comment.get("inputs"))
    submit_selectors = shadow_selector_list(comment.get("submit") or comment.get("submits"))
    return {
        "input": await first_visible_selector(page, input_selectors),
        "submit": await first_visible_selector(page, submit_selectors),
    }


def shadow_phase_is_ready(phase_name: str, observation) -> bool:
    if phase_name == "commented":
        return isinstance(observation, dict) and bool(observation.get("input") and observation.get("submit"))
    return bool(observation)


async def emergency_stop_real_runs_and_revoke_lease(
    *,
    task_id: str,
    platform: str,
    action: str,
) -> str:
    """Fail closed when the normal platform-breaker write is unavailable.

    Keeping the stream message pending is insufficient while this worker still
    owns a fresh lease: Recovery deliberately skips such tasks.  Establish a
    separately verified global stop first, then revoke this worker's lease so
    Recovery may retry the platform quarantine.  This uses existing tables and
    intentionally disables *all* real runs when the narrower breaker cannot be
    persisted.

    Returns the durable barrier that was established.  Lease revocation is
    attempted even when neither barrier can be verified; any missing barrier
    or failed revocation raises so the message remains pending and the failure
    is visible.
    """

    normalized_platform = str(platform or "unknown").strip().lower() or "unknown"
    normalized_action = str(action or "unknown").strip().lower() or "unknown"
    reason = f"emergency_unknown_outcome:{normalized_platform}:{normalized_action}"[:255]
    barrier: str | None = None
    barrier_errors: list[str] = []

    try:
        await database.execute(
            """INSERT INTO circuit_breakers (scope, status, reason, opened_at)
               VALUES ('global', 'open', :reason, NOW())
               ON DUPLICATE KEY UPDATE status = 'open', reason = :reason,
                 opened_at = NOW(), updated_at = NOW()""",
            {"reason": reason},
        )
        persisted = await database.fetch_one(
            "SELECT status FROM circuit_breakers WHERE scope = 'global'"
        )
        if str(row_get(persisted, "status") or "").strip().lower() != "open":
            raise RuntimeError("emergency_global_breaker_not_persisted")
        barrier = "global_breaker"
    except Exception as exc:
        barrier_errors.append(f"global_breaker:{type(exc).__name__}")

    if barrier is None:
        try:
            await database.execute(
                """INSERT INTO runtime_settings (setting_key, setting_value)
                   VALUES ('real_run_enabled', 'false')
                   ON DUPLICATE KEY UPDATE setting_value = 'false', updated_at = NOW()"""
            )
            persisted = await database.fetch_one(
                "SELECT setting_value FROM runtime_settings WHERE setting_key = 'real_run_enabled'"
            )
            if str(row_get(persisted, "setting_value") or "").strip().lower() not in {
                "0",
                "false",
                "no",
                "off",
            }:
                raise RuntimeError("emergency_real_run_disable_not_persisted")
            barrier = "runtime_setting"
        except Exception as exc:
            barrier_errors.append(f"runtime_setting:{type(exc).__name__}")

    lease_error: Exception | None = None
    try:
        async with database.transaction():
            await database.execute(
                """UPDATE task_runs
                   SET worker_id = NULL,
                       lease_expires_at = DATE_SUB(NOW(), INTERVAL 1 SECOND)
                   WHERE task_id = :task_id
                     AND status = 'running'""",
                {"task_id": task_id},
            )
            task = await database.fetch_one(
                """SELECT status, worker_id,
                          CASE WHEN lease_expires_at IS NOT NULL AND lease_expires_at > NOW()
                               THEN 1 ELSE 0 END AS lease_active
                   FROM task_runs WHERE task_id = :task_id FOR UPDATE""",
                {"task_id": task_id},
            )
            if not task:
                raise RuntimeError("emergency_task_missing")
            status = str(row_get(task, "status") or "").strip().lower()
            owner = str(row_get(task, "worker_id") or "").strip()
            lease_active = int(row_get(task, "lease_active", 0) or 0) == 1
            if status not in TERMINAL_TASK_STATUSES and (owner or lease_active):
                raise RuntimeError("emergency_task_lease_still_active")
    except Exception as exc:
        lease_error = exc

    structured_log(
        "error",
        "real_run_emergency_stop",
        task_id=task_id,
        platform=normalized_platform,
        action=normalized_action,
        barrier=barrier,
        barrier_errors=barrier_errors,
        lease_revoked=lease_error is None,
        lease_error=type(lease_error).__name__ if lease_error else None,
    )
    if barrier is None:
        raise RuntimeError(
            "emergency_global_stop_not_persisted:" + ",".join(barrier_errors)
        )
    if lease_error is not None:
        raise RuntimeError("emergency_task_lease_revoke_failed") from lease_error
    return barrier


async def quarantine_external_action_outcome(
    *,
    task_id: str,
    account_id: int,
    platform: str,
    action: str,
    cause: BaseException,
) -> None:
    """Persist the platform breaker; account quarantine has a task fallback."""

    errors: list[str] = []
    breaker_error: Exception | None = None
    emergency_error: Exception | None = None
    try:
        await open_unknown_outcome_breaker(
            db=database,
            platform=platform,
            action=action,
        )
    except Exception as exc:
        breaker_error = exc
        errors.append(f"breaker:{type(exc).__name__}")
        try:
            await emergency_stop_real_runs_and_revoke_lease(
                task_id=task_id,
                platform=platform,
                action=action,
            )
        except Exception as emergency_exc:
            emergency_error = emergency_exc
            errors.append(f"emergency:{type(emergency_exc).__name__}")
    try:
        await set_account_status(
            account_id,
            "cooling",
            f"{str(platform or 'unknown').lower()}_{str(action or 'unknown').lower()}_outcome_unknown",
        )
    except Exception as exc:
        errors.append(f"account:{type(exc).__name__}")
    structured_log(
        "error",
        "external_action_outcome_unknown",
        task_id=task_id,
        account_id=account_id,
        platform=platform,
        action=action,
        cause_type=type(cause).__name__,
        quarantine_errors=errors,
    )
    if breaker_error is not None:
        # The global stop prevents other real runs, and the revoked lease lets
        # Recovery retry the narrower platform quarantine. Keep the message in
        # PEL until that durable settlement succeeds.
        cause = emergency_error or breaker_error
        raise TaskSettlementUnconfirmed(task_id, cause) from cause


async def execute_real_task(task: dict, adapter, pool):
    task_id = task.get("task_id")
    account_id = int(task.get("account_id"))
    lottery_id = int(task.get("lottery_id"))
    lottery_url = task.get("raw_url") or task.get("canonical_url")
    platform = str(task.get("platform") or "bilibili").strip().lower()
    try:
        platform_module = get_platform_module(platform)
    except PlatformRoutingError as exc:
        raise RuntimeError(exc.code) from exc
    phase_order = list(platform_module.action_order)
    phases = requested_phases(task, require_plan=True)
    durable_intents_required = bool(
        getattr(adapter, "DURABLE_INTENTS_REQUIRED", False)
    )
    intent_plan = None
    if durable_intents_required:
        try:
            intent_plan = validate_action_plan_v2(
                task.get("action_plan"),
                require_executable=True,
                reject_media=True,
            )
        except ActionPlanV2Error as exc:
            raise RuntimeError(exc.code) from exc
        if intent_plan.required_actions != tuple(phases):
            raise RuntimeError("browser_action_intent_plan_mismatch")
    current_phase = await get_latest_phase(task_id) or "init"
    if current_phase == "completed":
        return
    if current_phase not in phase_order and current_phase != "init":
        raise RuntimeError(f"task_phase_invalid:{current_phase}")
    phase_fn = {
        "followed": adapter._follow,
        "liked": adapter._like,
        "commented": adapter._comment,
        "favorited": getattr(adapter, "_favorite", None),
        "reposted": adapter._repost,
    }
    canonical_uri = validated_platform_canonical_uri(platform, task.get("canonical_url"))
    profile_dir = f"/profiles/{platform}/account_{account_id}"
    proxy = None
    ctx = await pool.get_account_context(
        account_id,
        profile_dir,
        proxy,
        platform=platform,
    )
    await prepare_account_login(ctx, account_id, platform)
    page = await ctx.new_page()
    active_gate_binding = None
    active_execution_intent_kind = None

    def durable_gate_binding(snapshot):
        try:
            execution_plan = gate_execution_action_plan(snapshot)
            return (
                execution_plan.plan_hash,
                gate_requested_actions(snapshot),
                snapshot.execution_evidence_id,
                int(snapshot.execution_revision),
                snapshot.execution_intent_kind,
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise RealRunGateBlocked(
                "browser_real_run_gate_binding_invalid"
            ) from exc

    async def mark_intent_unknown(intent, phase_name: str, cause: BaseException):
        if intent is None:
            return
        try:
            await await_safety_settlement(
                mark_action_intent_unknown(
                    db=database,
                    intent=intent,
                    reason=(
                        f"{type(cause).__name__}: browser action outcome not proven"
                    ),
                )
            )
        except BaseException as intent_exc:
            structured_log(
                "error",
                "browser_action_intent_unknown_write_failed",
                task_id=task_id,
                action=phase_name,
                exception=intent_exc,
            )

    async def settle_intent_no_effect(
        intent,
        phase_name: str,
        cause: BaseException,
    ):
        if intent is None:
            return
        await await_safety_settlement(
            settle_action_intent(
                db=database,
                intent=intent,
                succeeded=False,
                outcome="rejected",
                error_message=(
                    f"browser_pre_mutation_failure:{type(cause).__name__}"
                ),
            )
        )

    try:
        lottery_url = validated_platform_navigation_url(platform, lottery_url)
        await install_main_frame_navigation_guard(page, platform)
        await page.goto(lottery_url, wait_until="domcontentloaded", timeout=30000)
        validated_platform_content_url(platform, page.url, canonical_uri)
        await install_main_frame_navigation_guard(page, platform, canonical_uri)
        await refresh_task_lease(task_id)
        await page.wait_for_timeout(2000)
        validated_platform_content_url(platform, page.url, canonical_uri)
        await detect_page_risk(page, account_id, platform)

        async def before_selector_mutation(_event: str) -> None:
            # Selector phases can contain more than one mutation-capable click
            # (for example repost + confirm, or comment submit). Re-check the
            # authoritative task gate immediately before every such click, not
            # merely once at the beginning of the phase.
            try:
                current_gate = await enforce_task_real_run_gate(
                    task, require_running=True
                )
                await refresh_task_lease(task_id)
                if durable_intents_required:
                    if (
                        active_gate_binding is None
                        or not active_execution_intent_kind
                    ):
                        raise RealRunGateBlocked(
                            "browser_action_intent_missing"
                        )
                    await renew_account_operation_lease(
                        db=database,
                        task_id=task_id,
                        account_id=account_id,
                        lottery_id=lottery_id,
                        worker_id=WORKER_ID,
                        execution_intent_kind=(
                            active_execution_intent_kind
                        ),
                    )
                    renewed_gate = await enforce_task_real_run_gate(
                        task, require_running=True
                    )
                    if (
                        durable_gate_binding(current_gate)
                        != active_gate_binding
                        or durable_gate_binding(renewed_gate)
                        != active_gate_binding
                    ):
                        raise RealRunGateBlocked(
                            "browser_real_run_binding_changed"
                        )
                validated_platform_content_url(platform, page.url, canonical_uri)
                await detect_page_risk(page, account_id, platform)
                validated_platform_content_url(platform, page.url, canonical_uri)
            except Exception as exc:
                raise SelectorMutationPreconditionFailed(_event, exc) from exc

        set_mutation_guard = getattr(adapter, "set_mutation_guard", None)
        if callable(set_mutation_guard):
            set_mutation_guard(before_selector_mutation)
        if current_phase == "init":
            remaining_phases = phases
        else:
            completed_index = phase_order.index(current_phase)
            remaining_phases = [
                phase for phase in phases if phase_order.index(phase) > completed_index
            ]
        for phase_name in remaining_phases:
            gate_snapshot = await enforce_task_real_run_gate(
                task, require_running=True
            )
            await refresh_task_lease(task_id)
            validated_platform_content_url(platform, page.url, canonical_uri)
            await detect_page_risk(page, account_id, platform)
            validated_platform_content_url(platform, page.url, canonical_uri)
            reset_mutation_tracking = getattr(adapter, "reset_mutation_tracking", None)
            if callable(reset_mutation_tracking):
                reset_mutation_tracking()
            active_intent = None
            active_gate_binding = None
            active_execution_intent_kind = None
            if durable_intents_required:
                initial_binding = durable_gate_binding(gate_snapshot)
                if (
                    intent_plan is None
                    or initial_binding[0] != intent_plan.plan_hash
                    or initial_binding[1] != intent_plan.required_actions
                ):
                    raise RealRunGateBlocked(
                        "browser_action_intent_plan_changed"
                    )
                active_execution_intent_kind = initial_binding[4]
                await renew_account_operation_lease(
                    db=database,
                    task_id=task_id,
                    account_id=account_id,
                    lottery_id=lottery_id,
                    worker_id=WORKER_ID,
                    execution_intent_kind=active_execution_intent_kind,
                )
                renewed_gate = await enforce_task_real_run_gate(
                    task, require_running=True
                )
                active_gate_binding = durable_gate_binding(renewed_gate)
                if active_gate_binding != initial_binding:
                    raise RealRunGateBlocked(
                        "browser_real_run_binding_changed"
                    )
                active_intent = await prepare_and_start_action_intent(
                    db=database,
                    task_id=task_id,
                    account_id=account_id,
                    lottery_id=lottery_id,
                    worker_id=WORKER_ID,
                    execution_intent_kind=active_execution_intent_kind,
                    action=phase_name,
                    payload={
                        "platform": platform,
                        "execution_path_id": intent_plan.execution_path_id,
                        "execution_evidence_id": active_gate_binding[2],
                        "execution_revision": active_gate_binding[3],
                        "target_hash": str(task.get("target_hash") or ""),
                        "config_hash": str(task.get("config_hash") or ""),
                        "action_payload": intent_plan.payload_for(phase_name),
                    },
                )
            try:
                phase_handler = phase_fn.get(phase_name)
                if not callable(phase_handler):
                    raise RuntimeError(f"platform_action_not_implemented:{phase_name}")
                await phase_handler(page)
                if active_intent is not None:
                    try:
                        await settle_action_intent(
                            db=database,
                            intent=active_intent,
                            succeeded=True,
                            outcome="ok",
                        )
                    except BaseException as exc:
                        await mark_intent_unknown(
                            active_intent,
                            phase_name,
                            exc,
                        )
                        await await_safety_settlement(
                            quarantine_external_action_outcome(
                                task_id=task_id,
                                account_id=account_id,
                                platform=platform,
                                action=phase_name,
                                cause=exc,
                            )
                        )
                        raise ExternalActionOutcomeUnknown(
                            platform, phase_name, exc
                        ) from exc
                    active_intent = None
                validated_platform_content_url(platform, page.url, canonical_uri)
                await detect_page_risk(page, account_id, platform)
                await save_phase(task_id, account_id, lottery_id, phase_name)
            except ExternalActionOutcomeUnknown:
                raise
            except SelectorMutationPreconditionFailed as exc:
                # The guard proves only that the *current* click did not start.
                # A multi-click phase (for example repost + confirm) may already
                # have issued an earlier mutation-capable click, in which case
                # the phase outcome is unknown and must be quarantined.
                if getattr(adapter, "mutation_started", None) is not True:
                    await settle_intent_no_effect(
                        active_intent,
                        phase_name,
                        exc,
                    )
                    raise
                await mark_intent_unknown(active_intent, phase_name, exc)
                unknown = ExternalActionOutcomeUnknown(platform, phase_name, exc)
                await await_safety_settlement(
                    quarantine_external_action_outcome(
                        task_id=task_id,
                        account_id=account_id,
                        platform=platform,
                        action=phase_name,
                        cause=exc,
                    )
                )
                raise unknown from exc
            except asyncio.CancelledError as exc:
                if getattr(adapter, "mutation_started", None) is False:
                    # Cancellation while reading selectors, delaying, typing or
                    # running the guard occurred before any mutation click.
                    await settle_intent_no_effect(
                        active_intent,
                        phase_name,
                        exc,
                    )
                    raise
                await mark_intent_unknown(active_intent, phase_name, exc)
                await await_safety_settlement(
                    quarantine_external_action_outcome(
                        task_id=task_id,
                        account_id=account_id,
                        platform=platform,
                        action=phase_name,
                        cause=exc,
                    )
                )
                raise
            except Exception as exc:
                if getattr(adapter, "mutation_started", None) is False:
                    await settle_intent_no_effect(
                        active_intent,
                        phase_name,
                        exc,
                    )
                    raise SelectorMutationPreconditionFailed(phase_name, exc) from exc
                await mark_intent_unknown(active_intent, phase_name, exc)
                unknown = ExternalActionOutcomeUnknown(platform, phase_name, exc)
                await await_safety_settlement(
                    quarantine_external_action_outcome(
                        task_id=task_id,
                        account_id=account_id,
                        platform=platform,
                        action=phase_name,
                        cause=exc,
                    )
                )
                raise unknown from exc
        try:
            await save_phase(task_id, account_id, lottery_id, "completed")
        except Exception as exc:
            unknown = ExternalActionOutcomeUnknown(platform, "task_completion", exc)
            await await_safety_settlement(
                quarantine_external_action_outcome(
                    task_id=task_id,
                    account_id=account_id,
                    platform=platform,
                    action="task_completion",
                    cause=exc,
                )
            )
            raise unknown from exc
    except Exception:
        await capture_failure_screenshot(page, task_id)
        raise
    finally:
        set_mutation_guard = getattr(adapter, "set_mutation_guard", None)
        if callable(set_mutation_guard):
            set_mutation_guard(None)
        try:
            await page.close()
        except Exception as exc:
            # Browser cleanup cannot reverse already-settled remote actions.
            structured_log("warning", "task_page_close_failed", task_id=task_id, exception=exc)


def real_executor_for_task(task: dict) -> str | None:
    """Compatibility introspection only; execution uses the path callable."""

    try:
        module = get_platform_module(task.get("platform"))
        _, executor = module.route("real_run", task.get("action_plan"))
    except PlatformRoutingError:
        return None
    return executor


async def execute_bilibili_api_real_task(task: dict):
    """Compatibility facade into Bilibili-owned mutation control flow."""

    from app.platform_modules.bilibili import (
        _execute_bilibili_api_real_owned as owned_real,
    )

    return await owned_real(
        task,
        runtime=_task_execution_services(),
    )


async def load_weibo_oauth_credential(
    account_id: int,
    *,
    expected_uid: str,
    expected_execution_revision: int,
):
    """Compatibility facade into Weibo-owned credential binding."""

    from app.platform_modules.weibo import (
        _load_weibo_oauth_credential_owned as owned_loader,
    )

    return await owned_loader(
        account_id,
        expected_uid=expected_uid,
        expected_execution_revision=expected_execution_revision,
        runtime=_task_execution_services(),
    )


def _weibo_handle_identity_key(value: str) -> str:
    """Compatibility facade for Weibo-owned handle normalization."""

    from app.platform_modules.weibo import (
        _weibo_handle_identity_key as owned_identity_key,
    )

    return owned_identity_key(value)


async def preflight_weibo_friend_mentions(
    client,
    plan,
    *,
    pre_resolved: dict[str, str] | None = None,
    on_progress=None,
) -> dict[str, str]:
    """Compatibility facade into Weibo-owned mention preflight."""

    from app.platform_modules.weibo import (
        _preflight_weibo_friend_mentions_owned as owned_preflight,
    )

    return await owned_preflight(
        client,
        plan,
        pre_resolved=pre_resolved,
        on_progress=on_progress,
    )


async def execute_weibo_oauth_real_task(task: dict) -> None:
    """Compatibility facade into Weibo-owned mutation control flow."""

    from app.platform_modules.weibo import (
        _execute_weibo_oauth_real_owned as owned_real,
    )

    await owned_real(
        task,
        runtime=_task_execution_services(),
    )


async def capture_failure_screenshot(page, task_id: str) -> str | None:
    path = None
    created_identity = None
    database_reference_possible = False
    try:
        clip = await ensure_page_screenshot_is_bounded(page)
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        path = evidence_screenshot_path(SCREENSHOT_DIR, task_id)
        screenshot_options = {"full_page": False}
        if clip is not None:
            screenshot_options["clip"] = clip
        screenshot_bytes = await page.screenshot(**screenshot_options)
        digest, created_identity = await write_evidence_file_cancellation_safe(
            path,
            screenshot_bytes,
        )
        async with database.transaction():
            await database.execute("UPDATE task_runs SET screenshot_path = :path WHERE task_id = :task_id", {"path": str(path), "task_id": task_id})
            row = await database.fetch_one("SELECT account_id, lottery_id FROM task_runs WHERE task_id = :task_id", {"task_id": task_id})
            await database.execute(
                """INSERT INTO evidence_files (evidence_type, task_id, account_id, lottery_id, file_path, sha256)
                   VALUES ('task_failure_screenshot', :task_id, :account_id, :lottery_id, :file_path, :sha256)""",
                {"task_id": task_id, "account_id": row["account_id"] if row else None, "lottery_id": row["lottery_id"] if row else None, "file_path": str(path), "sha256": digest},
            )
            event_id = await record_event(
                aggregate="task",
                aggregate_id=task_id,
                event_type="EvidenceCaptured",
                payload={"evidence_type": "task_failure_screenshot", "account_id": row["account_id"] if row else None, "lottery_id": row["lottery_id"] if row else None, "file_path": str(path), "sha256": digest},
                correlation_id=task_id,
            )
            if not event_id:
                raise RuntimeError("failure_screenshot_event_persistence_failed")
            # Preserve the file if commit outcome becomes uncertain. A possible
            # DB reference is safer than deleting evidence behind committed rows.
            database_reference_possible = True
        return str(path)
    except Exception as e:
        structured_log("error", "failure_screenshot_failed", task_id=task_id, exception=e)
        return None
    finally:
        if created_identity is not None and not database_reference_possible and path is not None:
            await remove_unpersisted_evidence(
                path,
                created_identity,
                task_id,
                "task_failure_screenshot",
            )


async def capture_shadow_screenshot(
    page,
    task_id: str,
    account_id: int,
    lottery_id: int,
    visible_phases: dict,
    *,
    validate_content=None,
) -> str | None:
    path = None
    created_identity = None
    database_reference_possible = False
    try:
        clip = await ensure_page_screenshot_is_bounded(page)
        SHADOW_SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        path = evidence_screenshot_path(SHADOW_SCREENSHOT_DIR, task_id)
        screenshot_options = {"full_page": False}
        if clip is not None:
            screenshot_options["clip"] = clip
        screenshot_bytes = await page.screenshot(**screenshot_options)
        if validate_content is not None:
            validate_content()
        digest, created_identity = await write_evidence_file_cancellation_safe(
            path,
            screenshot_bytes,
        )
        async with database.transaction():
            await database.execute("UPDATE task_runs SET screenshot_path = :path WHERE task_id = :task_id", {"path": str(path), "task_id": task_id})
            await database.execute(
                """INSERT INTO evidence_files (evidence_type, task_id, account_id, lottery_id, file_path, sha256)
                   VALUES ('shadow_run_screenshot', :task_id, :account_id, :lottery_id, :file_path, :sha256)""",
                {"task_id": task_id, "account_id": account_id, "lottery_id": lottery_id, "file_path": str(path), "sha256": digest},
            )
            event_id = await record_event(
                aggregate="task",
                aggregate_id=task_id,
                event_type="EvidenceCaptured",
                payload={"evidence_type": "shadow_run_screenshot", "account_id": account_id, "lottery_id": lottery_id, "file_path": str(path), "sha256": digest, "visible_phases": visible_phases},
                correlation_id=task_id,
            )
            if not event_id:
                raise RuntimeError("shadow_screenshot_event_persistence_failed")
            database_reference_possible = True
        return str(path)
    except Exception as e:
        structured_log("error", "shadow_screenshot_failed", task_id=task_id, exception=e)
        return None
    finally:
        if created_identity is not None and not database_reference_possible and path is not None:
            await remove_unpersisted_evidence(
                path,
                created_identity,
                task_id,
                "shadow_run_screenshot",
            )


async def ensure_page_screenshot_is_bounded(page) -> dict | None:
    """Return a bounded immutable clip for the upcoming screenshot."""
    evaluate = getattr(page, "evaluate", None)
    if not callable(evaluate):
        # Lightweight test doubles may omit evaluate; real Playwright pages do
        # not. Callers still use a viewport-only, non-full-page screenshot.
        return None
    dimensions = await evaluate(
        """() => {
          const root = document.documentElement;
          const body = document.body;
          return {
            width: Math.max(root?.scrollWidth || 0, body?.scrollWidth || 0, window.innerWidth || 0),
            height: Math.max(root?.scrollHeight || 0, body?.scrollHeight || 0, window.innerHeight || 0),
          };
        }"""
    )
    if not isinstance(dimensions, dict):
        raise RuntimeError("evidence_screenshot_dimensions_unavailable")
    try:
        width = int(dimensions.get("width") or 0)
        height = int(dimensions.get("height") or 0)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("evidence_screenshot_dimensions_invalid") from exc
    if (
        width <= 0
        or height <= 0
        or width > MAX_EVIDENCE_SCREENSHOT_WIDTH
        or height > MAX_EVIDENCE_SCREENSHOT_HEIGHT
        or width * height > MAX_EVIDENCE_SCREENSHOT_PIXELS
    ):
        raise RuntimeError("evidence_screenshot_dimensions_exceeded")
    return {"x": 0, "y": 0, "width": width, "height": height}


def evidence_screenshot_path(directory: Path, task_id: str) -> Path:
    """Build an in-directory evidence path from a bounded task identifier."""
    identifier = str(task_id or "")
    if (
        len(identifier) > 128
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", identifier)
        or ".." in identifier
    ):
        raise RuntimeError("evidence_screenshot_task_id_invalid")
    if not directory.is_absolute():
        raise RuntimeError("evidence_screenshot_directory_not_absolute")
    path = directory / f"{identifier}.png"
    if path.parent != directory:
        raise RuntimeError("evidence_screenshot_path_outside_root")
    return path


async def remove_unpersisted_evidence(
    path: Path,
    expected_identity: tuple[int, int, int, int],
    task_id: str,
    evidence_type: str,
) -> None:
    try:
        removed = await asyncio.to_thread(
            unlink_evidence_if_identity_matches,
            path,
            expected_identity,
        )
        if not removed and os.path.lexists(path):
            structured_log(
                "warning",
                "unpersisted_evidence_cleanup_identity_mismatch",
                task_id=task_id,
                evidence_type=evidence_type,
            )
    except Exception as exc:
        structured_log(
            "warning",
            "unpersisted_evidence_cleanup_failed",
            task_id=task_id,
            evidence_type=evidence_type,
            exception=exc,
        )


async def write_evidence_file_cancellation_safe(
    path: Path,
    screenshot_bytes: bytes,
) -> tuple[str, tuple[int, int, int, int]]:
    handoff = EvidenceWriteHandoff()
    write_task = asyncio.create_task(
        asyncio.to_thread(
            write_evidence_file_with_handoff,
            path,
            screenshot_bytes,
            handoff,
        ),
        name=f"evidence-writer:{path.name}",
    )

    # During asyncio shutdown the writer task itself may be cancelled in
    # addition to its caller. The underlying executor thread cannot be
    # cancelled, so make that cancellation visible to the synchronous writer;
    # it will remove any file it created before returning.
    write_task.add_done_callback(
        lambda task: handoff.cancellation_requested.set() if task.cancelled() else None
    )

    cancellation = None
    result = None
    writer_error = None
    while True:
        if write_task.done():
            try:
                result = write_task.result()
            except asyncio.CancelledError as exc:
                handoff.cancellation_requested.set()
                if cancellation is None:
                    cancellation = exc
            except BaseException as exc:  # includes a shutdown cancellation
                writer_error = exc
            break
        try:
            result = await asyncio.shield(write_task)
            break
        except asyncio.CancelledError as exc:
            handoff.cancellation_requested.set()
            if cancellation is None:
                cancellation = exc
            # Repeated cancellation must not abandon the executor thread. Keep
            # waiting until it either cleans its file or returns its identity.
            continue
        except BaseException as exc:
            writer_error = exc
            break

    if cancellation is None:
        if writer_error is not None:
            raise writer_error
        return result

    # If the executor job had started, it may keep running after its asyncio
    # Task is cancelled. Wait for its thread-owned handoff instead of relying
    # on Task.result(), which is permanently lost in that case. If it had not
    # started, the cancellation flag prevents a later start from creating a
    # file, so no wait is necessary.
    while handoff.started.is_set() and not handoff.finished.is_set():
        try:
            await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            handoff.cancellation_requested.set()
            continue

    durable_result = handoff.result or result
    if durable_result is not None:
        _, created_identity = durable_result
        cleanup_task = asyncio.create_task(
            asyncio.to_thread(
                unlink_evidence_if_identity_matches,
                path,
                created_identity,
            ),
            name=f"evidence-cleanup:{path.name}",
        )
        while True:
            if cleanup_task.done():
                try:
                    cleanup_task.result()
                except BaseException as exc:
                    structured_log(
                        "warning",
                        "cancelled_evidence_cleanup_failed",
                        file_path=str(path),
                        exception=exc,
                    )
                break
            try:
                await asyncio.shield(cleanup_task)
                break
            except asyncio.CancelledError:
                handoff.cancellation_requested.set()
                continue
            except Exception as exc:
                structured_log(
                    "warning",
                    "cancelled_evidence_cleanup_failed",
                    file_path=str(path),
                    exception=exc,
                )
                break
    raise cancellation


def write_evidence_file_with_handoff(
    path: Path,
    screenshot_bytes: bytes,
    handoff: EvidenceWriteHandoff,
) -> tuple[str, tuple[int, int, int, int]]:
    """Publish a writer result even if its asyncio wrapper loses delivery."""

    handoff.started.set()
    try:
        result = write_evidence_file_exclusive(
            path,
            screenshot_bytes,
            handoff.cancellation_requested,
        )
        handoff.publish_result(result)
        if handoff.cancellation_requested.is_set():
            _, created_identity = result
            try:
                unlink_evidence_if_identity_matches(path, created_identity)
            except Exception as cleanup_exc:
                raise RuntimeError("cancelled_evidence_cleanup_failed") from cleanup_exc
            raise RuntimeError("evidence_screenshot_write_cancelled")
        return result
    except BaseException as exc:
        handoff.error = exc
        raise
    finally:
        handoff.finished.set()


def write_evidence_file_exclusive(
    path: Path,
    screenshot_bytes: bytes,
    cancellation_requested: threading.Event | None = None,
) -> tuple[str, tuple[int, int, int, int]]:
    """Create one evidence file without following or overwriting any path."""
    if not isinstance(screenshot_bytes, bytes):
        raise RuntimeError("evidence_screenshot_bytes_unavailable")
    if not screenshot_bytes or len(screenshot_bytes) > MAX_EVIDENCE_SCREENSHOT_BYTES:
        raise RuntimeError("evidence_screenshot_size_exceeded")
    if cancellation_requested is not None and cancellation_requested.is_set():
        raise RuntimeError("evidence_screenshot_write_cancelled")
    directory_fd, directory_identity = open_locked_evidence_directory(path.parent)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    fd = -1
    created_identity = None
    try:
        # Platform Workers and Core use different non-root UIDs but share the
        # dedicated artifact GID. Parent directories are setgid; group-read is
        # therefore the minimum permission that lets Core serve this evidence.
        fd = os.open(path.name, flags, 0o640, dir_fd=directory_fd)
        created = os.fstat(fd)
        if not stat.S_ISREG(created.st_mode):
            raise RuntimeError("evidence_screenshot_not_regular")
        os.fchmod(fd, 0o640)
        created = os.fstat(fd)
        created_identity = (
            directory_identity[0],
            directory_identity[1],
            created.st_dev,
            created.st_ino,
        )
        digest = hashlib.sha256()
        view = memoryview(screenshot_bytes)
        offset = 0
        while offset < len(view):
            if cancellation_requested is not None and cancellation_requested.is_set():
                raise RuntimeError("evidence_screenshot_write_cancelled")
            end = min(offset + EVIDENCE_HASH_CHUNK_SIZE, len(view))
            chunk = view[offset:end]
            written = os.write(fd, chunk)
            if written <= 0:
                raise RuntimeError("evidence_screenshot_write_incomplete")
            digest.update(chunk[:written])
            offset += written
        os.fsync(fd)
        final = os.fstat(fd)
        if (final.st_dev, final.st_ino) != created_identity[2:] or final.st_size != len(view):
            raise RuntimeError("evidence_screenshot_write_incomplete")
        if cancellation_requested is not None and cancellation_requested.is_set():
            raise RuntimeError("evidence_screenshot_write_cancelled")
        fsync_evidence_directory(directory_fd)
        if cancellation_requested is not None and cancellation_requested.is_set():
            raise RuntimeError("evidence_screenshot_write_cancelled")
        return digest.hexdigest(), created_identity
    except Exception:
        if fd >= 0:
            os.close(fd)
            fd = -1
        if created_identity is not None:
            try:
                unlink_evidence_from_locked_directory(
                    directory_fd,
                    path.name,
                    created_identity,
                )
            except Exception as cleanup_exc:
                raise RuntimeError("evidence_screenshot_cleanup_failed") from cleanup_exc
        raise
    finally:
        if fd >= 0:
            os.close(fd)
        close_locked_evidence_directory(directory_fd)


def unlink_evidence_if_identity_matches(
    path: Path,
    expected_identity: tuple[int, int, int, int],
) -> bool:
    directory_fd, directory_identity = open_locked_evidence_directory(path.parent)
    try:
        if directory_identity != expected_identity[:2]:
            return False
        return unlink_evidence_from_locked_directory(
            directory_fd,
            path.name,
            expected_identity,
        )
    finally:
        close_locked_evidence_directory(directory_fd)


def open_locked_evidence_directory(directory: Path) -> tuple[int, tuple[int, int]]:
    """Pin every absolute directory component and take a cross-process lock."""
    if os.name != "posix" or fcntl is None or not hasattr(os, "O_NOFOLLOW"):
        raise RuntimeError("secure_evidence_directory_open_unsupported")
    absolute = Path(os.path.abspath(str(directory)))
    if not absolute.is_absolute():
        raise RuntimeError("evidence_screenshot_directory_not_absolute")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    current_fd = os.open(os.sep, flags)
    try:
        for part in absolute.parts[1:]:
            next_fd = os.open(part, flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        directory_stat = os.fstat(current_fd)
        if not stat.S_ISDIR(directory_stat.st_mode):
            raise RuntimeError("evidence_screenshot_directory_invalid")
        if directory_stat.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise RuntimeError("evidence_screenshot_directory_not_private")
        fcntl.flock(current_fd, fcntl.LOCK_EX)
        return current_fd, (directory_stat.st_dev, directory_stat.st_ino)
    except Exception:
        os.close(current_fd)
        raise


def close_locked_evidence_directory(directory_fd: int) -> None:
    try:
        if fcntl is not None:
            fcntl.flock(directory_fd, fcntl.LOCK_UN)
    finally:
        os.close(directory_fd)


def unlink_evidence_from_locked_directory(
    directory_fd: int,
    filename: str,
    expected_identity: tuple[int, int, int, int],
) -> bool:
    try:
        current = os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return True
    if (current.st_dev, current.st_ino) != expected_identity[2:] or not stat.S_ISREG(current.st_mode):
        return False
    os.unlink(filename, dir_fd=directory_fd)
    fsync_evidence_directory(directory_fd)
    return True


def fsync_evidence_directory(directory_fd: int) -> None:
    """Make a created or removed evidence directory entry crash-durable."""
    try:
        os.fsync(directory_fd)
    except OSError as exc:
        raise RuntimeError("evidence_screenshot_directory_fsync_failed") from exc


async def load_account_credential(account_id: int) -> str:
    row = await database.fetch_one("SELECT encrypted_credential FROM accounts WHERE id = :id", {"id": account_id})
    if not row or not row["encrypted_credential"]:
        raise ValueError(f"Account {account_id} has no imported login Cookie")
    credential_blob = row["encrypted_credential"]
    if isinstance(credential_blob, memoryview):
        credential_blob = credential_blob.tobytes()
    try:
        return cookie_vault.decrypt(credential_blob, aad=CREDENTIAL_AAD)
    except Exception as exc:
        # CookieVault.decrypt already supports the historical no-AAD encrypted
        # format. Treating an undecryptable database blob as plaintext would
        # both hide corruption and risk sending ciphertext as a Cookie header.
        raise ValueError("account_credential_decryption_failed") from exc


async def prepare_account_login(ctx, account_id: int, platform: str):
    credential = await load_account_credential(account_id)
    await inject_account_cookies(ctx, platform, credential)


def _require_int(task: dict, field: str) -> int:
    value = task.get(field)
    try:
        return int(value)
    except Exception as exc:
        raise InvalidTaskMessage(f"{field}_invalid") from exc


def validate_task_message(task: dict) -> dict:
    task_id = str(task.get("task_id") or "").strip()
    if not task_id:
        raise InvalidTaskMessage("task_id_required")
    account_id = _require_int(task, "account_id")
    lottery_id = _require_int(task, "lottery_id")
    platform = str(task.get("platform") or "bilibili").strip() or "bilibili"
    mode = normalize_task_mode(task)
    task["task_id"] = task_id
    task["account_id"] = str(account_id)
    task["lottery_id"] = str(lottery_id)
    normalized_platform = platform.lower()
    task["platform"] = normalized_platform
    task["mode"] = mode
    try:
        platform_module = get_platform_module(normalized_platform)
        execution_path, _ = platform_module.route(mode, task.get("action_plan"))
    except PlatformRoutingError as exc:
        raise InvalidTaskMessage(exc.code) from exc
    if mode == "dry_run" and execution_path.dry_run_requires_executable_plan:
        try:
            dry_plan = validate_action_plan_v2(
                task.get("action_plan"),
                reject_media=True,
            )
            if dry_plan.execution_path_id != execution_path.path_id:
                raise InvalidTaskMessage(
                    f"{normalized_platform}_execution_path_not_supported"
                )
        except ActionPlanV2Error as exc:
            raise InvalidTaskMessage(exc.code) from exc
    if "weibo_rip" in task:
        # Never accept or reserialize the retired plaintext queue field.
        task.pop("weibo_rip", None)
        raise InvalidTaskMessage("weibo_rip_plaintext_forbidden")
    encrypted_rip = task.get("weibo_rip_encrypted")
    if execution_path.credential_kind == "weibo_oauth" and mode == "real_run":
        weibo_rip_required = _platform_runtime_symbol("weibo_rip_required")

        try:
            plan = validate_action_plan_v2(task.get("action_plan"), reject_media=True)
            if plan.execution_path_id != execution_path.path_id:
                raise InvalidTaskMessage(
                    f"{normalized_platform}_execution_path_not_supported"
                )
            execution_intent_kind = str(
                task.get("execution_intent_kind") or ""
            ).strip()
            if execution_intent_kind == "repair":
                # The queue keeps the evidence-bound full plan. Only the
                # durable DB binding can authorize the repair subset, so this
                # untrusted structural precheck must not infer RIP scope from
                # the full plan. The real-run gate enforces required/not
                # applicable after validating the binding.
                if encrypted_rip is not None and not isinstance(
                    encrypted_rip,
                    str,
                ):
                    raise InvalidTaskMessage(
                        "weibo_rip_encrypted_invalid"
                    )
            else:
                rip_required = weibo_rip_required(
                    plan.required_actions
                )
                if rip_required and not isinstance(encrypted_rip, str):
                    raise InvalidTaskMessage("weibo_rip_encrypted_required")
                if rip_required and not encrypted_rip:
                    raise InvalidTaskMessage("weibo_rip_encrypted_required")
                if (
                    not rip_required
                    and encrypted_rip is not None
                    and encrypted_rip != ""
                ):
                    raise InvalidTaskMessage(
                        "weibo_rip_encrypted_not_applicable"
                    )
            task["weibo_rip_encrypted"] = encrypted_rip or ""
        except ActionPlanV2Error as exc:
            raise InvalidTaskMessage(exc.code) from exc
    elif encrypted_rip is not None and encrypted_rip != "":
        raise InvalidTaskMessage("weibo_rip_encrypted_not_applicable")
    else:
        task["weibo_rip_encrypted"] = ""
    return task


async def dead_letter_message(
    msg_id: str,
    task: dict,
    reason: str,
    *,
    stream_key: str = STREAM_KEY,
):
    task_id = _canonical_task_uuid(task)
    safe_reason = _safe_invalid_task_reason(reason)
    # Redis envelopes are untrusted.  Persisting the whole rejected mapping
    # would turn the dead-letter store into a credential/token sink whenever
    # a malformed producer injected an unexpected field.  Diagnostics need
    # only the finite transport identity; the authoritative plan remains in
    # MySQL and can be joined by task_id.
    # Even fields with apparently harmless names are attacker-controlled at
    # this boundary; a credential copied into ``platform`` or ``mode`` must
    # not become durable merely because the field name is allowlisted.  The
    # canonical task UUID is sufficient to join every authoritative diagnostic
    # field from MySQL.  Invalid UUIDs retain no envelope content at all.
    sanitized_task = {"task_id": task_id} if task_id is not None else {}
    payload = json.dumps(sanitized_task, ensure_ascii=False)
    try:
        await database.execute(
            """INSERT INTO failed_task_messages (stream_key, message_id, task_id, reason, payload)
               VALUES (:stream_key, :message_id, :task_id, :reason, :payload)""",
            {"stream_key": stream_key, "message_id": str(msg_id), "task_id": task_id, "reason": safe_reason, "payload": payload},
        )
    except Exception as exc:
        structured_log("error", "dead_letter_db_failed", message_id=msg_id, task_id=task_id, error=str(exc))
    try:
        await redis.xadd(
            "failed_task_messages",
            {"stream_key": stream_key, "message_id": str(msg_id), "task_id": task_id or "", "reason": safe_reason, "payload": payload},
        )
    except Exception as exc:
        structured_log("error", "dead_letter_stream_failed", message_id=msg_id, task_id=task_id, error=str(exc))


def _safe_invalid_task_reason(reason: object) -> str:
    """Keep only a finite diagnostic code, never an untrusted suffix/value."""

    candidate = str(reason or "").strip().casefold()
    prefix = candidate.partition(":")[0]
    if re.fullmatch(r"[a-z][a-z0-9_]{0,127}", prefix):
        return prefix
    return "invalid_task_message"


def _canonical_task_uuid(task: dict) -> str | None:
    """Return only the canonical UUID shape produced by Core dispatch.

    A Redis message is untrusted input.  In particular, merely supplying the
    task_id of another queued task must not grant authority to fail or release
    that task.  This lightweight shape check is followed by an authoritative
    database lookup and, for retained messages, Core recovery's immutable
    outbox/gate validation.
    """

    raw_task_id = str(task.get("task_id") or "").strip().lower()
    if not raw_task_id:
        return None
    try:
        parsed = uuid.UUID(raw_task_id)
    except (AttributeError, TypeError, ValueError):
        return None
    canonical = str(parsed)
    return canonical if raw_task_id == canonical else None


async def invalid_task_requires_authoritative_recovery(task: dict) -> bool:
    """Whether an invalid stream entry must remain pending for Core recovery.

    We intentionally do not settle the task here.  The recovery daemon owns
    the safe replay/terminal path: it locks the authoritative task, validates
    the immutable outbox payload and current real-run gates, then either
    rebuilds a clean message or marks a blocked queued task failed.  Retaining
    only an existing active task avoids permanently orphaning legitimate work
    without treating attacker-controlled message fields as settlement proof.
    """

    task_id = _canonical_task_uuid(task)
    if task_id is None:
        return False
    row = await database.fetch_one(
        "SELECT status FROM task_runs WHERE task_id = :task_id",
        {"task_id": task_id},
    )
    return bool(
        row
        and str(row_get(row, "status", "") or "").strip().lower()
        in {"queued", "running"}
    )


async def handle_invalid_task_message(
    msg_id: str,
    task: dict,
    reason: str,
    *,
    stream_key: str = STREAM_KEY,
    group_name: str = GROUP_NAME,
) -> bool:
    """Dead-letter an invalid entry and ack only when no active task needs it.

    Returns ``True`` when the message was acknowledged.  Database lookup
    failures deliberately propagate so the consumer leaves the entry pending;
    losing observability is preferable to acknowledging the only recovery
    trigger while authoritative state is unavailable.
    """

    if stream_key == STREAM_KEY:
        # Preserve the historical call shape for operational wrappers/tests
        # that patch this legacy helper without the newer lane keyword.
        await dead_letter_message(msg_id, task, reason)
    else:
        await dead_letter_message(
            msg_id,
            task,
            reason,
            stream_key=stream_key,
        )
    if await invalid_task_requires_authoritative_recovery(task):
        safe_reason = _safe_invalid_task_reason(reason)
        structured_log(
            "warning",
            "invalid_task_message_retained_for_authoritative_recovery",
            message_id=msg_id,
            task_id=_canonical_task_uuid(task),
            reason=safe_reason,
        )
        return False
    if reason == "weibo_rip_plaintext_forbidden":
        # XACK only removes the consumer-group pending reference; the stream
        # entry (and therefore the retired plaintext IP) remains readable.
        # There is no authoritative active task to recover on this branch, so
        # delete precisely the already-dead-lettered entry before acknowledging
        # it. If XDEL fails the entry remains pending for retry; if XACK fails
        # after deletion, recovery can safely acknowledge the PEL tombstone.
        # Active tasks take the retained branch above and must not be deleted
        # until Core recovery has rebuilt or terminally settled them.
        await redis.xdel(stream_key, msg_id)
    if stream_key == LEGACY_TASK_STREAM_KEY:
        # The compatibility stream remains the provenance source for Core
        # fan-out/recovery.  Retire only its PEL reference; platform lanes own
        # bounded terminal-entry retention.
        await redis.xack(stream_key, group_name, str(msg_id))
    else:
        await redis.eval(
            SAFE_TERMINAL_TASK_ACK_DELETE_LUA,
            1,
            stream_key,
            group_name,
            str(msg_id),
        )
    return True


async def execute_task_with_phases(task: dict, adapter, pool, stream_message_id: str | None = None):
    task_id = task.get("task_id")
    account_id = int(task.get("account_id"))
    lottery_id = int(task.get("lottery_id"))
    task_mode = normalize_task_mode(task)
    completion_screenshot_path = None
    claimed = False
    try:
        normalized_platform = str(task.get("platform") or "").strip().lower()
        try:
            platform_module = get_platform_module(normalized_platform)
            execution_path, _ = platform_module.route(
                task_mode, task.get("action_plan")
            )
        except PlatformRoutingError as exc:
            raise RuntimeError(exc.code) from exc
        if task_mode == "dry_run" and execution_path.dry_run_requires_executable_plan:
            try:
                dry_plan = validate_action_plan_v2(
                    task.get("action_plan"),
                    reject_media=True,
                )
            except ActionPlanV2Error as exc:
                raise RuntimeError(exc.code) from exc
            if (
                dry_plan.execution_path_id != execution_path.path_id
                or dry_plan.plan.get("executable") is not True
            ):
                raise RuntimeError(
                    f"{normalized_platform}_execution_path_not_supported"
                )
        if task_mode == "real_run":
            await enforce_task_real_run_gate(task)
            await ensure_account_can_run(account_id, task.get("platform", "bilibili"))
        binding = await mark_task_started(
            task_id,
            account_id,
            lottery_id,
            task_mode,
            stream_message_id,
            task_message=task,
        )
        claimed = True
        account_id = binding.account_id
        lottery_id = binding.lottery_id
        task_mode = binding.task_mode
        if task_mode == "dry_run":
            await execute_dry_run(
                task_id,
                account_id,
                lottery_id,
                requested_phases(task, require_plan=False),
                platform=task.get("platform", ""),
                action_plan=task.get("action_plan"),
            )
        elif task_mode == "shadow_run":
            completion_screenshot_path = await execution_path.execute(
                task_mode,
                task,
                adapter,
                pool,
                runtime=_task_execution_services(),
            )
        else:
            await execution_path.execute(
                task_mode,
                task,
                adapter,
                pool,
                runtime=_task_execution_services(),
            )
        try:
            finished = await mark_task_finished(task_id, True, screenshot_path=completion_screenshot_path)
        except Exception as exc:
            if task_mode == "real_run":
                unknown = ExternalActionOutcomeUnknown(task.get("platform"), "task_completion", exc)
                await quarantine_external_action_outcome(
                    task_id=task_id,
                    account_id=account_id,
                    platform=task.get("platform"),
                    action="task_completion",
                    cause=exc,
                )
                raise unknown from exc
            raise
        if task_mode == "real_run" and finished is not True:
            exc = TaskOwnershipLost(f"Task {task_id} became terminal before completion settlement")
            unknown = ExternalActionOutcomeUnknown(task.get("platform"), "task_completion", exc)
            await quarantine_external_action_outcome(
                task_id=task_id,
                account_id=account_id,
                platform=task.get("platform"),
                action="task_completion",
                cause=exc,
            )
            raise unknown from exc
        if task_mode == "shadow_run" and finished is True:
            # The materializer requires the exact shadow lease to have reached
            # its released terminal state, so it must run after settlement.
            materialize_for_shadow_task = _platform_runtime_symbol(
                "materialize_for_shadow_task"
            )
            await materialize_for_shadow_task(db=database, task_id=task_id)
        return True
    except TaskSettlementUnconfirmed:
        # Keeping the message pending is the durable recovery trigger.
        raise
    except AccountStatusPersistenceFailed as e:
        # Releasing a claimed real-run task would change an `executing`
        # account back to `ready` even though its risk state did not commit.
        # Keep the task pending so recovery can apply the existing real-run
        # breaker/account-quarantine path instead of performing generic
        # terminal cleanup.
        structured_log(
            "error",
            "account_status_settlement_unconfirmed",
            task_id=task_id,
            account_id=e.account_id,
            status=e.status,
            reason=e.reason,
        )
        raise TaskSettlementUnconfirmed(task_id, e) from e
    except TaskAlreadyTerminal as e:
        structured_log("info", "task_not_claimed_ack", task_id=task_id, reason=str(e))
        return True
    except TaskAlreadyClaimed as e:
        structured_log("info", "task_claim_token_retained", task_id=task_id, reason=str(e))
        raise TaskSettlementUnconfirmed(task_id, e) from e
    except Exception as e:
        if not claimed:
            structured_log(
                "warning",
                "unclaimed_task_failure_retained_for_recovery",
                task_id=task_id,
                exception=e,
            )
            raise TaskSettlementUnconfirmed(task_id, e) from e
        try:
            screenshot_row = await database.fetch_one(
                "SELECT screenshot_path FROM task_runs WHERE task_id = :task_id",
                {"task_id": task_id},
            )
        except Exception as screenshot_exc:
            screenshot_row = None
            structured_log(
                "error",
                "task_failure_screenshot_lookup_failed",
                task_id=task_id,
                exception=screenshot_exc,
            )
        try:
            settled = await mark_task_finished(
                task_id,
                False,
                str(e),
                screenshot_row["screenshot_path"] if screenshot_row else None,
                quarantine_account=bool(
                    getattr(e, "quarantine_account", False)
                ),
                account_failure_status=getattr(e, "account_status", None),
            )
            if settled is False:
                structured_log(
                    "info",
                    "task_failure_already_terminal",
                    task_id=task_id,
                )
        except TaskAlreadyTerminal as settle_exc:
            structured_log(
                "info",
                "task_failure_already_terminal",
                task_id=task_id,
                reason=str(settle_exc),
            )
        except TaskOwnershipLost as settle_exc:
            structured_log(
                "warning",
                "task_failure_settlement_unconfirmed",
                task_id=task_id,
                reason=str(settle_exc),
            )
            raise TaskSettlementUnconfirmed(task_id, settle_exc) from settle_exc
        except Exception as settle_exc:
            structured_log(
                "error",
                "task_failure_settlement_failed",
                task_id=task_id,
                exception=settle_exc,
            )
            raise TaskSettlementUnconfirmed(task_id, settle_exc) from settle_exc
        structured_log("error", "task_execution_failed", task_id=task_id, exception=e)
        return False


@dataclass
class _DispatchedTaskMessage:
    message_id: str
    task: dict
    stream_key: str
    group_name: str
    waiting_for_platform: bool = True


def _legacy_fanout_marker_key(source_message_id: str) -> str:
    marker_digest = hashlib.sha256(
        (
            f"{LEGACY_TASK_STREAM_KEY}:{str(source_message_id)}"
        ).encode("utf-8")
    ).hexdigest()
    return f"legacy_task_fanout:{marker_digest}"


def _legacy_fanout_marker_member(
    stream_key: str,
    message_id: str,
    task_id: str,
) -> str:
    return (
        f"{str(stream_key)}|{str(task_id).strip()}|"
        f"{str(message_id)}"
    )


def _has_valid_legacy_fanout_source(task: dict) -> bool:
    source_stream = str(
        task.get(LEGACY_SOURCE_STREAM_FIELD) or ""
    ).strip()
    source_message_id = str(
        task.get(LEGACY_SOURCE_MESSAGE_ID_FIELD) or ""
    ).strip()
    return bool(
        source_stream == LEGACY_TASK_STREAM_KEY
        and _REDIS_STREAM_ID_RE.fullmatch(source_message_id)
    )


async def _ack_settled_dispatched_message(
    dispatched: _DispatchedTaskMessage,
) -> None:
    """ACK a terminal delivery and retire its legacy provenance atomically."""

    task = dispatched.task
    if _has_valid_legacy_fanout_source(task):
        source_message_id = str(
            task[LEGACY_SOURCE_MESSAGE_ID_FIELD]
        ).strip()
        await redis.eval(
            SAFE_TERMINAL_FANOUT_ACK_DELETE_LUA,
            2,
            dispatched.stream_key,
            _legacy_fanout_marker_key(source_message_id),
            dispatched.group_name,
            dispatched.message_id,
            _legacy_fanout_marker_member(
                dispatched.stream_key,
                dispatched.message_id,
                str(task.get("task_id") or ""),
            ),
        )
        return
    await redis.eval(
        SAFE_TERMINAL_TASK_ACK_DELETE_LUA,
        1,
        dispatched.stream_key,
        dispatched.group_name,
        dispatched.message_id,
    )


async def _execute_dispatched_task(
    dispatched: _DispatchedTaskMessage,
    platform_lock: asyncio.Lock,
    pool: BrowserPool,
) -> None:
    """Run one validated entry and acknowledge only after safe settlement.

    The lock is per platform, not global: execution is serial for a platform
    but concurrent across platforms.  ``CancelledError`` is intentionally not
    converted to a normal failure and XACK is intentionally not in ``finally``;
    shutdown therefore leaves an interrupted entry pending for Core recovery.
    """

    task = dispatched.task
    msg_id = dispatched.message_id
    platform = str(task.get("platform") or "").strip().lower()
    try:
        async with platform_lock:
            # From this point the existing DB claim/lease and recovery contract
            # is authoritative.  The PEL refresher must no longer hide an
            # expired running lease from Core recovery.
            dispatched.waiting_for_platform = False
            selector_config = parse_json_field(task.get("selector_config")) or {}
            success = await execute_task_with_phases(
                task,
                get_adapter(platform, selector_config),
                pool,
                str(msg_id),
            )
            await _ack_settled_dispatched_message(dispatched)
            if not success:
                structured_log(
                    "error",
                    "task_failed",
                    task_id=task.get("task_id"),
                    platform=platform,
                )
    except asyncio.CancelledError:
        structured_log(
            "info",
            "platform_task_cancelled_pending_recovery",
            task_id=task.get("task_id"),
            platform=platform,
            message_id=msg_id,
        )
        raise
    except Exception as exc:
        # execute_task_with_phases deliberately raises when authoritative
        # settlement is unconfirmed.  Isolate that entry/platform without
        # acknowledging it or terminating sibling platform lanes.
        structured_log(
            "error",
            "platform_task_dispatch_error",
            task_id=task.get("task_id"),
            platform=platform,
            message_id=msg_id,
            exception=exc,
        )


async def _refresh_waiting_pending_entries(
    inflight: dict[asyncio.Task, _DispatchedTaskMessage],
    shutdown_event: asyncio.Event,
    *,
    stream_key: str = STREAM_KEY,
    group_name: str = GROUP_NAME,
) -> None:
    """Keep locally queued (but not running) PEL entries from false recovery."""

    while not shutdown_event.is_set():
        try:
            await asyncio.wait_for(
                shutdown_event.wait(), timeout=TASK_PENDING_REFRESH_SECONDS
            )
            return
        except asyncio.TimeoutError:
            pass

        message_ids = [
            dispatched.message_id
            for task, dispatched in tuple(inflight.items())
            if not task.done() and dispatched.waiting_for_platform
        ]
        if not message_ids:
            continue
        try:
            # JUSTID avoids returning/re-decoding payloads and, in Redis, does
            # not increment the delivery retry counter.  If the worker/event
            # loop dies this refresh stops and the normal 120s recovery path
            # becomes eligible again.
            await redis.xclaim(
                stream_key,
                group_name,
                CONSUMER_NAME,
                min_idle_time=0,
                message_ids=message_ids,
                justid=True,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A refresh failure must not make a valid task execute twice or be
            # acknowledged.  DB claim ownership remains the final arbiter if
            # recovery races this local waiter.
            structured_log(
                "warning",
                "waiting_task_pending_refresh_failed",
                stream=stream_key,
                group=group_name,
                message_count=len(message_ids),
                exception=exc,
            )


async def _wait_for_dispatch_capacity(
    binding: TaskStreamBinding,
    inflight: dict[asyncio.Task, _DispatchedTaskMessage],
    shutdown_event: asyncio.Event,
) -> bool:
    """Wait for one bounded slot while publishing bounded liveness evidence."""

    while len(inflight) >= TASK_DISPATCH_MAX_INFLIGHT:
        _record_task_lane_loop_progress(
            binding,
            "capacity_wait",
            inflight_count=len(inflight),
        )
        shutdown_waiter = asyncio.create_task(shutdown_event.wait())
        try:
            done, _ = await asyncio.wait(
                (*tuple(inflight), shutdown_waiter),
                return_when=asyncio.FIRST_COMPLETED,
                timeout=TASK_LANE_HEALTH_PROGRESS_INTERVAL_SECONDS,
            )
        finally:
            if not shutdown_waiter.done():
                shutdown_waiter.cancel()
                await asyncio.gather(shutdown_waiter, return_exceptions=True)
        for completed in done:
            if completed is not shutdown_waiter:
                inflight.pop(completed, None)
        if shutdown_event.is_set():
            _record_task_lane_loop_progress(
                binding,
                "shutdown",
                inflight_count=len(inflight),
            )
            return False
    _record_task_lane_loop_progress(
        binding,
        "capacity_available",
        inflight_count=len(inflight),
    )
    return not shutdown_event.is_set()


async def _ensure_task_stream_group(binding: TaskStreamBinding) -> None:
    await verify_redis_consumer_group(
        redis,
        stream_key=binding.stream_key,
        group_name=binding.group_name,
    )


def _validate_task_stream_envelope(
    binding: TaskStreamBinding,
    task: dict,
) -> None:
    try:
        validate_task_stream_message(binding, task)
    except ValueError as exc:
        raise InvalidTaskMessage(str(exc)) from exc


async def _task_stream_loop(
    binding: TaskStreamBinding,
    pool: BrowserPool,
    shutdown_event: asyncio.Event,
    platform_locks: dict[str, asyncio.Lock],
) -> None:
    """Consume one durable task lane with independent capacity and recovery."""

    inflight: dict[asyncio.Task, _DispatchedTaskMessage] = {}
    pending_refresh_task = asyncio.create_task(
        _refresh_waiting_pending_entries(
            inflight,
            shutdown_event,
            stream_key=binding.stream_key,
            group_name=binding.group_name,
        )
    )
    group_ready = False
    try:
        while not shutdown_event.is_set():
            if not group_ready:
                try:
                    await _ensure_task_stream_group(binding)
                    group_ready = True
                    _record_task_lane_success(
                        binding,
                        "consumer_group_verify",
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    _record_task_lane_failure(
                        binding,
                        "consumer_group_verify",
                        exc,
                    )
                    structured_log(
                        "error",
                        "task_stream_group_verify_failed",
                        stream=binding.stream_key,
                        group=binding.group_name,
                        exception=exc,
                    )
                    try:
                        await asyncio.wait_for(
                            shutdown_event.wait(),
                            timeout=5,
                        )
                    except asyncio.TimeoutError:
                        pass
                    continue
            if not await _wait_for_dispatch_capacity(
                binding,
                inflight,
                shutdown_event,
            ):
                break
            available = TASK_DISPATCH_MAX_INFLIGHT - len(inflight)
            read_count = min(TASK_STREAM_READ_COUNT, available)
            lane_operation = "xreadgroup"
            try:
                msgs = await redis.xreadgroup(
                    binding.group_name,
                    CONSUMER_NAME,
                    {binding.stream_key: ">"},
                    count=read_count,
                    block=1000,
                )
                _record_task_lane_success(
                    binding,
                    "xreadgroup",
                )
                if not msgs:
                    continue
                lane_operation = "dispatch"
                for stream_name, entries in msgs:
                    if str(stream_name) != binding.stream_key:
                        structured_log(
                            "error",
                            "task_stream_read_mismatch",
                            expected_stream=binding.stream_key,
                            actual_stream=str(stream_name),
                        )
                        continue
                    for msg_id, data in entries:
                        task = {k: v for k, v in data.items()}
                        structured_log(
                            "info",
                            "task_received",
                            task_id=task.get("task_id"),
                            message_id=msg_id,
                            stream=binding.stream_key,
                        )
                        try:
                            # Reject cross-protocol entries before parsing a
                            # full action plan or touching platform routing.
                            _validate_task_stream_envelope(binding, task)
                            # All queue input still crosses the existing
                            # untrusted-message boundary before dispatch.
                            task = validate_task_message(task)
                            _validate_task_stream_envelope(binding, task)
                        except InvalidTaskMessage as exc:
                            acknowledged = await handle_invalid_task_message(
                                msg_id,
                                task,
                                str(exc),
                                stream_key=binding.stream_key,
                                group_name=binding.group_name,
                            )
                            structured_log(
                                "error",
                                "task_message_dead_lettered",
                                message_id=msg_id,
                                reason=str(exc),
                                acknowledged=acknowledged,
                                stream=binding.stream_key,
                            )
                            continue

                        platform = task["platform"]
                        dispatched = _DispatchedTaskMessage(
                            message_id=str(msg_id),
                            task=task,
                            stream_key=binding.stream_key,
                            group_name=binding.group_name,
                        )
                        execution_task = asyncio.create_task(
                            _execute_dispatched_task(
                                dispatched,
                                platform_locks[platform],
                                pool,
                            )
                        )
                        inflight[execution_task] = dispatched

                        def discard_completed(completed: asyncio.Task) -> None:
                            inflight.pop(completed, None)

                        execution_task.add_done_callback(discard_completed)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _record_task_lane_failure(
                    binding,
                    lane_operation,
                    exc,
                )
                # Redis may be recreated while this process stays alive. Any
                # lane error revalidates its exact bootstrap-owned group before
                # the next read; a missing group keeps this lane fail-closed.
                group_ready = False
                structured_log(
                    "error",
                    "task_stream_loop_error",
                    stream=binding.stream_key,
                    group=binding.group_name,
                    exception=exc,
                )
                try:
                    await asyncio.wait_for(shutdown_event.wait(), timeout=5)
                except asyncio.TimeoutError:
                    pass
    except asyncio.CancelledError:
        # Main cancels this coroutine during shutdown.  Cleanup below cancels
        # every active/waiting entry and deliberately leaves it unacknowledged.
        pass
    finally:
        pending_refresh_task.cancel()
        execution_tasks = tuple(inflight)
        for execution_task in execution_tasks:
            execution_task.cancel()
        await asyncio.gather(
            pending_refresh_task, *execution_tasks, return_exceptions=True
        )


async def task_loop(
    pool: BrowserPool,
    shutdown_event: asyncio.Event,
    *,
    platforms=None,
):
    selected_platforms = normalize_platform_scope(
        "all" if platforms is None else platforms
    )
    registered = tuple(
        platform
        for platform in registered_platforms()
        if platform in selected_platforms
    )
    bindings = tuple(
        binding
        for binding in task_stream_bindings(include_legacy=False)
        if binding.platform in selected_platforms
    )
    topology_platforms = {
        binding.platform
        for binding in bindings
        if binding.platform is not None
    }
    if set(registered) != topology_platforms:
        raise RuntimeError(
            "task_stream_platform_registry_mismatch:"
            + ",".join(sorted(set(registered) ^ topology_platforms))
        )
    platform_locks = {platform: asyncio.Lock() for platform in registered}

    lane_tasks = tuple(
        asyncio.create_task(
            _task_stream_loop(
                binding,
                pool,
                shutdown_event,
                platform_locks,
            ),
            name=f"task-stream:{binding.stream_key}",
        )
        for binding in bindings
    )
    try:
        await asyncio.gather(*lane_tasks)
    except asyncio.CancelledError:
        # Each lane owns its local PEL tasks and will leave interrupted entries
        # pending when cancelled. Cleanup below waits for that contract.
        pass
    finally:
        for lane_task in lane_tasks:
            lane_task.cancel()
        await asyncio.gather(*lane_tasks, return_exceptions=True)


def parse_json_field(value):
    if isinstance(value, (dict, list)) or value is None:
        return value
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    try:
        return json.loads(value)
    except Exception:
        return None


def normalize_task_mode(task: dict) -> str:
    raw_mode = str(task.get("mode") or "").strip().lower()
    if raw_mode in {"dry_run", "shadow_run", "real_run"}:
        return raw_mode
    dry_run = str(task.get("dry_run", "1")).lower() in {"1", "true", "yes"}
    return "dry_run" if dry_run else "real_run"


def requested_phases(task: dict, require_plan: bool) -> list[str]:
    plan = parse_json_field(task.get("action_plan"))
    if not isinstance(plan, dict):
        plan = {}
    normalized_platform = str(task.get("platform") or "").strip().lower()
    try:
        platform_module = get_platform_module(normalized_platform or "bilibili")
    except PlatformRoutingError as exc:
        raise RuntimeError(exc.code) from exc
    selected_phases = platform_module.selected_phases(plan)
    if require_plan:
        if plan.get("review_required"):
            raise RuntimeError("Lottery rule requires review before real-run")
        if not selected_phases:
            raise RuntimeError("Lottery action plan is missing required actions")
    return selected_phases or list(platform_module.action_order)
