import asyncio
import hashlib
import json
import os
import re
import stat
import threading
import unicodedata
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from app.adapters.registry import get_adapter
from app.adapter_config import load_selector_config
from app.action_plan import (
    BILIBILI_API_EXECUTION_PATH,
    DOUYIN_ACTION_ORDER,
    WEIBO_ACTION_ORDER,
    WEIBO_MAX_UNIQUE_HANDLES,
    WEIBO_MANUAL_EXECUTION_PATH,
    WEIBO_OAUTH_EXECUTION_PATH,
    XIAOHONGSHU_REQUIRED_ACTIONS,
    ActionPlanV2Error,
    canonical_json_bytes,
    compute_bilibili_api_config_hash,
    compute_target_hash,
    validate_action_plan_v2,
)
from app.bilibili.preflight import (
    API_PREFLIGHT_KIND,
    bilibili_author_handle,
    run_readonly_api_preflight,
)
from app.bilibili.client import BilibiliApiActionOutcomeUnknown, BilibiliApiClient
from app.bilibili.config import BiliEngineConfig
from app.bilibili.executor import BilibiliApiExecutor
from app.bilibili.runtime import (
    API_TO_DPMS_PHASE,
    account_status_for_results,
    dpms_phases_to_api_actions,
    extract_bilibili_dynamic_id,
    parse_detail_card,
    validate_card_for_actions,
)
from app.browser_pool import BrowserPool
from app.db import database, execute_affected_rows, redis
from app.event_store.service import record_event
from app.evidence_storage import (
    SHADOW_EVIDENCE_DIR,
    TASK_FAILURE_EVIDENCE_DIR,
)
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
from app.services.execution_evidence import materialize_for_shadow_task
from app.safety import (
    AccountStatusPersistenceFailed,
    detect_page_risk,
    ensure_account_can_run,
    set_account_status,
)
from app.utils.log import structured_log
from app.utils.navigation_safety import (
    install_main_frame_navigation_guard,
    validated_platform_canonical_uri,
    validated_platform_content_url,
    validated_platform_navigation_url,
)
from app.utils.cookies import credential_to_cookie_header, inject_account_cookies
from app.utils.crypto import CREDENTIAL_AAD, cookie_vault
from app.weibo.client import (
    WeiboApiActionOutcomeUnknown,
    WeiboApiClient,
    WeiboApiRejected,
    build_weibo_mutation_request,
    status_identifier_from_canonical_uri,
)
from app.weibo.credentials import (
    WeiboOAuthCredentialError,
    decrypt_weibo_rip,
    parse_weibo_oauth_credential,
    weibo_rip_required,
)
from app.weibo.executor import (
    WeiboExecutionOutcomeUnknown,
    WeiboOAuthExecutor,
)

try:
    import fcntl
except ImportError:  # pragma: no cover - browser workers are deployed on Linux
    fcntl = None


STREAM_KEY = "lottery_tasks"
GROUP_NAME = "workers"
WORKER_ID = os.getenv("HOSTNAME") or f"worker-{os.getpid()}"
CONSUMER_NAME = WORKER_ID
PHASE_ORDER = ["followed", "liked", "commented", "reposted"]
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


class TaskAlreadyTerminal(Exception):
    pass


class BilibiliForwardedTargetRequiresReview(RuntimeError):
    """The API target differs from the dynamic that the operator reviewed."""

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


class BilibiliActionSettlementFailed(RuntimeError):
    """The API result is known, but its local audit settlement did not finish."""

    def __init__(self, action: str, action_result, cause: BaseException) -> None:
        self.action = action
        self.action_result = action_result
        self.reason = "confirmed_result_persistence_failed"
        super().__init__(f"bilibili_action_settlement_failed:{action}:{type(cause).__name__}")


class ExternalActionOutcomeUnknown(RuntimeError):
    """A browser mutation may have happened but was not durably settled."""

    def __init__(self, platform: str, action: str, cause: BaseException) -> None:
        self.platform = str(platform or "unknown").strip().lower() or "unknown"
        self.action = str(action or "unknown").strip().lower() or "unknown"
        self.reason = f"{self.platform}_{self.action}_outcome_unknown"
        super().__init__(
            f"external_action_outcome_unknown:{self.platform}:{self.action}:{type(cause).__name__}"
        )


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
    """Load successful actions only when their full task binding is exact."""

    rows = await database.fetch_all(
        """SELECT account_id, lottery_id, dynamic_id, action, phase
           FROM bilibili_action_ledger
           WHERE task_id = :task_id AND ok = 1 AND outcome = 'ok'""",
        {"task_id": task_id},
    )
    completed: set[str] = set()
    for row in rows:
        phase = str(row_get(row, "phase", "") or "").strip().lower()
        if phase not in PHASE_ORDER:
            raise RuntimeError("bilibili_action_ledger_phase_invalid")
        action = str(row_get(row, "action", "") or "").strip().lower()
        try:
            ledger_account_id = int(row_get(row, "account_id", 0) or 0)
            ledger_lottery_id = int(row_get(row, "lottery_id", 0) or 0)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("bilibili_action_ledger_binding_invalid") from exc
        if (
            ledger_account_id != account_id
            or ledger_lottery_id != lottery_id
            or str(row_get(row, "dynamic_id", "") or "").strip() != dynamic_id
            or API_TO_DPMS_PHASE.get(action) != phase
        ):
            raise RuntimeError("bilibili_action_ledger_binding_invalid")
        completed.add(phase)
    return completed


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
    try:
        await database.execute(
            """INSERT INTO bilibili_action_ledger
                 (task_id, account_id, lottery_id, dynamic_id, action, phase, code, outcome, message, ok, task_mode, source)
               VALUES
                 (:task_id, :account_id, :lottery_id, :dynamic_id, :action, :phase, :code, :outcome, :message, :ok, 'real_run', 'api_real_run')
               ON DUPLICATE KEY UPDATE
                 dynamic_id = VALUES(dynamic_id),
                 phase = VALUES(phase),
                 code = VALUES(code),
                 outcome = VALUES(outcome),
                 message = VALUES(message),
                 ok = VALUES(ok),
                 updated_at = CURRENT_TIMESTAMP""",
            {
                "task_id": task_id,
                "account_id": account_id,
                "lottery_id": lottery_id,
                "dynamic_id": dynamic_id,
                "action": action,
                "phase": phase,
                "code": code,
                "outcome": outcome,
                "message": message,
                "ok": 1 if ok else 0,
            },
        )
    except Exception as exc:
        structured_log(
            "warning",
            "bilibili_action_ledger_write_failed",
            task_id=task_id,
            lottery_id=lottery_id,
            action=action,
            error=str(exc),
        )
        raise


async def persist_bilibili_action_result(
    *,
    task_id: str,
    account_id: int,
    lottery_id: int,
    dynamic_id: str,
    action: str,
    action_result,
) -> None:
    """Durably settle one confirmed API result before another mutation starts."""

    phase = API_TO_DPMS_PHASE.get(action)
    await save_bilibili_action_ledger(
        task_id=task_id,
        account_id=account_id,
        lottery_id=lottery_id,
        dynamic_id=dynamic_id,
        action=action,
        phase=phase,
        code=action_result.code,
        outcome=action_result.outcome.value,
        message=action_result.message,
        ok=action_result.ok,
    )
    event_id = await record_event(
        aggregate="task",
        aggregate_id=task_id,
        event_type="BilibiliApiActionCompleted",
        payload={
            "account_id": account_id,
            "lottery_id": lottery_id,
            "dynamic_id": dynamic_id,
            "action": action,
            "code": action_result.code,
            "outcome": action_result.outcome.value,
            "message": action_result.message,
        },
        correlation_id=task_id,
    )
    if not event_id:
        raise RuntimeError("bilibili_action_event_persistence_failed")
    if phase and action_result.ok:
        await save_phase(task_id, account_id, lottery_id, phase)


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
        # Selector configuration is evidence for observation-only shadow runs.
        # Official OAuth/API real paths bind their own immutable capability and
        # endpoint contracts and must never inherit browser selector state.
        needs_selector_binding = task_mode == "shadow_run" and platform != "bilibili"
        if task_mode == "shadow_run" and platform == "bilibili":
            await validate_bilibili_api_shadow_claim(
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
                    raise TaskClaimConflict("shadow_task_selector_config_invalid")
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
    """Bind Bilibili Shadow to one immutable API contract and active lease."""

    if not isinstance(task_message, dict):
        raise TaskClaimConflict("shadow_task_message_missing")
    try:
        authoritative_plan = validate_action_plan_v2(
            row_get(lottery, "action_plan"), reject_media=True
        )
        message_plan = validate_action_plan_v2(
            task_message.get("action_plan"), reject_media=True
        )
    except ActionPlanV2Error as exc:
        raise TaskClaimConflict(f"shadow_task_{exc.code}") from exc
    if (
        authoritative_plan.execution_path_id != BILIBILI_API_EXECUTION_PATH
        or message_plan.execution_path_id != BILIBILI_API_EXECUTION_PATH
        or canonical_json_bytes(authoritative_plan.plan)
        != canonical_json_bytes(message_plan.plan)
    ):
        raise TaskClaimConflict("shadow_task_action_plan_mismatch")

    canonical_url = str(row_get(lottery, "canonical_url") or "").strip()
    target_hash = compute_target_hash(canonical_url)
    execution_revision = _claim_positive_int(
        row_get(account, "execution_revision"),
        "shadow_task_execution_revision_invalid",
    )
    if (
        _claim_positive_int(
            task_message.get("execution_revision"),
            "shadow_task_execution_revision_invalid",
        )
        != execution_revision
    ):
        raise TaskClaimConflict("shadow_task_execution_revision_mismatch")
    config_hash = compute_bilibili_api_config_hash(execution_revision)
    snapshot_id = _claim_positive_int(
        row_get(lottery, "authoritative_rule_snapshot_id"),
        "shadow_task_rule_snapshot_binding_invalid",
    )
    task_snapshot_id = _claim_positive_int(
        row_get(task_row, "rule_snapshot_id"),
        "shadow_task_rule_snapshot_binding_invalid",
    )
    message_snapshot_id = _claim_positive_int(
        task_message.get("rule_snapshot_id"),
        "shadow_task_rule_snapshot_binding_invalid",
    )
    exact_strings = (
        ("platform", str(task_message.get("platform") or "").strip().lower(), "bilibili"),
        ("raw_url", str(task_message.get("raw_url") or "").strip(), str(row_get(lottery, "raw_url") or "").strip()),
        ("canonical_url", str(task_message.get("canonical_url") or "").strip(), canonical_url),
        ("rule_hash", str(task_message.get("rule_hash") or "").strip(), authoritative_plan.rule_hash),
        ("action_plan_hash", str(task_message.get("action_plan_hash") or "").strip(), authoritative_plan.plan_hash),
        ("execution_path_id", str(task_message.get("execution_path_id") or "").strip(), BILIBILI_API_EXECUTION_PATH),
        ("target_hash", str(task_message.get("target_hash") or "").strip(), target_hash),
        ("config_hash", str(task_message.get("config_hash") or "").strip(), config_hash),
    )
    for field, message_value, expected in exact_strings:
        if not message_value or message_value != expected:
            raise TaskClaimConflict(f"shadow_task_{field}_mismatch")
    if (
        snapshot_id != task_snapshot_id
        or task_snapshot_id != message_snapshot_id
        or message_snapshot_id != authoritative_plan.rule_snapshot_id
        or str(row_get(task_row, "rule_hash") or "").strip() != authoritative_plan.rule_hash
        or str(row_get(lottery, "rule_hash") or "").strip() != authoritative_plan.rule_hash
        or str(row_get(task_row, "action_plan_hash") or "").strip() != authoritative_plan.plan_hash
        or str(row_get(lottery, "action_plan_hash") or "").strip() != authoritative_plan.plan_hash
        or str(row_get(task_row, "execution_path_id") or "").strip()
        != BILIBILI_API_EXECUTION_PATH
        or str(row_get(task_row, "target_hash") or "").strip() != target_hash
        or str(row_get(task_row, "config_hash") or "").strip() != config_hash
        or str(row_get(account, "platform") or "").strip().lower() != "bilibili"
    ):
        raise TaskClaimConflict("shadow_task_api_binding_mismatch")

    lease_id = str(row_get(task_row, "account_lease_id") or "").strip()
    lease_generation = _claim_positive_int(
        row_get(task_row, "account_lease_generation"),
        "shadow_task_account_lease_binding_invalid",
    )
    if (
        str(task_message.get("account_lease_id") or "").strip() != lease_id
        or _claim_positive_int(
            task_message.get("account_lease_generation"),
            "shadow_task_account_lease_binding_invalid",
        )
        != lease_generation
    ):
        raise TaskClaimConflict("shadow_task_account_lease_binding_invalid")
    lease = await database.fetch_one(
        """SELECT lease_id, account_id, generation, operation_kind, owner_id, task_id,
                  CASE WHEN expires_at > NOW() THEN 1 ELSE 0 END AS lease_active,
                  CASE WHEN released_at IS NULL THEN 1 ELSE 0 END AS lease_unreleased,
                  CASE WHEN generation = (
                    SELECT MAX(newest.generation) FROM account_operation_leases newest
                    WHERE newest.account_id = :account_id
                  ) THEN 1 ELSE 0 END AS lease_latest_generation,
                  (SELECT COUNT(*) FROM account_operation_leases live
                   WHERE live.account_id = :account_id AND live.released_at IS NULL
                     AND live.expires_at > NOW()) AS active_account_lease_count
           FROM account_operation_leases
           WHERE lease_id = :lease_id AND account_id = :account_id
             AND generation = :generation
           FOR UPDATE""",
        {
            "lease_id": lease_id,
            "account_id": _claim_positive_int(row_get(task_row, "account_id"), "shadow_task_account_binding_invalid"),
            "generation": lease_generation,
        },
    )
    task_id = str(row_get(task_row, "task_id") or "").strip()
    if (
        not lease
        or str(row_get(lease, "lease_id") or "").strip() != lease_id
        or _claim_positive_int(row_get(lease, "account_id"), "shadow_task_account_lease_binding_invalid")
        != _claim_positive_int(row_get(task_row, "account_id"), "shadow_task_account_binding_invalid")
        or _claim_positive_int(row_get(lease, "generation"), "shadow_task_account_lease_binding_invalid")
        != lease_generation
        or str(row_get(lease, "operation_kind") or "").strip().lower() != "shadow_run"
        or str(row_get(lease, "owner_id") or "").strip() != task_id
        or str(row_get(lease, "task_id") or "").strip() != task_id
        or int(row_get(lease, "lease_active", 0) or 0) != 1
        or int(row_get(lease, "lease_unreleased", 0) or 0) != 1
        or int(row_get(lease, "lease_latest_generation", 0) or 0) != 1
        or int(row_get(lease, "active_account_lease_count", 0) or 0) != 1
        or int(row_get(task_row, "reconciliation_required", 0) or 0) != 0
    ):
        raise TaskClaimConflict("shadow_task_account_lease_binding_invalid")


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

    manual_platform = str(
        row_get(lottery, "platform") or ""
    ).strip().lower()
    if manual_platform in {"douyin", "weibo", "xiaohongshu"}:
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


def _real_success_intents_match_reviewed_plan(
    *, task_row, lottery, intent_rows
) -> bool:
    """Prove every reviewed API mutation has one confirmed durable intent.

    A caller saying ``success=True`` is not authority.  Settlement revalidates
    the locked lottery's immutable Action Plan and requires a one-to-one set of
    confirmed external intents before it may release the real-run fence.
    """

    platform = str(row_get(lottery, "platform") or "").strip().lower()
    if platform not in {"bilibili", "weibo"}:
        return False
    try:
        plan = validate_action_plan_v2(
            row_get(lottery, "action_plan"), reject_media=True
        )
    except (ActionPlanV2Error, TypeError, ValueError):
        return False
    task_plan_hash = str(row_get(task_row, "action_plan_hash") or "").strip()
    lottery_plan_hash = str(row_get(lottery, "action_plan_hash") or "").strip()
    expected_execution_path = (
        BILIBILI_API_EXECUTION_PATH
        if platform == "bilibili"
        else WEIBO_OAUTH_EXECUTION_PATH
    )
    if (
        plan.execution_path_id != expected_execution_path
        or str(plan.plan.get("platform") or "").strip().lower() != platform
        or not task_plan_hash
        or task_plan_hash != plan.plan_hash
        or lottery_plan_hash != plan.plan_hash
    ):
        return False

    if platform == "bilibili":
        try:
            expected_actions = dpms_phases_to_api_actions(list(plan.required_actions))
        except (TypeError, ValueError):
            return False
    else:
        expected_actions = list(plan.required_actions)
    if not expected_actions or len(expected_actions) != len(plan.required_actions):
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
        if binding.task_mode == "real_run":
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
        any_succeeded = any(
            certainty == "confirmed_effect" for _status, certainty in intent_statuses
        )
        any_started_or_unknown = any(
            certainty == "unknown" for _status, certainty in intent_statuses
        )
        partial_real_failure = (
            binding.task_mode == "real_run"
            and not success
            and (any_succeeded or any_started_or_unknown)
        )
        inconsistent_real_success = bool(
            binding.task_mode == "real_run"
            and success
            and not _real_success_intents_match_reviewed_plan(
                task_row=row,
                lottery=lottery,
                intent_rows=intent_rows,
            )
        )
        real_reconciliation_required = bool(
            quarantine_account and not success and binding.task_mode == "real_run"
        ) or partial_real_failure or inconsistent_real_success or invalid_intent_state or int(
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
        # generation they owned. Ambiguous/partially successful real failures
        # retain the append-only lease and lottery lock for reconciliation.
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
                    "operation_kind": binding.task_mode,
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
    normalized_platform = str(platform or "").strip().lower()
    if normalized_platform == "weibo":
        try:
            preliminary_plan = validate_action_plan_v2(
                action_plan,
                require_executable=False,
                reject_media=True,
            )
            if preliminary_plan.execution_path_id == WEIBO_MANUAL_EXECUTION_PATH:
                raise RuntimeError("weibo_manual_shadow_only")
            validated_plan = validate_action_plan_v2(
                action_plan,
                reject_media=True,
            )
        except ActionPlanV2Error as exc:
            raise RuntimeError(exc.code) from exc
        if validated_plan.execution_path_id != WEIBO_OAUTH_EXECUTION_PATH:
            raise RuntimeError("weibo_execution_path_not_supported")
        if tuple(phases) != validated_plan.required_actions:
            raise RuntimeError("weibo_dry_run_phase_binding_mismatch")
    elif normalized_platform in {"douyin", "xiaohongshu"}:
        raise RuntimeError(f"{normalized_platform}_manual_shadow_only")
    for phase_name in phases:
        await asyncio.sleep(0.2)
        await save_phase(task_id, account_id, lottery_id, phase_name)
    await save_phase(task_id, account_id, lottery_id, "completed")
    structured_log("info", "dry_run_task_completed", task_id=task_id, account_id=account_id, lottery_id=lottery_id)


async def execute_bilibili_api_shadow(task: dict) -> None:
    """Persist one GET-only Bilibili API observation; never open a browser."""

    task_id = str(task.get("task_id") or "").strip()
    account_id = int(task.get("account_id"))
    lottery_id = int(task.get("lottery_id"))
    try:
        plan = validate_action_plan_v2(task.get("action_plan"), reject_media=True)
    except ActionPlanV2Error as exc:
        raise RuntimeError(f"bilibili_shadow_{exc.code}") from exc
    if plan.execution_path_id != BILIBILI_API_EXECUTION_PATH:
        raise RuntimeError("bilibili_shadow_execution_path_not_supported")
    dynamic_id = extract_bilibili_dynamic_id(task.get("raw_url"), task.get("canonical_url"))
    authority = await database.fetch_one(
        """SELECT execution_revision, platform, status
           FROM accounts WHERE id = :account_id""",
        {"account_id": account_id},
    )
    execution_revision = _claim_positive_int(
        row_get(authority, "execution_revision"),
        "bilibili_shadow_execution_revision_invalid",
    )
    config_hash = compute_bilibili_api_config_hash(execution_revision)
    if (
        str(row_get(authority, "platform") or "").strip().lower() != "bilibili"
        or str(row_get(authority, "status") or "").strip().lower() != "ready"
        or str(task.get("config_hash") or "").strip() != config_hash
        or str(task.get("target_hash") or "").strip()
        != compute_target_hash(str(task.get("canonical_url") or "").strip())
    ):
        raise RuntimeError("bilibili_shadow_authority_changed")
    cookie_header = credential_to_cookie_header(await load_account_credential(account_id))
    if not cookie_header:
        raise RuntimeError(f"Account {account_id} has no usable Bilibili Cookie")
    preflight = await run_readonly_api_preflight(
        cookie_header=cookie_header,
        dynamic_id=dynamic_id,
        required_actions=plan.required_actions,
        execution_revision=execution_revision,
        config_hash=config_hash,
        expected_follow_handle=(
            plan.follow_target_handle if "followed" in plan.required_actions else None
        ),
    )
    observation_json = json.dumps(
        preflight.observation,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    async with database.transaction():
        current = await database.fetch_one(
            """SELECT tr.status, tr.worker_id, tr.account_id, tr.lottery_id,
                      tr.execution_path_id, tr.target_hash, tr.rule_hash,
                      tr.action_plan_hash, tr.config_hash,
                      tr.account_lease_id, tr.account_lease_generation,
                      a.execution_revision,
                       CASE WHEN lease.expires_at > NOW() THEN 1 ELSE 0 END AS lease_active,
                       CASE WHEN lease.released_at IS NULL THEN 1 ELSE 0 END AS lease_unreleased,
                       CASE WHEN lease.generation = (
                         SELECT MAX(newest.generation)
                         FROM account_operation_leases newest
                         WHERE newest.account_id = tr.account_id
                       ) THEN 1 ELSE 0 END AS lease_latest_generation,
                       (SELECT COUNT(*) FROM account_operation_leases live
                        WHERE live.account_id = tr.account_id
                          AND live.released_at IS NULL
                          AND live.expires_at > NOW()) AS active_account_lease_count,
                       lease.operation_kind, lease.owner_id, lease.task_id AS lease_task_id
               FROM task_runs tr
               JOIN accounts a ON a.id = tr.account_id
               JOIN account_operation_leases lease
                 ON lease.lease_id = tr.account_lease_id
                AND lease.account_id = tr.account_id
                AND lease.generation = tr.account_lease_generation
               WHERE tr.task_id = :task_id
               FOR UPDATE""",
            {"task_id": task_id},
        )
        if (
            not current
            or str(row_get(current, "status") or "").strip().lower() != "running"
            or str(row_get(current, "worker_id") or "").strip() != WORKER_ID
            or int(row_get(current, "account_id") or 0) != account_id
            or int(row_get(current, "lottery_id") or 0) != lottery_id
            or str(row_get(current, "execution_path_id") or "").strip()
            != BILIBILI_API_EXECUTION_PATH
            or str(row_get(current, "target_hash") or "").strip()
            != str(task.get("target_hash") or "").strip()
            or str(row_get(current, "rule_hash") or "").strip() != plan.rule_hash
            or str(row_get(current, "action_plan_hash") or "").strip() != plan.plan_hash
            or str(row_get(current, "config_hash") or "").strip() != config_hash
            or int(row_get(current, "execution_revision") or 0) != execution_revision
            or str(row_get(current, "account_lease_id") or "").strip()
            != str(task.get("account_lease_id") or "").strip()
            or int(row_get(current, "account_lease_generation") or 0)
            != _claim_positive_int(
                task.get("account_lease_generation"),
                "bilibili_shadow_account_lease_binding_invalid",
            )
            or str(row_get(current, "operation_kind") or "").strip().lower()
            != "shadow_run"
            or str(row_get(current, "owner_id") or "").strip() != task_id
            or str(row_get(current, "lease_task_id") or "").strip() != task_id
            or int(row_get(current, "lease_active", 0) or 0) != 1
            or int(row_get(current, "lease_unreleased", 0) or 0) != 1
            or int(row_get(current, "lease_latest_generation", 0) or 0) != 1
            or int(row_get(current, "active_account_lease_count", 0) or 0) != 1
        ):
            raise TaskOwnershipLost("bilibili_shadow_binding_changed")
        await database.execute(
            """UPDATE task_runs
               SET preflight_observation = :observation,
                   preflight_observation_kind = :kind,
                   preflight_observation_hash = :observation_hash
               WHERE task_id = :task_id AND status = 'running'
                 AND worker_id = :worker_id""",
            {
                "task_id": task_id,
                "worker_id": WORKER_ID,
                "observation": observation_json,
                "kind": API_PREFLIGHT_KIND,
                "observation_hash": preflight.observation_hash,
            },
        )
        persisted = await database.fetch_one(
            """SELECT preflight_observation, preflight_observation_kind,
                      preflight_observation_hash
               FROM task_runs WHERE task_id = :task_id""",
            {"task_id": task_id},
        )
        if (
            not persisted
            or str(row_get(persisted, "preflight_observation_kind") or "").strip()
            != API_PREFLIGHT_KIND
            or str(row_get(persisted, "preflight_observation_hash") or "").strip()
            != preflight.observation_hash
            or parse_json_field(row_get(persisted, "preflight_observation"))
            != preflight.observation
        ):
            raise RuntimeError("bilibili_shadow_observation_persistence_failed")
    event_id = await record_event(
        aggregate="task",
        aggregate_id=task_id,
        event_type="TaskApiShadowPreflightObserved",
        payload={
            "account_id": account_id,
            "lottery_id": lottery_id,
            "probe_kind": API_PREFLIGHT_KIND,
            "observation_hash": preflight.observation_hash,
            "target_identity": preflight.observation["target_identity"],
            "required_actions": list(plan.required_actions),
            "side_effects": False,
        },
        correlation_id=task_id,
    )
    if not event_id:
        raise RuntimeError("bilibili_shadow_observation_event_persistence_failed")
    await save_phase(task_id, account_id, lottery_id, "completed")


async def execute_shadow_run(task: dict, adapter, pool):
    task_id = task.get("task_id")
    account_id = int(task.get("account_id"))
    lottery_id = int(task.get("lottery_id"))
    lottery_url = task.get("raw_url") or task.get("canonical_url")
    platform = task.get("platform", "bilibili")
    if str(platform or "").strip().lower() == "bilibili":
        await execute_bilibili_api_shadow(task)
        return None
    canonical_uri = validated_platform_canonical_uri(platform, task.get("canonical_url"))
    profile_dir = f"/profiles/{platform}/account_{account_id}"
    proxy = None
    phases = requested_phases(task, require_plan=False)
    ctx = await pool.get_account_context(account_id, profile_dir, proxy)
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
        manual_confirmation_required = bool(
            getattr(adapter, "MANUAL_CONFIRMATION_REQUIRED", False)
        )
        capability_block_reason = (
            (
                "weibo_selector_observation_only"
                if str(platform or "").strip().lower() == "weibo"
                else platform_real_run_block_reason(platform)
            )
            if manual_confirmation_required
            else None
        )
        real_run_capable = (
            capability_block_reason is None
            and bool(getattr(adapter, "REAL_ACTIONS", False))
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
    phases = requested_phases(task, require_plan=True)
    current_phase = await get_latest_phase(task_id) or "init"
    if current_phase == "completed":
        return
    if current_phase not in PHASE_ORDER and current_phase != "init":
        raise RuntimeError(f"task_phase_invalid:{current_phase}")
    phase_fn = {
        "followed": adapter._follow,
        "liked": adapter._like,
        "commented": adapter._comment,
        "reposted": adapter._repost,
    }
    platform = task.get("platform", "bilibili")
    canonical_uri = validated_platform_canonical_uri(platform, task.get("canonical_url"))
    profile_dir = f"/profiles/{platform}/account_{account_id}"
    proxy = None
    ctx = await pool.get_account_context(account_id, profile_dir, proxy)
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

        async def before_selector_mutation(_event: str) -> None:
            # Selector phases can contain more than one mutation-capable click
            # (for example repost + confirm, or comment submit). Re-check the
            # authoritative task gate immediately before every such click, not
            # merely once at the beginning of the phase.
            try:
                await enforce_task_real_run_gate(task, require_running=True)
                await refresh_task_lease(task_id)
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
            completed_index = PHASE_ORDER.index(current_phase)
            remaining_phases = [phase for phase in phases if PHASE_ORDER.index(phase) > completed_index]
        for phase_name in remaining_phases:
            await enforce_task_real_run_gate(task, require_running=True)
            await refresh_task_lease(task_id)
            validated_platform_content_url(platform, page.url, canonical_uri)
            await detect_page_risk(page, account_id, platform)
            validated_platform_content_url(platform, page.url, canonical_uri)
            reset_mutation_tracking = getattr(adapter, "reset_mutation_tracking", None)
            if callable(reset_mutation_tracking):
                reset_mutation_tracking()
            try:
                await phase_fn[phase_name](page)
                validated_platform_content_url(platform, page.url, canonical_uri)
                await detect_page_risk(page, account_id, platform)
                await save_phase(task_id, account_id, lottery_id, phase_name)
            except SelectorMutationPreconditionFailed as exc:
                # The guard proves only that the *current* click did not start.
                # A multi-click phase (for example repost + confirm) may already
                # have issued an earlier mutation-capable click, in which case
                # the phase outcome is unknown and must be quarantined.
                if getattr(adapter, "mutation_started", None) is not True:
                    raise
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
                    raise
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
                    raise SelectorMutationPreconditionFailed(phase_name, exc) from exc
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


def uses_bilibili_api_real_task(task: dict) -> bool:
    return str(task.get("platform") or "").lower() == "bilibili"


def uses_weibo_oauth_real_task(task: dict) -> bool:
    # The real-run gate independently requires the exact OAuth execution path;
    # this predicate only selects the platform executor after that proof.
    return str(task.get("platform") or "").strip().lower() == "weibo"


async def execute_bilibili_api_real_task(task: dict):
    task_id = task.get("task_id")
    account_id = int(task.get("account_id"))
    lottery_id = int(task.get("lottery_id"))
    try:
        validated_plan = validate_action_plan_v2(task.get("action_plan"), reject_media=True)
    except ActionPlanV2Error as exc:
        raise RuntimeError(f"bilibili_{exc.code}") from exc
    phases = list(validated_plan.required_actions)
    dynamic_id = extract_bilibili_dynamic_id(task.get("raw_url"), task.get("canonical_url"))
    current_phase = await get_latest_phase(task_id) or "init"
    if current_phase == "completed":
        return
    if current_phase not in PHASE_ORDER and current_phase != "init":
        raise RuntimeError(f"task_phase_invalid:{current_phase}")
    completed_phases = await get_completed_bilibili_phases(
        task_id,
        account_id=account_id,
        lottery_id=lottery_id,
        dynamic_id=dynamic_id,
    )
    if current_phase != "init" and not completed_phases:
        # Before the per-action ledger existed, the API executor used a
        # different comment/repost order than task_phases. Inferring completed
        # work from that single marker can either skip a required action or
        # repeat a remote mutation, so legacy in-flight work needs explicit
        # reconciliation instead of automatic resume.
        raise RuntimeError("bilibili_legacy_phase_requires_reconciliation")
    remaining_phases = [phase for phase in phases if phase not in completed_phases]
    if not remaining_phases:
        await save_phase(task_id, account_id, lottery_id, "completed")
        return

    actions = dpms_phases_to_api_actions(remaining_phases)
    cookie_header = credential_to_cookie_header(await load_account_credential(account_id))
    if not cookie_header:
        raise RuntimeError(f"Account {account_id} has no usable Bilibili Cookie")

    async with BilibiliApiClient(cookie_header, config=BiliEngineConfig()) as client:
        if not await client.check_login():
            await set_account_status(account_id, "login_required", "bilibili_cookie_invalid")
            raise RuntimeError("Bilibili account Cookie is invalid or expired")

        detail = await client.get_dynamic_detail(dynamic_id)
        if int(detail.get("code", -1)) != 0:
            raise RuntimeError(f"Bilibili dynamic detail failed: code={detail.get('code')}")
        card = parse_detail_card(detail, dynamic_id)
        if card.type == 1:
            # The reviewed URL identifies the wrapper dynamic, while the old
            # executor silently changed follow/comment/repost to ``origin``.
            # Until Action Plan v2 binds the origin id, author and rule hash,
            # any forwarded wrapper must remain fail-closed before mutation.
            raise BilibiliForwardedTargetRequiresReview(
                "bilibili_forwarded_origin_requires_review"
            )
        validate_card_for_actions(card, actions)
        if "followed" in validated_plan.required_actions:
            if bilibili_author_handle(card.uname) != validated_plan.follow_target_handle:
                raise RuntimeError("bilibili_real_follow_target_mismatch")

        current_intents: dict[str, StartedActionIntent] = {}

        async def before_action(action: str) -> None:
            snapshot = await enforce_task_real_run_gate(task, require_running=True)
            if snapshot.action_plan.plan_hash != validated_plan.plan_hash:
                raise RealRunGateBlocked("action_plan_changed_during_execution")
            await refresh_task_lease(task_id)
            await renew_account_operation_lease(
                db=database,
                task_id=task_id,
                account_id=account_id,
                lottery_id=lottery_id,
                worker_id=WORKER_ID,
            )
            # Re-read every authoritative binding after both lease renewals and
            # immediately before the intent transaction. No external request
            # has started at this point.
            await enforce_task_real_run_gate(task, require_running=True)
            dpms_action = API_TO_DPMS_PHASE.get(action)
            if not dpms_action:
                raise RuntimeError("bilibili_action_intent_action_invalid")
            intent = await prepare_and_start_action_intent(
                db=database,
                task_id=task_id,
                account_id=account_id,
                lottery_id=lottery_id,
                worker_id=WORKER_ID,
                action=action,
                payload=validated_plan.payload_for(dpms_action),
            )
            current_intents[action] = intent

        async def after_attempt(action: str, action_result) -> None:
            intent = current_intents.get(action)
            if intent is None:
                raise BilibiliActionSettlementFailed(
                    action,
                    action_result,
                    RuntimeError("bilibili_action_intent_missing"),
                )
            try:
                await settle_action_intent(
                    db=database,
                    intent=intent,
                    succeeded=action_result.ok,
                    outcome=action_result.outcome.value,
                    error_message=None if action_result.ok else action_result.message,
                )
            except BaseException as exc:
                raise BilibiliActionSettlementFailed(action, action_result, exc) from exc
            current_intents.pop(action, None)

        async def on_attempt_error(action: str, exc: BaseException) -> None:
            intent = current_intents.get(action)
            if intent is None:
                return
            try:
                await await_safety_settlement(
                    mark_action_intent_unknown(
                        db=database,
                        intent=intent,
                        reason=f"{type(exc).__name__}: remote outcome not proven",
                    )
                )
            except Exception as intent_exc:
                # The platform breaker/emergency barrier below is still the
                # last-resort global fence when the intent settlement itself is
                # unavailable. Do not replace the original network exception.
                structured_log(
                    "error",
                    "external_action_intent_unknown_write_failed",
                    task_id=task_id,
                    action=action,
                    exception=intent_exc,
                )

        executed_dynamic_id = card.dynamic_id

        async def after_action(action: str, action_result) -> None:
            try:
                await persist_bilibili_action_result(
                    task_id=task_id,
                    account_id=account_id,
                    lottery_id=lottery_id,
                    dynamic_id=executed_dynamic_id,
                    action=action,
                    action_result=action_result,
                )
            except asyncio.CancelledError as exc:
                # The remote response is already classified, so cancellation
                # here means local settlement is incomplete rather than that no
                # action happened. Convert it to the same durable quarantine
                # path as every other confirmed-result persistence failure.
                raise BilibiliActionSettlementFailed(action, action_result, exc) from exc
            except Exception as exc:
                raise BilibiliActionSettlementFailed(action, action_result, exc) from exc

        try:
            result = await BilibiliApiExecutor(
                client,
                client.config,
                before_action=before_action,
                after_attempt=after_attempt,
                on_attempt_error=on_attempt_error,
                after_action=after_action,
            ).participate(card, actions, validated_plan.action_payloads)
        except (BilibiliApiActionOutcomeUnknown, BilibiliActionSettlementFailed) as exc:
            settlement_failed = isinstance(exc, BilibiliActionSettlementFailed)
            reason = (
                f"bilibili_{exc.action}_settlement_failed"
                if settlement_failed
                else f"bilibili_{exc.action}_outcome_unknown"
            )
            quarantine_errors: list[str] = []
            breaker_opened = False
            breaker_failure: Exception | None = None
            emergency_failure: Exception | None = None
            emergency_barrier: str | None = None
            unknown_ledger_recorded = False
            active_intent = current_intents.get(exc.action)
            if active_intent is not None:
                try:
                    await await_safety_settlement(
                        mark_action_intent_unknown(
                            db=database,
                            intent=active_intent,
                            reason=reason,
                        )
                    )
                except Exception as quarantine_exc:
                    quarantine_errors.append(
                        f"intent:{type(quarantine_exc).__name__}"
                    )
            try:
                await await_safety_settlement(
                    open_unknown_outcome_breaker(
                        db=database,
                        platform="bilibili",
                        action=exc.action,
                    )
                )
                breaker_opened = True
            except Exception as quarantine_exc:
                breaker_failure = quarantine_exc
                quarantine_errors.append(f"breaker:{type(quarantine_exc).__name__}")
                structured_log(
                    "error",
                    "unknown_outcome_breaker_failed",
                    task_id=task_id,
                    action=exc.action,
                    exception=quarantine_exc,
                )
                try:
                    emergency_barrier = await await_safety_settlement(
                        emergency_stop_real_runs_and_revoke_lease(
                            task_id=task_id,
                            platform="bilibili",
                            action=exc.action,
                        )
                    )
                except Exception as emergency_exc:
                    emergency_failure = emergency_exc
                    quarantine_errors.append(f"emergency:{type(emergency_exc).__name__}")
            try:
                known_result = exc.action_result if settlement_failed else None
                await await_safety_settlement(
                    save_bilibili_action_ledger(
                        task_id=task_id,
                        account_id=account_id,
                        lottery_id=lottery_id,
                        dynamic_id=executed_dynamic_id,
                        action=exc.action,
                        phase=API_TO_DPMS_PHASE.get(exc.action),
                        code=known_result.code if known_result is not None else None,
                        outcome=known_result.outcome.value if known_result is not None else "unknown",
                        message=(
                            f"{known_result.message}; local settlement failed"
                            if known_result is not None
                            else exc.reason
                        ),
                        ok=known_result.ok if known_result is not None else False,
                    )
                )
                unknown_ledger_recorded = True
            except Exception as quarantine_exc:
                quarantine_errors.append(f"ledger:{type(quarantine_exc).__name__}")
            try:
                quarantine_event_id = await await_safety_settlement(
                    record_event(
                        aggregate="task",
                        aggregate_id=task_id,
                        event_type=(
                            "BilibiliApiActionSettlementFailed"
                            if settlement_failed
                            else "BilibiliApiActionOutcomeUnknown"
                        ),
                        payload={
                            "account_id": account_id,
                            "lottery_id": lottery_id,
                            "dynamic_id": executed_dynamic_id,
                            "action": exc.action,
                            "reason": exc.reason,
                            "remote_outcome": (
                                exc.action_result.outcome.value if settlement_failed else "unknown"
                            ),
                            "remote_code": exc.action_result.code if settlement_failed else None,
                            "platform_breaker_opened": breaker_opened,
                            "emergency_barrier": emergency_barrier,
                            "unknown_ledger_recorded": unknown_ledger_recorded,
                            "quarantine_errors": quarantine_errors,
                        },
                        correlation_id=task_id,
                    )
                )
                if not quarantine_event_id:
                    quarantine_errors.append("event:persistence_failed")
            except Exception as quarantine_exc:
                quarantine_errors.append(f"event:{type(quarantine_exc).__name__}")
                structured_log(
                    "error",
                    "unknown_outcome_event_failed",
                    task_id=task_id,
                    action=exc.action,
                    exception=quarantine_exc,
                )
            try:
                status_change = (
                    account_status_for_results({exc.action: exc.action_result})
                    if settlement_failed
                    else None
                )
                if status_change:
                    await await_safety_settlement(
                        set_account_status(account_id, status_change[0], status_change[1])
                    )
                else:
                    await await_safety_settlement(
                        set_account_status(account_id, "cooling", reason)
                    )
            except AccountStatusPersistenceFailed:
                # Do not downgrade an uncommitted canonical risk settlement to
                # the generic task-failure cleanup below. Keeping the claim and
                # account in their current state lets Recovery apply the same
                # fail-closed path used by direct risk-settlement failures.
                raise
            except Exception as quarantine_exc:
                quarantine_errors.append(f"account:{type(quarantine_exc).__name__}")
                structured_log(
                    "error",
                    "unknown_outcome_account_quarantine_failed",
                    task_id=task_id,
                    account_id=account_id,
                    action=exc.action,
                    exception=quarantine_exc,
                )
            if not breaker_opened:
                raise TaskSettlementUnconfirmed(
                    task_id,
                    emergency_failure or breaker_failure or exc,
                ) from exc
            raise

        status_change = account_status_for_results(result.actions)
        if status_change:
            status, reason = status_change
            await set_account_status(account_id, status, reason)

        try:
            completion_event_id = await record_event(
                aggregate="task",
                aggregate_id=task_id,
                event_type="BilibiliApiRealRunExecuted",
                payload={
                    "account_id": account_id,
                    "lottery_id": lottery_id,
                    "requested_dynamic_id": dynamic_id,
                    "executed_dynamic_id": result.dynamic_id,
                    "actions": list(result.actions.keys()),
                    "success": result.success,
                    "aborted": result.aborted,
                    "abort_reason": result.abort_reason,
                },
                correlation_id=task_id,
            )
            if not completion_event_id:
                raise RuntimeError("bilibili_completion_event_persistence_failed")
        except asyncio.CancelledError as exc:
            raise TaskSettlementUnconfirmed(task_id, exc) from exc
        except Exception as exc:
            raise TaskSettlementUnconfirmed(task_id, exc) from exc

        if not result.success:
            details = "; ".join(
                f"{name}={res.outcome.value}(code={res.code})" for name, res in result.actions.items()
            )
            raise RuntimeError(result.abort_reason or f"bilibili_api_real_run_failed: {details}")
        try:
            await save_phase(task_id, account_id, lottery_id, "completed")
        except asyncio.CancelledError as exc:
            raise TaskSettlementUnconfirmed(task_id, exc) from exc
        except TaskSettlementUnconfirmed:
            raise
        except Exception as exc:
            raise TaskSettlementUnconfirmed(task_id, exc) from exc


async def load_weibo_oauth_credential(
    account_id: int,
    *,
    expected_uid: str,
    expected_execution_revision: int,
):
    """Decrypt and bind the exact OAuth credential selected by the gate."""

    row = await database.fetch_one(
        """SELECT platform, encrypted_credential, execution_revision
             FROM accounts
            WHERE id = :account_id AND deleted_at IS NULL""",
        {"account_id": account_id},
    )
    if not row or str(row_get(row, "platform") or "").strip().lower() != "weibo":
        raise WeiboOAuthCredentialError("weibo_oauth_account_binding_invalid")
    try:
        revision = int(row_get(row, "execution_revision"))
    except (TypeError, ValueError) as exc:
        raise WeiboOAuthCredentialError(
            "weibo_oauth_execution_revision_mismatch"
        ) from exc
    if revision != expected_execution_revision:
        raise WeiboOAuthCredentialError("weibo_oauth_execution_revision_mismatch")
    encrypted = row_get(row, "encrypted_credential")
    if not encrypted:
        raise WeiboOAuthCredentialError("weibo_oauth_credential_required")
    try:
        decrypted = cookie_vault.decrypt(encrypted, aad=CREDENTIAL_AAD)
    except Exception as exc:
        # OAuth secrets never use the legacy plaintext cookie fallback.
        raise WeiboOAuthCredentialError(
            "weibo_oauth_credential_decryption_failed"
        ) from exc
    return parse_weibo_oauth_credential(decrypted, expected_uid=expected_uid)


def _weibo_handle_identity_key(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


async def preflight_weibo_friend_mentions(
    client,
    plan,
    *,
    pre_resolved: dict[str, str] | None = None,
    on_progress=None,
) -> dict[str, str]:
    """Resolve every constrained mention and enforce friend counts by UID.

    This runs before any durable intent or mutation. Source mentions and follow
    targets are excluded by both normalized handle identity and resolved UID so
    aliases cannot be counted as distinct friends or disguise a brand account.
    """

    constraints = dict(plan.friend_mention_requirements or {})
    cache: dict[str, str] = {
        _weibo_handle_identity_key(handle): uid
        for handle, uid in dict(pre_resolved or {}).items()
    }

    async def resolve(handle: str) -> str:
        key = _weibo_handle_identity_key(handle)
        if key not in cache:
            cache[key] = await client.resolve_user_uid(handle)
            if on_progress is not None:
                await on_progress()
        return cache[key]

    source = dict(plan.source_content_requirements or {})
    bound = dict(plan.content_requirements or {})
    excluded_handles = list(source.get("follow_targets") or []) + list(
        bound.get("follow_targets") or []
    )
    all_handles = list(excluded_handles)
    for action in ("commented", "reposted"):
        source_action = source.get(action, {})
        if isinstance(source_action, dict):
            all_handles.extend(source_action.get("mentions") or [])
        all_handles.extend(plan.payload_for(action).get("mentions") or [])
    unique_handle_keys = {
        _weibo_handle_identity_key(handle) for handle in all_handles
    }
    if len(unique_handle_keys) > WEIBO_MAX_UNIQUE_HANDLES:
        raise RuntimeError("weibo_preflight_unique_handle_limit_exceeded")
    excluded_keys = {
        _weibo_handle_identity_key(handle) for handle in excluded_handles
    }
    excluded_uids = {await resolve(handle) for handle in excluded_handles}

    # Mention validity is an independent precondition, not merely an input to
    # the optional friend-count rule.  Resolve every user identity referenced
    # by an executable text action before any durable intent/POST is created.
    # Otherwise a misspelled/non-existent brand mention could be accepted as
    # plain comment text and later recorded as a successful requirement.
    for action in ("commented", "reposted"):
        source_action = source.get(action, {})
        source_mentions = list(
            source_action.get("mentions") or []
            if isinstance(source_action, dict)
            else []
        )
        source_keys = {
            _weibo_handle_identity_key(handle) for handle in source_mentions
        }
        source_uids = {await resolve(handle) for handle in source_mentions}
        payload_mentions = list(plan.payload_for(action).get("mentions") or [])
        resolved_payload = [
            (handle, await resolve(handle)) for handle in payload_mentions
        ]
        constraint = constraints.get(action)
        if constraint is None:
            continue
        friend_uids = {
            uid
            for handle, uid in resolved_payload
            if _weibo_handle_identity_key(handle)
            not in source_keys | excluded_keys
            and uid not in source_uids | excluded_uids
        }
        expected = int(constraint["count"])
        satisfied = (
            len(friend_uids) == expected
            if constraint["mode"] == "exact"
            else len(friend_uids) >= expected
        )
        if not satisfied:
            raise RuntimeError(
                f"weibo_friend_identity_count_mismatch:{action}"
            )
    return dict(cache)


async def execute_weibo_oauth_real_task(task: dict) -> None:
    """Execute one immutable official Weibo OAuth plan with durable fencing."""

    task_id = str(task.get("task_id") or "").strip()
    if "weibo_rip" in task:
        raise RuntimeError("weibo_rip_plaintext_forbidden")
    account_id = int(task.get("account_id"))
    lottery_id = int(task.get("lottery_id"))
    try:
        validated_plan = validate_action_plan_v2(
            task.get("action_plan"), reject_media=True
        )
    except ActionPlanV2Error as exc:
        raise RuntimeError(exc.code) from exc
    if validated_plan.execution_path_id != WEIBO_OAUTH_EXECUTION_PATH:
        raise RuntimeError("weibo_execution_path_not_supported")

    current_phase = await get_latest_phase(task_id) or "init"
    if current_phase == "completed":
        return
    if current_phase != "init":
        # Weibo actions are journaled only in external_action_intents; the
        # task_phases ENUM is not an action ledger (and may not contain favorite).
        raise RuntimeError("weibo_task_phase_requires_reconciliation")

    gate = await enforce_task_real_run_gate(task, require_running=True)
    if (
        gate.platform != "weibo"
        or gate.execution_evidence_id
        != str(task.get("execution_evidence_id") or "").strip()
        or gate.oauth_capabilities is None
        or not gate.weibo_uid
    ):
        raise RealRunGateBlocked("weibo_oauth_execution_binding_invalid")
    credential = await load_weibo_oauth_credential(
        account_id,
        expected_uid=gate.weibo_uid,
        expected_execution_revision=gate.execution_revision,
    )
    rip = decrypt_weibo_rip(
        task.get("weibo_rip_encrypted"),
        required=weibo_rip_required(validated_plan.required_actions),
    )
    canonical_identifier = status_identifier_from_canonical_uri(
        task.get("canonical_url")
    )

    client = WeiboApiClient(
        credential.access_token,
        capability_attestation=gate.oauth_capabilities,
        calibration_id=gate.execution_evidence_id,
        account_id=account_id,
        execution_revision=gate.execution_revision,
        runtime_capability_requirements=(
            validated_plan.runtime_capability_requirements
        ),
    )
    current_intents: dict[str, StartedActionIntent] = {}
    expected_mutations = {}

    async def renew_preflight_leases() -> None:
        await refresh_task_lease(task_id)
        await renew_account_operation_lease(
            db=database,
            task_id=task_id,
            account_id=account_id,
            lottery_id=lottery_id,
            worker_id=WORKER_ID,
        )

    async def quarantine_unknown(action: str, cause: BaseException) -> None:
        intent = current_intents.get(action)
        if intent is not None:
            try:
                await await_safety_settlement(
                    mark_action_intent_unknown(
                        db=database,
                        intent=intent,
                        reason=f"weibo_{action}_outcome_unknown",
                    )
                )
            except Exception as intent_exc:
                structured_log(
                    "error",
                    "external_action_intent_unknown_write_failed",
                    task_id=task_id,
                    action=action,
                    exception=intent_exc,
                )
        await await_safety_settlement(
            quarantine_external_action_outcome(
                task_id=task_id,
                account_id=account_id,
                platform="weibo",
                action=action,
                cause=cause,
            )
        )

    async def run_readonly_weibo_preflight():
        status_id = await client.resolve_status_id(canonical_identifier)
        await renew_preflight_leases()
        await client.preflight_status(status_id)
        await renew_preflight_leases()
        follow_target_uid = None
        if "followed" in validated_plan.required_actions:
            follow_target_uid = await client.resolve_user_uid(
                validated_plan.follow_target_handle
            )
            await renew_preflight_leases()
        await preflight_weibo_friend_mentions(
            client,
            validated_plan,
            pre_resolved=(
                {validated_plan.follow_target_handle: follow_target_uid}
                if follow_target_uid
                else None
            ),
            on_progress=renew_preflight_leases,
        )
        return status_id, follow_target_uid

    try:
        await renew_preflight_leases()
        try:
            status_id, follow_target_uid = await asyncio.wait_for(
                run_readonly_weibo_preflight(),
                timeout=WEIBO_PREFLIGHT_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as exc:
            raise RuntimeError("weibo_preflight_deadline_exceeded") from exc

        # Lease renewal is liveness only. Re-read every authorization binding
        # after the potentially long read-only preflight and before the first
        # intent can be created.
        post_preflight_gate = await enforce_task_real_run_gate(
            task, require_running=True
        )
        credential.require_fresh(
            min_remaining_seconds=(
                len(validated_plan.required_actions)
                * WEIBO_ACTION_HTTP_BUDGET_SECONDS
                + WEIBO_ACTION_SETTLEMENT_BUFFER_SECONDS
            )
        )
        if (
            post_preflight_gate.action_plan.plan_hash != validated_plan.plan_hash
            or post_preflight_gate.execution_evidence_id
            != gate.execution_evidence_id
            or post_preflight_gate.execution_revision != gate.execution_revision
            or post_preflight_gate.weibo_uid != gate.weibo_uid
            or post_preflight_gate.oauth_capabilities != gate.oauth_capabilities
        ):
            raise RealRunGateBlocked(
                "weibo_oauth_binding_changed_during_preflight"
            )

        async def before_action(action: str) -> None:
            action_index = validated_plan.required_actions.index(action)
            remaining_actions = len(validated_plan.required_actions) - action_index
            credential.require_fresh(
                min_remaining_seconds=(
                    remaining_actions * WEIBO_ACTION_HTTP_BUDGET_SECONDS
                    + WEIBO_ACTION_SETTLEMENT_BUFFER_SECONDS
                )
            )
            current_gate = await enforce_task_real_run_gate(
                task, require_running=True
            )
            if (
                current_gate.action_plan.plan_hash != validated_plan.plan_hash
                or current_gate.execution_evidence_id != gate.execution_evidence_id
                or current_gate.execution_revision != gate.execution_revision
                or current_gate.weibo_uid != gate.weibo_uid
                or current_gate.oauth_capabilities != gate.oauth_capabilities
            ):
                raise RealRunGateBlocked(
                    "weibo_oauth_binding_changed_during_execution"
                )
            await refresh_task_lease(task_id)
            await renew_account_operation_lease(
                db=database,
                task_id=task_id,
                account_id=account_id,
                lottery_id=lottery_id,
                worker_id=WORKER_ID,
            )
            # Renewals are not authority. Re-read the entire gate immediately
            # before the transaction that marks the external intent started.
            renewed_gate = await enforce_task_real_run_gate(
                task, require_running=True
            )
            if (
                renewed_gate.action_plan.plan_hash != validated_plan.plan_hash
                or renewed_gate.execution_evidence_id != gate.execution_evidence_id
                or renewed_gate.execution_revision != gate.execution_revision
                or renewed_gate.weibo_uid != gate.weibo_uid
                or renewed_gate.oauth_capabilities != gate.oauth_capabilities
            ):
                raise RealRunGateBlocked(
                    "weibo_oauth_binding_changed_during_execution"
                )
            intent_payload = {
                "platform": "weibo",
                "execution_path_id": WEIBO_OAUTH_EXECUTION_PATH,
                "calibration_id": gate.execution_evidence_id,
                "execution_revision": gate.execution_revision,
                "status_id": status_id,
                "action_payload": validated_plan.payload_for(action),
            }
            mutation_target = (
                follow_target_uid if action == "followed" else status_id
            )
            expected_mutation = build_weibo_mutation_request(
                action,
                mutation_target,
                payload=validated_plan.payload_for(action),
                rip=(
                    rip
                    if action in {"followed", "commented", "reposted"}
                    else ""
                ),
            )
            expected_mutations[action] = expected_mutation
            intent_payload["mutation_spec"] = expected_mutation.audit_spec
            if action == "followed":
                intent_payload["follow_target_handle"] = (
                    validated_plan.follow_target_handle
                )
                intent_payload["follow_target_uid"] = follow_target_uid
            current_intents[action] = await prepare_and_start_action_intent(
                db=database,
                task_id=task_id,
                account_id=account_id,
                lottery_id=lottery_id,
                worker_id=WORKER_ID,
                action=action,
                payload=intent_payload,
            )

        async def operation_key_for(action: str) -> str:
            intent = current_intents.get(action)
            if intent is None:
                raise RuntimeError("weibo_action_intent_missing")
            return f"{intent.intent_id}:{intent.attempt_no}"

        async def after_receipt(action: str, receipt) -> None:
            intent = current_intents.get(action)
            if intent is None:
                raise RuntimeError("weibo_action_intent_missing")
            expected = expected_mutations.get(action)
            if (
                expected is None
                or receipt.action != action
                or receipt.target_id != expected.target_id
                or receipt.operation_key
                != f"{intent.intent_id}:{intent.attempt_no}"
                or receipt.request_payload_hash != expected.audit_spec_hash
            ):
                raise RuntimeError("weibo_action_receipt_binding_invalid")
            await settle_action_intent(
                db=database,
                intent=intent,
                succeeded=True,
                outcome="ok",
                remote_ref=receipt.remote_id,
            )
            current_intents.pop(action, None)
            expected_mutations.pop(action, None)

        try:
            result = await WeiboOAuthExecutor(
                client,
                operation_key_for=operation_key_for,
                before_action=before_action,
                after_receipt=after_receipt,
            ).execute(
                validated_plan,
                status_id=status_id,
                follow_target_uid=follow_target_uid,
                rip=rip,
            )
        except WeiboApiRejected as exc:
            if not exc.confirmed_no_effect:
                await quarantine_unknown(exc.action, exc)
                raise ExternalActionOutcomeUnknown(
                    "weibo", exc.action, exc
                ) from exc
            intent = current_intents.get(exc.action)
            if intent is None:
                raise
            try:
                await await_safety_settlement(
                    settle_action_intent(
                        db=database,
                        intent=intent,
                        succeeded=False,
                        outcome="rejected",
                        error_message=f"weibo_api_rejected:{exc.error_code}",
                    )
                )
                current_intents.pop(exc.action, None)
            except BaseException as settlement_exc:
                await quarantine_unknown(exc.action, settlement_exc)
                raise ExternalActionOutcomeUnknown(
                    "weibo", exc.action, settlement_exc
                ) from settlement_exc
            raise
        except (WeiboApiActionOutcomeUnknown, WeiboExecutionOutcomeUnknown) as exc:
            await quarantine_unknown(exc.action, exc)
            raise ExternalActionOutcomeUnknown("weibo", exc.action, exc) from exc
        except asyncio.CancelledError as exc:
            if current_intents:
                action = next(reversed(current_intents))
                await quarantine_unknown(action, exc)
            raise
        except BaseException as exc:
            if current_intents:
                action = next(reversed(current_intents))
                await quarantine_unknown(action, exc)
                raise ExternalActionOutcomeUnknown("weibo", action, exc) from exc
            raise

        try:
            completion_event_id = await record_event(
                aggregate="task",
                aggregate_id=task_id,
                event_type="WeiboOAuthRealRunExecuted",
                payload={
                    "account_id": account_id,
                    "lottery_id": lottery_id,
                    "calibration_id": gate.execution_evidence_id,
                    "actions": list(result.receipts),
                    "success": result.success,
                },
                correlation_id=task_id,
            )
            if not completion_event_id:
                raise RuntimeError("weibo_completion_event_persistence_failed")
        except BaseException as exc:
            await quarantine_unknown("task_completion", exc)
            raise ExternalActionOutcomeUnknown(
                "weibo", "task_completion", exc
            ) from exc
        try:
            await save_phase(task_id, account_id, lottery_id, "completed")
        except BaseException as exc:
            await quarantine_unknown("task_completion", exc)
            raise ExternalActionOutcomeUnknown(
                "weibo", "task_completion", exc
            ) from exc
    finally:
        try:
            await client.aclose()
        except Exception as exc:
            # Closing a socket cannot undo a durably settled remote result and
            # must not manufacture an unknown external outcome.
            structured_log(
                "warning",
                "weibo_http_client_close_failed",
                task_id=task_id,
                exception=exc,
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
        fd = os.open(path.name, flags, 0o600, dir_fd=directory_fd)
        created = os.fstat(fd)
        if not stat.S_ISREG(created.st_mode):
            raise RuntimeError("evidence_screenshot_not_regular")
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
    try:
        return cookie_vault.decrypt(credential_blob, aad=CREDENTIAL_AAD)
    except Exception:
        return credential_blob.decode("utf-8") if isinstance(credential_blob, bytes) else str(credential_blob)


async def prepare_account_login(ctx, account_id: int, platform: str):
    credential = await load_account_credential(account_id)
    await inject_account_cookies(ctx, platform, credential)


def _require_int(task: dict, field: str) -> int:
    value = task.get(field)
    try:
        return int(value)
    except Exception as exc:
        raise InvalidTaskMessage(f"{field} must be an integer") from exc


def validate_task_message(task: dict) -> dict:
    task_id = str(task.get("task_id") or "").strip()
    if not task_id:
        raise InvalidTaskMessage("task_id is required")
    account_id = _require_int(task, "account_id")
    lottery_id = _require_int(task, "lottery_id")
    platform = str(task.get("platform") or "bilibili").strip() or "bilibili"
    mode = normalize_task_mode(task)
    task["task_id"] = task_id
    task["account_id"] = str(account_id)
    task["lottery_id"] = str(lottery_id)
    task["platform"] = platform
    task["mode"] = mode
    normalized_platform = platform.lower()
    if mode == "dry_run" and normalized_platform in {
        "douyin",
        "xiaohongshu",
    }:
        raise InvalidTaskMessage(f"{normalized_platform}_manual_shadow_only")
    if mode == "dry_run" and normalized_platform == "weibo":
        try:
            preliminary_plan = validate_action_plan_v2(
                task.get("action_plan"),
                require_executable=False,
                reject_media=True,
            )
            if preliminary_plan.execution_path_id == WEIBO_MANUAL_EXECUTION_PATH:
                raise InvalidTaskMessage("weibo_manual_shadow_only")
            oauth_dry_plan = validate_action_plan_v2(
                task.get("action_plan"),
                reject_media=True,
            )
            if oauth_dry_plan.execution_path_id != WEIBO_OAUTH_EXECUTION_PATH:
                raise InvalidTaskMessage("weibo_execution_path_not_supported")
        except ActionPlanV2Error as exc:
            raise InvalidTaskMessage(exc.code) from exc
    if "weibo_rip" in task:
        # Never accept or reserialize the retired plaintext queue field.
        task.pop("weibo_rip", None)
        raise InvalidTaskMessage("weibo_rip_plaintext_forbidden")
    encrypted_rip = task.get("weibo_rip_encrypted")
    if normalized_platform == "weibo" and mode == "real_run":
        try:
            plan = validate_action_plan_v2(task.get("action_plan"), reject_media=True)
            if plan.execution_path_id != WEIBO_OAUTH_EXECUTION_PATH:
                raise InvalidTaskMessage("weibo_execution_path_not_supported")
            rip_required = weibo_rip_required(plan.required_actions)
            if rip_required and not isinstance(encrypted_rip, str):
                raise InvalidTaskMessage("weibo_rip_encrypted_required")
            if rip_required and not encrypted_rip:
                raise InvalidTaskMessage("weibo_rip_encrypted_required")
            if not rip_required and encrypted_rip is not None and encrypted_rip != "":
                raise InvalidTaskMessage("weibo_rip_encrypted_not_applicable")
            task["weibo_rip_encrypted"] = encrypted_rip or ""
        except ActionPlanV2Error as exc:
            raise InvalidTaskMessage(exc.code) from exc
    elif encrypted_rip is not None and encrypted_rip != "":
        raise InvalidTaskMessage("weibo_rip_encrypted_not_applicable")
    else:
        task["weibo_rip_encrypted"] = ""
    return task


async def dead_letter_message(msg_id: str, task: dict, reason: str):
    task_id = str(task.get("task_id") or "") or None
    sanitized_task = dict(task)
    sanitized_task.pop("weibo_rip", None)
    payload = json.dumps(sanitized_task, ensure_ascii=False)
    try:
        await database.execute(
            """INSERT INTO failed_task_messages (stream_key, message_id, task_id, reason, payload)
               VALUES (:stream_key, :message_id, :task_id, :reason, :payload)""",
            {"stream_key": STREAM_KEY, "message_id": str(msg_id), "task_id": task_id, "reason": reason[:255], "payload": payload},
        )
    except Exception as exc:
        structured_log("error", "dead_letter_db_failed", message_id=msg_id, task_id=task_id, error=str(exc))
    try:
        await redis.xadd(
            "failed_task_messages",
            {"stream_key": STREAM_KEY, "message_id": str(msg_id), "task_id": task_id or "", "reason": reason[:255], "payload": payload},
        )
    except Exception as exc:
        structured_log("error", "dead_letter_stream_failed", message_id=msg_id, task_id=task_id, error=str(exc))


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


async def handle_invalid_task_message(msg_id: str, task: dict, reason: str) -> bool:
    """Dead-letter an invalid entry and ack only when no active task needs it.

    Returns ``True`` when the message was acknowledged.  Database lookup
    failures deliberately propagate so the consumer leaves the entry pending;
    losing observability is preferable to acknowledging the only recovery
    trigger while authoritative state is unavailable.
    """

    await dead_letter_message(msg_id, task, reason)
    if await invalid_task_requires_authoritative_recovery(task):
        structured_log(
            "warning",
            "invalid_task_message_retained_for_authoritative_recovery",
            message_id=msg_id,
            task_id=_canonical_task_uuid(task),
            reason=reason,
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
        await redis.xdel(STREAM_KEY, msg_id)
    await redis.xack(STREAM_KEY, GROUP_NAME, msg_id)
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
        if task_mode == "dry_run" and normalized_platform in {
            "douyin",
            "xiaohongshu",
        }:
            raise RuntimeError(f"{normalized_platform}_manual_shadow_only")
        if task_mode == "dry_run" and normalized_platform == "weibo":
            try:
                dry_plan = validate_action_plan_v2(
                    task.get("action_plan"),
                    require_executable=False,
                    reject_media=True,
                )
            except ActionPlanV2Error as exc:
                raise RuntimeError(exc.code) from exc
            if dry_plan.execution_path_id == WEIBO_MANUAL_EXECUTION_PATH:
                raise RuntimeError("weibo_manual_shadow_only")
            if (
                dry_plan.execution_path_id != WEIBO_OAUTH_EXECUTION_PATH
                or dry_plan.plan.get("executable") is not True
            ):
                raise RuntimeError("weibo_execution_path_not_supported")
        if task_mode == "real_run":
            await enforce_task_real_run_gate(task)
            if (
                not uses_bilibili_api_real_task(task)
                and not uses_weibo_oauth_real_task(task)
                and not getattr(adapter, "REAL_ACTIONS", False)
            ):
                raise RuntimeError(f"Real actions for {adapter.PLATFORM} are not implemented")
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
            completion_screenshot_path = await execute_shadow_run(task, adapter, pool)
        elif uses_bilibili_api_real_task(task):
            await execute_bilibili_api_real_task(task)
        elif uses_weibo_oauth_real_task(task):
            await execute_weibo_oauth_real_task(task)
        else:
            await execute_real_task(task, adapter, pool)
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
                quarantine_account=isinstance(
                    e,
                    (
                        ExternalActionOutcomeUnknown,
                        BilibiliApiActionOutcomeUnknown,
                        BilibiliActionSettlementFailed,
                    ),
                ),
                account_failure_status=(
                    e.account_status if isinstance(e, WeiboApiRejected) else None
                ),
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


async def task_loop(pool: BrowserPool, shutdown_event: asyncio.Event):
    try:
        await redis.xgroup_create(STREAM_KEY, GROUP_NAME, id="0", mkstream=True)
    except Exception:
        pass
    while not shutdown_event.is_set():
        try:
            msgs = await asyncio.wait_for(redis.xreadgroup(GROUP_NAME, CONSUMER_NAME, {STREAM_KEY: ">"}, count=1, block=5000), timeout=1)
            if not msgs:
                continue
            for msg_id, data in msgs[0][1]:
                task = {k: v for k, v in data.items()}
                structured_log("info", "task_received", task_id=task.get("task_id"), message_id=msg_id)
                try:
                    task = validate_task_message(task)
                except InvalidTaskMessage as exc:
                    acknowledged = await handle_invalid_task_message(
                        msg_id, task, str(exc)
                    )
                    structured_log(
                        "error",
                        "task_message_dead_lettered",
                        message_id=msg_id,
                        reason=str(exc),
                        acknowledged=acknowledged,
                    )
                    continue
                selector_config = parse_json_field(task.get("selector_config")) or {}
                success = await execute_task_with_phases(task, get_adapter(task.get("platform", "bilibili"), selector_config), pool, str(msg_id))
                await redis.xack(STREAM_KEY, GROUP_NAME, msg_id)
                if not success:
                    structured_log("error", "task_failed", task_id=task.get("task_id"))
        except asyncio.TimeoutError:
            continue
        except asyncio.CancelledError:
            break
        except Exception as e:
            structured_log("error", "task_loop_error", exception=e)
            await asyncio.sleep(5)


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
    actions = plan.get("required_actions")
    normalized_platform = str(task.get("platform") or "").strip().lower()
    if normalized_platform == "xiaohongshu":
        phase_order = XIAOHONGSHU_PHASE_ORDER
    elif normalized_platform == "douyin":
        phase_order = DOUYIN_PHASE_ORDER
    elif normalized_platform == "weibo":
        phase_order = WEIBO_PHASE_ORDER
    else:
        phase_order = PHASE_ORDER
    phases = [
        phase
        for phase in phase_order
        if isinstance(actions, list) and phase in actions
    ]
    if require_plan:
        if plan.get("review_required"):
            raise RuntimeError("Lottery rule requires review before real-run")
        if not phases:
            raise RuntimeError("Lottery action plan is missing required actions")
    return phases or list(phase_order)
