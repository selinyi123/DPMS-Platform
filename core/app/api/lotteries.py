import asyncio
import json
import ipaddress
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.action_plan import (
    DOUYIN_MANUAL_EXECUTION_PATH,
    WEIBO_MANUAL_EXECUTION_PATH,
    XIAOHONGSHU_MANUAL_EXECUTION_PATH,
    ActionPlanV2Error,
    action_order_for_platform,
    compute_action_plan_hash,
    compute_config_hash,
    compute_target_hash,
    default_execution_path_for_platform,
    semantic_requirement_status,
    validate_action_payload,
    validate_friend_mention_requirements,
)

from app.adapter_config import (
    PHASES as ADAPTER_PHASES,
    MANUAL_ASSISTED_ONLY_PLATFORMS,
    load_runtime_selector_config,
    platform_has_runtime_real_adapter,
    platform_real_adapter_kind,
    recommended_config_from_probe,
    selector_config_complete,
    selector_phases_for_platform,
)
from app.config import settings
from app.db import database, redis
from app.event_store.service import record_event
from app.knowledge.service import account_reputation
from app.models.schemas import (
    AdapterSelectorConfigUpdate,
    AdapterProbeRequest,
    DispatchTaskRequest,
    LOTTERY_RAW_URL_MAX_LENGTH,
    LotteryActionPlanUpdate,
    LotteryCreate,
    LotteryResultUpdate,
    LotteryTargetImport,
    TrackedSourceCreate,
)
from app.platform_modules import (
    LotteryTargetValidation,
    PlatformCapabilityError,
    PlatformModuleUnavailableError,
    PlatformPolicyConflict,
    get_platform_module,
)
from app.platform_modules.base import build_manual_shadow_plan_binding
from app.platform_modules.catalog import PLATFORM_MODULE_SPECS
from app.platform_modules.shadow import missing_manual_shadow_selector_phases
from app.platforms import get_platform, get_platforms
from app.utils.secure_files import (
    SecureFileError,
    open_bounded_regular_file_beneath_root,
)
from app.strategy.engine import (
    account_tier,
    choose_strategy_mode,
    empty_platform_knowledge,
    estimate_trust_score_breakdown,
    estimate_win_probability_breakdown,
    first_or_none,
    priority_tier,
    strategy_knowledge_confidence,
    strategy_knowledge_confidence_breakdown,
    strategy_score_breakdown,
)
from app.services.discovery import run_discovery
from app.services.discovery_requests import dispatch_manual_discovery_scan
from app.services.lottery_rules import parse_lottery_rule
from app.services.lottery_rule_hydration import (
    LotteryRuleHydrationError,
    hydrate_lottery_rule,
    target_identity_from_lottery,
)
from app.services.account_leases import (
    AccountOperationLeaseConflict,
    acquire_account_operation_lease,
    bind_lease_to_task,
)
from app.services.outbox import build_lottery_task_message, enqueue_outbox, try_flush_dedup
from app.services.execution_intents import (
    ExecutionIntentError,
    ExecutionIntentLoadFailure,
    FrozenLotteryExecutionIntent,
    build_repair_execution_subset,
    coerce_frozen_execution_intent,
    load_lottery_execution_intent,
    load_lottery_execution_intents,
    persist_full_execution_intent,
    persist_repair_execution_binding,
    validate_lottery_execution_intent_binding,
)
from app.task_streams import (
    repair_task_stream_binding_for_platform,
    repair_task_stream_for_platform,
    task_stream_for_platform,
)
from app.adapter_probe_streams import adapter_probe_stream_for_platform
from app.services.real_run_gate import evaluate_real_run_decision
from app.services.real_run_readiness import (
    ACCOUNT_SCOPED_READINESS_MAX_CANDIDATES_PER_PLATFORM,
    emit_real_run_gate_notification,
    evaluate_account_scoped_real_run_readiness_batch,
    load_account_scoped_readiness_candidate_prefilter,
    parse_json_field,
    phase_configured,
    platform_selectors_complete,
    recent_account_risk,
    load_real_run_evidence_batch,
    real_run_account_risk_summaries,
    real_run_gate_status,
    validate_real_run_evidence,
)
from app.services.task_transport_health import (
    REPAIR_CONSUMER_MAX_IDLE_MILLISECONDS,
    repair_consumer_idle_by_name,
    worker_rows_support_repair_dispatch,
)
from app.services.rule_provenance import ensure_rule_snapshot
from app.security import (
    audit_event,
    circuit_breaker_allows,
    is_real_run_enabled,
    require_confirmation,
    require_min_role,
)
from app.utils.canonicalizer import CanonicalizationError, canonicalize_platform_url
from app.utils.lottery_targets import (
    validate_lottery_identity,
    validate_lottery_target,
)
from app.utils.crypto import encrypt_weibo_rip
from app.utils.log import structured_log
from shared.execution_contracts import (
    FULL_EXECUTION_INTENT_KIND,
    REPAIR_EXECUTION_INTENT_KIND,
    lease_operation_kind_for_execution_intent,
)


router = APIRouter()
EVIDENCE_ROOT = Path(os.getenv("EVIDENCE_ROOT", "/profiles"))
CONFIRMED_NO_EFFECT_OUTCOMES = frozenset(
    {"retry", "limit", "skip", "captcha", "risk", "auth", "rejected"}
)


@dataclass(frozen=True)
class ExternalActionAuthorityBlocker:
    intent_id: str
    action: str
    status: str
    effect_certainty: str
    outcome: str
    reason: str

    def public_view(self) -> dict:
        return {
            "action": self.action,
            "status": self.status,
            "effect_certainty": self.effect_certainty,
            "outcome": self.outcome or None,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class RealRunCompletionAuthority:
    completed_actions: tuple[str, ...]
    blockers: tuple[ExternalActionAuthorityBlocker, ...] = ()
EVIDENCE_IMAGE_MAX_BYTES = 32 * 1024 * 1024


def _platform_evidence_roots(
    platform: object,
    *categories: str,
) -> tuple[Path, ...]:
    platform_id = str(platform or "").strip().casefold()
    if platform_id not in PLATFORM_MODULE_SPECS:
        return ()
    platform_roots = tuple(
        EVIDENCE_ROOT / platform_id / category
        for category in categories
    )
    # The pre-split evidence volume is mounted read-only in Core during the
    # compatibility window. No Worker can write these roots after cutover.
    legacy_roots = tuple(EVIDENCE_ROOT / category for category in categories)
    return platform_roots + legacy_roots


def _evidence_png_response(
    path_value,
    *,
    allowed_roots: tuple[Path, ...],
) -> StreamingResponse:
    candidate = Path(str(path_value or ""))
    for allowed_root in allowed_roots:
        try:
            snapshot = open_bounded_regular_file_beneath_root(
                allowed_root,
                candidate,
                max_bytes=EVIDENCE_IMAGE_MAX_BYTES,
            )
        except (OSError, SecureFileError, TypeError, ValueError):
            continue
        return StreamingResponse(
            snapshot.iter_chunks(),
            media_type="image/png",
            headers={
                "Cache-Control": "no-store",
                "Content-Length": str(snapshot.size),
                "X-Content-Type-Options": "nosniff",
            },
        )
    raise HTTPException(404, detail="Evidence image not found")


def _public_ip(value: object) -> str | None:
    token = str(value or "")
    if (
        not token
        or token != token.strip()
        or len(token) > 64
        or "," in token
    ):
        return None
    try:
        parsed = ipaddress.ip_address(token)
    except ValueError:
        return None
    if not parsed.is_global:
        return None
    return parsed.compressed


def _trusted_weibo_proxy(peer: object) -> bool:
    raw_cidrs = settings.weibo_trusted_proxy_cidrs.strip()
    if not raw_cidrs:
        return False
    try:
        peer_ip = ipaddress.ip_address(str(peer or "").strip())
        networks = tuple(
            ipaddress.ip_network(item.strip(), strict=False)
            for item in raw_cidrs.split(",")
            if item.strip()
        )
    except ValueError:
        return False
    return bool(networks) and any(peer_ip in network for network in networks)


def trusted_weibo_rip(request: Request) -> str:
    """Resolve Weibo's public RIP without trusting client-forgeable headers.

    A direct public socket peer is authoritative. ``X-Real-IP`` is accepted
    only from an explicitly configured proxy CIDR. Local Compose deployments
    may set ``WEIBO_PUBLIC_RIP`` to the public egress address used by Worker.
    ``X-Forwarded-For`` is never used.
    """

    peer = request.client.host if request.client is not None else ""
    if _trusted_weibo_proxy(peer):
        proxy_public = _public_ip(request.headers.get("x-real-ip"))
        if proxy_public is not None:
            return proxy_public
    direct_public = _public_ip(peer)
    if direct_public is not None:
        return direct_public
    configured_public = _public_ip(settings.weibo_public_rip)
    if configured_public is not None:
        return configured_public
    raise HTTPException(
        409,
        detail={"code": "weibo_public_rip_required"},
    )


def validated_probe_navigation_url(raw_url: str) -> str:
    target_url = str(raw_url or "").strip()
    parsed = urlparse(target_url)
    if parsed.scheme.lower() != "https" or not parsed.netloc:
        raise HTTPException(400, detail="Adapter probe target must use HTTPS")
    return target_url


def protected_source_rule_text(existing_rule_text, proposed_rule_text) -> str:
    """Keep the captured source rule immutable once it has been stored.

    The action-plan editor may classify the source rule, but it must not turn
    into a second ingestion path that replaces a complex source rule with a
    simpler executable summary.  An empty legacy record may be populated once;
    subsequent source corrections need a dedicated, separately audited flow.
    """

    existing_raw = str(existing_rule_text or "")
    existing = existing_raw.strip()
    if proposed_rule_text is None:
        candidate = existing
    else:
        candidate = str(proposed_rule_text).strip()
    if not candidate:
        raise HTTPException(400, detail="Source lottery rule text is required")
    if existing and candidate != existing:
        raise HTTPException(
            409,
            detail="Source lottery rule text cannot be replaced through action-plan review",
        )
    return existing_raw if existing else candidate


STRATEGY_TARGET_METRICS_SQL = """(
                    SELECT COUNT(*)
                    FROM accounts a
                    WHERE a.platform = l.platform
                      AND a.status = 'ready'
                      AND a.deleted_at IS NULL
                      AND OCTET_LENGTH(a.encrypted_credential) > 0
                      AND (
                        SELECT c.status FROM account_calibrations c
                        WHERE c.account_id = a.id
                          AND c.platform = a.platform
                        ORDER BY c.id DESC
                        LIMIT 1
                      ) = 'succeeded'
                      AND NOT EXISTS (
                        SELECT 1
                        FROM account_operation_leases lease
                        WHERE lease.account_id = a.id
                          AND lease.released_at IS NULL
                          AND lease.expires_at > NOW()
                      )
                  ) AS safe_accounts,
                  (
                    SELECT COUNT(*)
                    FROM risk_events r
                    JOIN accounts a ON a.id = r.account_id
                    WHERE a.platform = l.platform
                      AND r.created_at >= DATE_SUB(NOW(), INTERVAL 24 HOUR)
                  ) AS recent_platform_risk,
                  (
                    SELECT COUNT(*)
                    FROM task_runs tr
                    WHERE tr.lottery_id = l.id
                      AND tr.status IN ('queued','running')
                  ) AS active_runs,
                  (
                    SELECT COUNT(*)
                    FROM task_runs tr
                    WHERE tr.lottery_id = l.id
                      AND COALESCE(tr.task_mode, IF(tr.dry_run = 1, 'dry_run', 'real_run')) = 'dry_run'
                      AND tr.status = 'succeeded'
                  ) AS dry_success,
                  (
                    SELECT COUNT(*)
                    FROM task_runs tr
                    WHERE tr.lottery_id = l.id
                      AND tr.task_mode = 'shadow_run'
                      AND tr.status = 'succeeded'
                  ) AS shadow_success,
                  (
                    SELECT COUNT(*)
                    FROM task_runs tr
                    WHERE tr.lottery_id = l.id
                      AND tr.status = 'failed'
                  ) AS failed_runs"""

STRATEGY_QUERY_TIMEOUT_SECONDS = 2.0
STRATEGY_EVALUATION_TIMEOUT_SECONDS = 15.0
STRATEGY_HISTORY_WINDOW_DAYS_MAX = 30
# Return one overflow sentinel beyond the evaluator's exact candidate budget.
# The evaluator consumes at most 64 candidates and uses the 65th to surface its
# existing candidate-budget blocker rather than pretending the population was
# exhausted.
STRATEGY_ACCOUNT_ROWS_PER_PLATFORM = (
    ACCOUNT_SCOPED_READINESS_MAX_CANDIDATES_PER_PLATFORM + 1
)
STRATEGY_TASK_HISTORY_ROWS_PER_PLATFORM = 4096
STRATEGY_RISK_HISTORY_ROWS_PER_PLATFORM = 2048
STRATEGY_ACCOUNT_TASK_HISTORY_BUDGET_BLOCKER = (
    "strategy_account_task_history_budget_exhausted"
)
STRATEGY_ACCOUNT_RISK_HISTORY_BUDGET_BLOCKER = (
    "strategy_account_risk_history_budget_exhausted"
)
STRATEGY_ACCOUNT_QUERY_TIMEOUT_BLOCKER = (
    "strategy_account_query_timeout"
)
STRATEGY_ACCOUNT_QUERY_FAILED_BLOCKER = (
    "strategy_account_query_failed"
)
STRATEGY_BREAKER_QUERY_TIMEOUT_BLOCKER = (
    "strategy_breaker_query_timeout"
)
STRATEGY_BREAKER_QUERY_FAILED_BLOCKER = (
    "strategy_breaker_query_failed"
)
LOTTERY_LIST_QUERY_TIMEOUT_SECONDS = 2.0
REAL_RUN_EVIDENCE_LIST_QUERY_TIMEOUT_SECONDS = 2.0


MAX_IMPORT_TARGET_LINES = 1000
REPAIR_DISPATCH_INTENT_BINDING_READY = True
REPAIR_DISPATCH_BLOCKER = "worker_repair_intent_contract_not_ready"
REPAIR_LANE_HEALTH_QUERY_TIMEOUT_SECONDS = 5.0
_LOAD_EXECUTION_INTENT = object()


def _canonicalization_failure(exc: Exception) -> dict:
    if isinstance(exc, CanonicalizationError):
        reason_code = exc.code
        retryable = exc.retryable
    elif isinstance(exc, ValueError):
        value = str(exc)
        reason_code = (
            value
            if value in {
                "canonicalization_target_not_allowed",
                "canonicalization_redirect_limit_exceeded",
            }
            else "canonicalization_target_unrecognized"
        )
        retryable = False
    else:
        reason_code = "canonicalization_service_unavailable"
        retryable = True
    return {
        "code": "lottery_target_canonicalization_failed",
        "reason_code": reason_code,
        "retryable": retryable,
    }


class StrategyAccountRecommendations(dict):
    """Backward-compatible recommendations plus platform-local diagnostics."""

    def __init__(
        self,
        values: dict[str, list[dict]] | None = None,
        *,
        blockers_by_platform: dict[str, str] | None = None,
    ):
        super().__init__(values or {})
        self.blockers_by_platform = dict(blockers_by_platform or {})


def clamp_limit(value: int, minimum: int = 1, maximum: int = 200) -> int:
    return min(max(int(value or minimum), minimum), maximum)


async def repair_dispatch_workers_ready(platform: str) -> bool:
    """Check one platform repair group, without consulting sibling lanes."""

    try:
        binding = repair_task_stream_binding_for_platform(platform)
    except ValueError as exc:
        structured_log(
            "error",
            "repair_worker_lane_binding_invalid",
            platform=str(platform or ""),
            error=str(exc),
        )
        return False

    try:
        consumers = await asyncio.wait_for(
            redis.xinfo_consumers(
                binding.stream_key,
                binding.group_name,
            ),
            timeout=REPAIR_LANE_HEALTH_QUERY_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        structured_log(
            "error",
            "repair_worker_lane_consumers_unavailable",
            platform=binding.platform,
            stream=binding.stream_key,
            group=binding.group_name,
            error=str(exc),
        )
        return False
    consumer_idle_milliseconds_by_name = repair_consumer_idle_by_name(
        consumers
    )
    active_consumers = frozenset(
        name
        for name, idle_milliseconds in (
            consumer_idle_milliseconds_by_name.items()
        )
        if idle_milliseconds <= REPAIR_CONSUMER_MAX_IDLE_MILLISECONDS
    )
    if not consumer_idle_milliseconds_by_name:
        return False

    try:
        rows = await asyncio.wait_for(
            database.fetch_all(
                """SELECT worker_id,
                          detail,
                          TIMESTAMPDIFF(
                            SECOND, last_seen_at, NOW()
                          ) AS heartbeat_age_seconds
                   FROM worker_heartbeats
                   WHERE service_name = 'worker'
                     AND status = 'ok'
                     AND last_seen_at >= DATE_SUB(
                           NOW(), INTERVAL 45 SECOND
                         )"""
            ),
            timeout=REPAIR_LANE_HEALTH_QUERY_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        structured_log(
            "error",
            "repair_worker_capability_check_failed",
            platform=binding.platform,
            error=str(exc),
        )
        return False
    return worker_rows_support_repair_dispatch(
        rows,
        binding=binding,
        active_consumer_names=active_consumers,
        consumer_idle_milliseconds_by_name=(
            consumer_idle_milliseconds_by_name
        ),
    )


def _mysql_error_code(value) -> int | None:
    """Extract a numeric MySQL error code without depending on one driver type."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, (tuple, list)) and value:
        return _mysql_error_code(value[0])
    return None


def _is_mysql_duplicate_entry(exc: Exception) -> bool:
    """Return true only for MySQL duplicate-entry error 1062.

    ``databases`` may expose the DB-API exception directly or through a wrapper,
    so inspect the normal exception chain while avoiding message matching that
    could misclassify a timeout or an application error containing the word
    "duplicate".
    """
    pending = [exc]
    seen = set()
    while pending:
        current = pending.pop()
        marker = id(current)
        if marker in seen:
            continue
        seen.add(marker)

        if _mysql_error_code(getattr(current, "errno", None)) == 1062:
            return True
        if _mysql_error_code(getattr(current, "args", None)) == 1062:
            return True

        for attr in ("orig", "__cause__", "__context__"):
            nested = getattr(current, attr, None)
            if isinstance(nested, BaseException):
                pending.append(nested)
    return False


async def _record_post_commit_event(**event) -> str | None:
    """Record an event without turning a committed command into a false 500.

    The event store already retries and dead-letters critical events. This
    boundary also protects callers from an unexpected event-store exception and
    makes a returned ``None`` explicit in logs. Cancellation is deliberately not
    swallowed because ``CancelledError`` is not an ``Exception`` on supported
    Python versions.
    """
    try:
        event_id = await record_event(**event)
    except Exception as exc:
        structured_log(
            "error",
            "post_commit_event_record_failed",
            aggregate=event.get("aggregate"),
            aggregate_id=event.get("aggregate_id"),
            event_type=event.get("event_type"),
            error=str(exc),
        )
        return None
    if event_id is None:
        structured_log(
            "error",
            "post_commit_event_unrecorded",
            aggregate=event.get("aggregate"),
            aggregate_id=event.get("aggregate_id"),
            event_type=event.get("event_type"),
        )
    return event_id


def _sql_in_values(prefix: str, values) -> tuple[str, dict]:
    parameters = {}
    placeholders = []
    for index, value in enumerate(values):
        key = f"{prefix}_{index}"
        placeholders.append(f":{key}")
        parameters[key] = value
    return ", ".join(placeholders), parameters


def _platform_module_for_cross_platform_read(platform: str):
    """Keep one broken optional module inside its own read-model entry."""

    try:
        module = get_platform_module(platform)
    except (ImportError, PlatformModuleUnavailableError):
        return None, "platform_module_unavailable"
    if module is None:
        return None, "platform_module_unsupported"
    return module, None


@router.get("/adapters")
async def list_adapters():
    selector_config = await load_runtime_selector_config()
    adapters = []
    for key, raw_cfg in get_platforms().items():
        cfg = raw_cfg if isinstance(raw_cfg, dict) else {}
        platform_module, module_error = (
            _platform_module_for_cross_platform_read(key)
        )
        module_available = platform_module is not None
        runtime_real_adapter = (
            module_available
            and platform_has_runtime_real_adapter(selector_config, key)
        )
        adapter_kind = platform_real_adapter_kind(selector_config, key)
        if not module_available:
            adapter_status = "module_unavailable"
        elif adapter_kind in {"oauth", "manual_assisted"}:
            adapter_status = cfg.get("adapter_status", "planned")
        else:
            adapter_status = (
                "configured"
                if runtime_real_adapter
                else cfg.get("adapter_status", "planned")
            )
        spec = PLATFORM_MODULE_SPECS.get(key)
        phases = (
            platform_module.action_order
            if platform_module is not None
            else (spec.action_order if spec is not None else ())
        )
        adapters.append(
            {
                "platform": key,
                "label": cfg.get("label", key),
                "dry_run": bool(
                    platform_module
                    and platform_module.dry_run_supported
                ),
                "real_actions": bool(
                    module_available
                    and (
                        cfg.get("action_adapter", False)
                        or runtime_real_adapter
                    )
                ),
                "adapter_status": adapter_status,
                "adapter_kind": adapter_kind,
                "phases": list(phases),
                "notes": (
                    platform_module.notes
                    if platform_module is not None
                    else "platform_module_unavailable"
                ),
                "module_available": module_available,
                "module_error": module_error,
            }
        )
    return adapters


@router.get("/adapters/config")
async def get_adapter_config_status():
    config = await load_runtime_selector_config()
    platforms = []
    for platform in get_platforms():
        platform_module, module_error = (
            _platform_module_for_cross_platform_read(platform)
        )
        module_available = platform_module is not None
        required_phases = selector_phases_for_platform(platform)
        phase_status = {}
        configured = config.get(platform, {})
        if not isinstance(configured, dict):
            configured = {}
        for phase in required_phases:
            phase_status[phase] = bool(
                module_available
                and phase_configured(platform, configured, phase)
            )
        configured_complete = platform_selectors_complete(config, platform)
        spec = PLATFORM_MODULE_SPECS.get(platform)
        platforms.append(
            {
                "platform": platform,
                "configured": bool(
                    module_available
                    and (
                        configured_complete
                        or platform_has_runtime_real_adapter(
                            config,
                            platform,
                        )
                    )
                ),
                "selector_configured": bool(
                    module_available and configured_complete
                ),
                "adapter_kind": platform_real_adapter_kind(config, platform),
                "configuration_kind": (
                    platform_module.configuration_kind
                    if platform_module is not None
                    else (
                        spec.configuration_kind
                        if spec is not None
                        else "unavailable"
                    )
                ),
                "required_phases": list(required_phases),
                "phases": phase_status,
                "module_available": module_available,
                "module_error": module_error,
            }
        )
    return {
        "preferred_env": "DPMS_ADAPTER_SELECTORS_B64",
        "fallback_env": "DPMS_ADAPTER_SELECTORS",
        "required_phases": list(ADAPTER_PHASES),
        "required_phases_by_platform": {
            platform: list(selector_phases_for_platform(platform)) for platform in get_platforms()
        },
        "platforms": platforms,
    }


@router.put("/adapters/config")
async def save_adapter_config(data: AdapterSelectorConfigUpdate, request: Request):
    actor = require_min_role(request, "admin")
    if not isinstance(data.config, dict) or not data.config:
        raise HTTPException(400, detail="config must be a non-empty object")
    saved = []
    invalid = []
    for platform, config in data.config.items():
        if not get_platform(platform):
            invalid.append({"platform": platform, "error": "unsupported platform"})
            continue
        if not isinstance(config, dict):
            invalid.append({"platform": platform, "error": "platform config must be an object"})
            continue
        required_phases = selector_phases_for_platform(platform)
        phase_status = {phase: phase_configured(platform, config, phase) for phase in required_phases}
        configured_complete = selector_config_complete(platform, config)
        await save_platform_selector_config(platform, config)
        saved.append({"platform": platform, "configured": configured_complete, "phases": phase_status})
    if not saved:
        raise HTTPException(400, detail={"message": "no valid platform config", "invalid": invalid})
    await audit_event(
        request,
        action="adapter_selector_config.save",
        resource_type="adapter_selector_config",
        result="saved",
        risk_level="high",
        detail={"saved": saved, "invalid_count": len(invalid)},
    )
    for item in saved:
        await _record_post_commit_event(
            aggregate="platform",
            aggregate_id=item["platform"],
            event_type="AdapterSelectorConfigSaved",
            payload={"configured": item["configured"], "phases": item["phases"], "invalid_count": len(invalid)},
            actor_type="operator",
            actor_id=actor["actor_id"],
        )
    return {"status": "saved", "saved": saved, "invalid": invalid}


@router.delete("/adapters/config/{platform}")
async def clear_adapter_config(platform: str, request: Request):
    actor = require_min_role(request, "admin")
    require_confirmation(request)
    if not get_platform(platform):
        raise HTTPException(404, detail="Platform not found")
    await database.execute("DELETE FROM adapter_selector_configs WHERE platform = :platform", {"platform": platform})
    await audit_event(
        request,
        action="adapter_selector_config.clear",
        resource_type="adapter_selector_config",
        resource_id=platform,
        result="cleared",
        risk_level="high",
    )
    await _record_post_commit_event(
        aggregate="platform",
        aggregate_id=platform,
        event_type="AdapterSelectorConfigCleared",
        payload={"platform": platform},
        actor_type="operator",
        actor_id=actor["actor_id"],
    )
    return {"status": "cleared", "platform": platform}


@router.get("/")
async def list_lotteries(status: str = None, limit: int = 50):
    limit = clamp_limit(limit)
    query = "SELECT * FROM lotteries"
    values = {"limit": limit}
    if status:
        query += " WHERE status = :status"
        values["status"] = status
    query += " ORDER BY id DESC LIMIT :limit"
    try:
        rows = await asyncio.wait_for(
            database.fetch_all(query, values),
            timeout=max(
                float(LOTTERY_LIST_QUERY_TIMEOUT_SECONDS),
                0.001,
            ),
        )
    except asyncio.TimeoutError as exc:
        structured_log(
            "warning",
            "lottery_list_query_timeout",
            limit=values["limit"],
            status=status,
            timeout_seconds=LOTTERY_LIST_QUERY_TIMEOUT_SECONDS,
        )
        raise HTTPException(
            503,
            detail={"code": "lottery_list_query_timeout"},
        ) from exc
    return [serialize_lottery(r) for r in rows]


@router.get("/real-run/evidence")
async def list_real_run_evidence(
    status: str = None,
    limit: int = 50,
    account_id: int | None = None,
):
    selected_account = None
    if account_id is not None:
        if account_id <= 0:
            raise HTTPException(400, detail="account_id must be positive")
        selected_account = await database.fetch_one(
            "SELECT id, platform FROM accounts WHERE id = :account_id",
            {"account_id": account_id},
        )
        if not selected_account:
            raise HTTPException(404, detail="Account not found")
    query = "SELECT * FROM lotteries"
    values = {"limit": min(max(limit, 1), 100)}
    if status:
        query += " WHERE status = :status"
        values["status"] = status
    query += " ORDER BY id DESC LIMIT :limit"
    try:
        rows = await asyncio.wait_for(
            database.fetch_all(query, values),
            timeout=max(
                float(REAL_RUN_EVIDENCE_LIST_QUERY_TIMEOUT_SECONDS),
                0.001,
            ),
        )
    except asyncio.TimeoutError as exc:
        structured_log(
            "warning",
            "real_run_evidence_list_query_timeout",
            limit=values["limit"],
            status=status,
            timeout_seconds=REAL_RUN_EVIDENCE_LIST_QUERY_TIMEOUT_SECONDS,
        )
        raise HTTPException(
            503,
            detail={"code": "real_run_evidence_list_query_timeout"},
        ) from exc
    selector_config = await load_runtime_selector_config()
    real_run_enabled = await is_real_run_enabled()
    lottery_ids = [int(row["id"]) for row in rows]
    evidence_batch = await load_real_run_evidence_batch(rows, account_id=account_id)
    account_summaries = await real_run_account_risk_summaries(
        dict.fromkeys(str(row["platform"]) for row in rows)
    )
    execution_intents = await load_lottery_execution_intents(
        database,
        lottery_ids,
    )
    completion_authorities = (
        await load_real_run_completion_authorities_for_lotteries(
            {int(row["id"]): str(row["platform"]) for row in rows},
            execution_intents=execution_intents,
        )
    )
    action_ledgers = await bilibili_action_ledgers_for_lotteries(lottery_ids, limit=12)
    repair_platforms = tuple(
        dict.fromkeys(str(row["platform"]) for row in rows)
    )
    repair_readiness_values = (
        await asyncio.gather(
            *(
                repair_dispatch_workers_ready(platform)
                for platform in repair_platforms
            )
        )
        if REPAIR_DISPATCH_INTENT_BINDING_READY
        else (False,) * len(repair_platforms)
    )
    repair_workers_ready_by_platform = dict(
        zip(repair_platforms, repair_readiness_values)
    )
    items = []
    for row in rows:
        lottery = dict(row)
        lottery_id = int(lottery["id"])
        scoped_account_id = account_id
        account_scope_matches = bool(
            not selected_account
            or str(selected_account["platform"]) == str(lottery["platform"])
        )
        gate = await real_run_gate_status(
            lottery,
            selector_config=selector_config,
            real_run_enabled=real_run_enabled,
            account_summary=account_summaries[str(lottery["platform"])],
            evidence_batch=evidence_batch,
            account_id=scoped_account_id,
        )
        if not account_scope_matches:
            gate["allowed"] = False
            gate["blockers"] = ["account_platform_mismatch", *gate.get("blockers", [])]
            gate["next_action"] = "select_account"
        gate["selected_account_id"] = account_id
        gate["account_scope_matches_platform"] = account_scope_matches if account_id is not None else None
        gate["repair_plan"] = await build_lottery_repair_plan(
            lottery,
            completion_authority=completion_authorities[lottery_id],
            execution_intent=execution_intents.get(lottery_id),
            dispatch_runtime_ready=repair_workers_ready_by_platform.get(
                str(lottery["platform"]),
                False,
            ),
        )
        gate["action_ledger"] = action_ledgers[lottery_id]
        items.append(gate)
    return {"items": items, "selected_account_id": account_id}


def ordered_actions(platform: str, actions) -> list[str]:
    if not isinstance(actions, list):
        return []
    platform_module = get_platform_module(platform)
    if platform_module is None:
        return []
    selected = {str(action) for action in actions}
    return [action for action in platform_module.action_order if action in selected]


def missing_repair_actions(
    platform: str,
    required_actions: list[str],
    completed_actions: list[str],
) -> list[str]:
    completed = set(completed_actions)
    return [
        action
        for action in ordered_actions(platform, required_actions)
        if action not in completed
    ]


def require_dispatchable_lottery_state(locked, *, repair: bool = False) -> None:
    """Fail closed unless a locked lottery row is safe to claim.

    A terminal result is never a signal to replay the full action plan. Missing
    actions must use the dedicated repair path, and an unknown external outcome
    keeps ``execution_lock`` populated until an explicit reconciliation flow is
    implemented.
    """
    if not locked:
        raise HTTPException(404, detail="Lottery not found")
    status = str(locked["status"] or "").strip().lower()
    execution_lock = str(locked["execution_lock"] or "").strip()
    if execution_lock:
        raise HTTPException(409, detail="Lottery has an execution lock and requires settlement or reconciliation")
    if status != "pending":
        operation = "repair-dispatchable" if repair else "dispatchable"
        raise HTTPException(409, detail=f"Lottery status '{status}' is not {operation}")


def require_lottery_not_executing(locked, *, operation: str) -> None:
    """Protect execution semantics from concurrent operator mutations."""
    if not locked:
        raise HTTPException(404, detail="Lottery not found")
    status = str(locked["status"] or "").strip().lower()
    execution_lock = str(locked["execution_lock"] or "").strip()
    if execution_lock or status in {"claimed", "running"}:
        raise HTTPException(409, detail=f"Lottery cannot {operation} while an execution is active or unresolved")


def require_dispatch_snapshot_unchanged(locked, snapshot) -> None:
    """Reject a dispatch if its preflight snapshot changed before row claim."""
    locked_data = dict(locked)
    snapshot_data = dict(snapshot)
    for field in ("platform", "raw_url", "canonical_url", "rule_text"):
        if str(locked_data.get(field) or "") != str(snapshot_data.get(field) or ""):
            raise HTTPException(409, detail=f"Lottery {field} changed during dispatch preflight; retry review")
    if parse_json_field(locked_data.get("action_plan")) != parse_json_field(snapshot_data.get("action_plan")):
        raise HTTPException(409, detail="Lottery action plan changed during dispatch preflight; retry review")
    for field in (
        "authoritative_rule_snapshot_id",
        "rule_hash",
        "action_plan_hash",
    ):
        if str(locked_data.get(field) or "") != str(snapshot_data.get(field) or ""):
            raise HTTPException(409, detail=f"Lottery {field} changed during dispatch preflight; retry review")


def _translate_platform_policy_conflict(callback):
    try:
        return callback()
    except PlatformPolicyConflict as exc:
        raise HTTPException(
            exc.status_code,
            detail=exc.detail,
        ) from exc


def bilibili_plan_binding(
    lottery,
    *,
    require_executable: bool,
    execution_revision: int,
) -> dict:
    """Compatibility facade for the Bilibili-owned binding policy."""

    return _translate_platform_policy_conflict(
        lambda: get_platform_module("bilibili").build_dispatch_plan_binding(
            lottery=lottery,
            task_mode="real_run" if require_executable else "shadow_run",
            account={"execution_revision": execution_revision},
        )
    )


def manual_shadow_plan_binding(
    lottery,
    *,
    platform: str,
    execution_path_id: str,
    platform_label: str,
    execution_revision: int,
    selector_config: dict,
) -> dict:
    """Compatibility facade for shared manual binding hash mechanics."""

    return _translate_platform_policy_conflict(
        lambda: build_manual_shadow_plan_binding(
            lottery,
            platform=platform,
            execution_path_id=execution_path_id,
            platform_label=platform_label,
            execution_revision=execution_revision,
            selector_config=selector_config,
        )
    )


def xiaohongshu_manual_plan_binding(
    lottery,
    *,
    execution_revision: int,
    selector_config: dict,
) -> dict:
    """Bind a reviewed XHS plan for a side-effect-free shadow run only."""

    return manual_shadow_plan_binding(
        lottery,
        platform="xiaohongshu",
        execution_path_id=XIAOHONGSHU_MANUAL_EXECUTION_PATH,
        platform_label="Xiaohongshu",
        execution_revision=execution_revision,
        selector_config=selector_config,
    )


def douyin_manual_plan_binding(
    lottery,
    *,
    execution_revision: int,
    selector_config: dict,
) -> dict:
    """Bind a reviewed Douyin plan for a side-effect-free shadow run only."""

    return manual_shadow_plan_binding(
        lottery,
        platform="douyin",
        execution_path_id=DOUYIN_MANUAL_EXECUTION_PATH,
        platform_label="Douyin",
        execution_revision=execution_revision,
        selector_config=selector_config,
    )


def weibo_manual_plan_binding(
    lottery,
    *,
    execution_revision: int,
    selector_config: dict,
) -> dict:
    """Bind an explicitly selected Weibo manual fallback for shadow only."""

    return manual_shadow_plan_binding(
        lottery,
        platform="weibo",
        execution_path_id=WEIBO_MANUAL_EXECUTION_PATH,
        platform_label="Weibo",
        execution_revision=execution_revision,
        selector_config=selector_config,
    )


def weibo_oauth_plan_binding(
    lottery,
    *,
    require_executable: bool,
    execution_revision: int,
    weibo_rip: str = "",
) -> dict:
    """Compatibility facade for the Weibo-owned OAuth binding policy."""

    return _translate_platform_policy_conflict(
        lambda: get_platform_module("weibo").build_dispatch_plan_binding(
            lottery=lottery,
            task_mode="real_run" if require_executable else "dry_run",
            account={"execution_revision": execution_revision},
            selector_config={},
            stored_execution_path="weibo_oauth_v1",
            weibo_rip=weibo_rip,
        )
    )


def require_no_completed_actions_for_full_real_dispatch(completed_actions: list[str]) -> None:
    if completed_actions:
        raise HTTPException(
            409,
            detail={
                "message": "Lottery already has confirmed real actions; use repair or reconciliation instead of full dispatch",
                "completed_actions": completed_actions,
            },
        )


def require_repair_plan_unchanged(current_plan: dict, preflight_plan: dict) -> None:
    immutable_fields = (
        "execution_intent_id",
        "execution_intent_hash",
        "full_action_plan_hash",
        "full_required_actions_hash",
        "required_actions",
        "completed_actions",
        "missing_actions",
        "requested_actions_hash",
        "repair_action_plan_hash",
        "repair_action_plan",
    )
    if (
        not current_plan.get("eligible")
        or any(
            current_plan.get(field) != preflight_plan.get(field)
            for field in immutable_fields
        )
    ):
        raise HTTPException(
            409,
            detail={
                "message": "Completed-action evidence changed during repair preflight; rebuild the repair plan",
                "repair_plan": current_plan,
            },
        )


def normalize_action_ledger_row(row) -> dict:
    item = dict(row)
    item["ok"] = bool(item.get("ok"))
    return item


async def bilibili_action_ledger_for_lottery(lottery_id: int, limit: int = 20) -> list[dict]:
    try:
        rows = await database.fetch_all(
            """SELECT * FROM bilibili_action_ledger
               WHERE lottery_id = :lottery_id
               ORDER BY id DESC LIMIT :limit""",
            {"lottery_id": lottery_id, "limit": clamp_limit(limit)},
        )
    except Exception as exc:
        structured_log("warning", "bilibili_action_ledger_query_failed", lottery_id=lottery_id, error=str(exc))
        raise
    return [normalize_action_ledger_row(row) for row in rows]


async def bilibili_action_ledgers_for_lotteries(lottery_ids, limit: int = 20) -> dict[int, list[dict]]:
    """Load the newest N ledger rows per lottery with one MySQL 8 query."""
    ids = list(dict.fromkeys(int(lottery_id) for lottery_id in lottery_ids))
    ledgers = {lottery_id: [] for lottery_id in ids}
    if not ids:
        return ledgers
    lottery_clause, values = _sql_in_values("ledger_lottery", ids)
    values["ledger_limit"] = clamp_limit(limit)
    try:
        rows = await database.fetch_all(
            f"""SELECT ranked_ledger.*
                FROM (
                  SELECT bal.*,
                         ROW_NUMBER() OVER (
                           PARTITION BY bal.lottery_id
                           ORDER BY bal.id DESC
                         ) AS evidence_rank
                  FROM bilibili_action_ledger bal
                  WHERE bal.lottery_id IN ({lottery_clause})
                ) ranked_ledger
                WHERE evidence_rank <= :ledger_limit
                ORDER BY lottery_id, id DESC""",
            values,
        )
    except Exception as exc:
        structured_log(
            "warning",
            "bilibili_action_ledger_batch_query_failed",
            lottery_count=len(ids),
            error=str(exc),
        )
        # This endpoint is an evidence view, so storage failure must not be
        # rendered indistinguishably from a valid empty action history.
        raise
    for row in rows:
        try:
            lottery_id = int(row["lottery_id"])
        except (KeyError, TypeError, ValueError):
            continue
        if lottery_id not in ledgers:
            continue
        item = normalize_action_ledger_row(row)
        item.pop("evidence_rank", None)
        ledgers[lottery_id].append(item)
    return ledgers


async def completed_real_run_actions_from_ledger(
    lottery_id: int,
    platform: str,
) -> list[str]:
    # This evidence decides whether a full real-run can safely be replayed.
    # Query failure is not equivalent to "no completed actions" and must
    # propagate so dispatch/repair fail closed.
    rows = await database.fetch_all(
        """SELECT DISTINCT phase
           FROM bilibili_action_ledger
           WHERE lottery_id = :lottery_id
             AND task_mode = 'real_run'
             AND ok = 1
             AND phase IS NOT NULL""",
        {"lottery_id": lottery_id},
    )
    completed = [row["phase"] for row in rows if row["phase"]]
    return ordered_actions(platform, completed)


@router.get("/action-ledger")
async def list_bilibili_action_ledger(limit: int = 100, lottery_id: int = None, task_id: str = None):
    limit = clamp_limit(limit)
    where = []
    values = {"limit": limit}
    if lottery_id is not None:
        where.append("lottery_id = :lottery_id")
        values["lottery_id"] = lottery_id
    if task_id:
        where.append("task_id = :task_id")
        values["task_id"] = task_id
    query = "SELECT * FROM bilibili_action_ledger"
    if where:
        query += " WHERE " + " AND ".join(where)
    query += " ORDER BY id DESC LIMIT :limit"
    rows = await database.fetch_all(query, values)
    return [normalize_action_ledger_row(row) for row in rows]


def _external_action_authority_state(row, platform: str):
    values = dict(row)
    intent_id = str(values.get("intent_id") or "").strip()
    raw_action = str(
        values.get("phase") or values.get("action") or ""
    ).strip()
    platform_module = get_platform_module(platform)
    action = (
        platform_module.normalize_external_action(raw_action)
        if platform_module is not None
        else None
    )
    status = str(values.get("status") or "").strip().lower()
    effect_certainty = str(
        values.get("effect_certainty") or ""
    ).strip().lower()
    outcome = str(values.get("outcome") or "").strip().lower()
    valid_action = action is not None

    if (
        intent_id
        and valid_action
        and status == "succeeded"
        and effect_certainty == "confirmed_effect"
        and outcome == "ok"
    ):
        return action, "completed", None
    if (
        intent_id
        and valid_action
        and status == "failed"
        and effect_certainty == "confirmed_no_effect"
        and outcome in CONFIRMED_NO_EFFECT_OUTCOMES
    ):
        return action, "confirmed_no_effect", None

    expected_unsettled = {
        "pending": ("not_started", ""),
        "prepared": ("not_started", ""),
        "started": ("unknown", ""),
        "unknown": ("unknown", "unknown"),
    }.get(status)
    lifecycle_valid_unsettled = (
        bool(intent_id)
        and valid_action
        and expected_unsettled is not None
        and (effect_certainty, outcome) == expected_unsettled
    )
    return (
        action or raw_action,
        "blocked",
        ExternalActionAuthorityBlocker(
            intent_id=intent_id,
            action=action or raw_action,
            status=status,
            effect_certainty=effect_certainty,
            outcome=outcome,
            reason=(
                "external_action_intent_unsettled"
                if lifecycle_valid_unsettled
                else "external_action_intent_lifecycle_invalid"
            ),
        ),
    )


def _build_real_run_completion_authority(
    platform: str,
    *,
    legacy_completed_actions,
    external_action_rows,
) -> RealRunCompletionAuthority:
    external_actions: set[str] = set()
    external_completed: set[str] = set()
    external_no_effect: set[str] = set()
    blockers: list[ExternalActionAuthorityBlocker] = []
    for row in external_action_rows or []:
        action, state, blocker = _external_action_authority_state(
            row,
            platform,
        )
        if action:
            external_actions.add(action)
        if state == "completed":
            external_completed.add(action)
        elif state == "confirmed_no_effect":
            external_no_effect.add(action)
        elif blocker is not None:
            blockers.append(blocker)

    legacy_completed = set(
        ordered_actions(platform, list(legacy_completed_actions or []))
    )
    for action in sorted(legacy_completed & external_no_effect):
        blockers.append(
            ExternalActionAuthorityBlocker(
                intent_id="",
                action=action,
                status="conflict",
                effect_certainty="conflict",
                outcome="",
                reason="external_action_completion_authority_conflict",
            )
        )
    completed = [
        *external_completed,
        *(legacy_completed - external_actions),
    ]
    return RealRunCompletionAuthority(
        completed_actions=tuple(ordered_actions(platform, completed)),
        blockers=tuple(blockers),
    )


def require_completion_authority_settled(
    authority: RealRunCompletionAuthority,
) -> None:
    if not authority.blockers:
        return
    raise HTTPException(
        409,
        detail={
            "code": "reconciliation_required",
            "message": (
                "Prior real-run action intents must be reconciled before "
                "another real-run can be queued"
            ),
            "blockers": [
                blocker.public_view() for blocker in authority.blockers
            ],
        },
    )


def require_action_plan_mutation_safe(
    authority: RealRunCompletionAuthority,
) -> None:
    """Keep a partially executed frozen intent repairable.

    Once any external action is confirmed, changing the mutable lottery plan
    would make the immutable current intent fail its lottery binding while a
    full replay is correctly forbidden. Unknown/unsettled effects are blocked
    by the same reconciliation authority before considering confirmed actions.
    """

    require_completion_authority_settled(authority)
    completed_actions = list(authority.completed_actions)
    if completed_actions:
        raise HTTPException(
            409,
            detail={
                "code": "confirmed_real_actions_require_frozen_plan",
                "message": (
                    "The reviewed action plan is frozen after confirmed "
                    "real actions; finish the exact Repair plan before "
                    "authoring a new plan"
                ),
                "completed_actions": completed_actions,
            },
        )


async def load_real_run_completion_authority(
    lottery_id: int,
    platform: str,
    *,
    for_update: bool = False,
    execution_intent: (
        FrozenLotteryExecutionIntent | dict | object
    ) = _LOAD_EXECUTION_INTENT,
) -> RealRunCompletionAuthority:
    strict_scope = execution_intent is not _LOAD_EXECUTION_INTENT
    frozen_intent: FrozenLotteryExecutionIntent | None = None
    if strict_scope:
        try:
            frozen_intent = coerce_frozen_execution_intent(
                execution_intent
            )
        except (
            ExecutionIntentError,
            ImportError,
            KeyError,
            PlatformModuleUnavailableError,
            TypeError,
            ValueError,
        ):
            return RealRunCompletionAuthority(
                completed_actions=(),
                blockers=(
                    ExternalActionAuthorityBlocker(
                        intent_id="",
                        action="",
                        status="unavailable",
                        effect_certainty="unknown",
                        outcome="",
                        reason="execution_intent_scope_unavailable",
                    ),
                ),
            )
        if (
            frozen_intent.lottery_id != int(lottery_id)
            or frozen_intent.platform != str(platform)
        ):
            return RealRunCompletionAuthority(
                completed_actions=(),
                blockers=(
                    ExternalActionAuthorityBlocker(
                        intent_id="",
                        action="",
                        status="unavailable",
                        effect_certainty="unknown",
                        outcome="",
                        reason="execution_intent_scope_unavailable",
                    ),
                ),
            )

    values = {"lottery_id": lottery_id}
    binding_scope = ""
    ledger_binding_join = ""
    external_binding_join = ""
    task_binding_join = ""
    if frozen_intent is not None:
        values.update(
            {
                "scope_intent_id": frozen_intent.intent_id,
                "scope_account_id": frozen_intent.source_account_id,
            }
        )
        binding_scope = """
             AND execution_binding.intent_id = :scope_intent_id
             AND execution_binding.account_id = :scope_account_id"""
        ledger_binding_join = """
           JOIN task_execution_intent_bindings execution_binding
             ON execution_binding.task_id = ledger.task_id
            AND execution_binding.lottery_id = ledger.lottery_id
            AND execution_binding.account_id = ledger.account_id"""
        external_binding_join = """
           JOIN task_execution_intent_bindings execution_binding
             ON execution_binding.task_id = eai.task_id
            AND execution_binding.lottery_id = eai.lottery_id
            AND execution_binding.account_id = eai.account_id"""
        task_binding_join = """
           JOIN task_execution_intent_bindings execution_binding
             ON execution_binding.task_id = tr.task_id
            AND execution_binding.lottery_id = tr.lottery_id
            AND execution_binding.account_id = tr.account_id"""

    ledger_rows = await database.fetch_all(
        f"""SELECT DISTINCT ledger.phase
           FROM bilibili_action_ledger ledger
           {ledger_binding_join}
           WHERE ledger.lottery_id = :lottery_id
             AND ledger.task_mode = 'real_run'
             AND ledger.ok = 1
             AND ledger.phase IS NOT NULL
             {binding_scope}""",
        values,
    )
    ledger_completed = ordered_actions(
        platform,
        [row["phase"] for row in ledger_rows if row["phase"]],
    )
    external_action_lock = " FOR UPDATE" if for_update else ""
    external_action_rows = await database.fetch_all(
        f"""SELECT eai.intent_id, eai.action AS phase, eai.status,
                   eai.effect_certainty, eai.outcome
           FROM external_action_intents eai
           JOIN task_runs tr
             ON tr.task_id = eai.task_id
            AND tr.lottery_id = eai.lottery_id
           {external_binding_join}
           WHERE tr.lottery_id = :lottery_id
             AND tr.task_mode = 'real_run'
             {binding_scope}
           {external_action_lock}""",
        values,
    )
    event_rows = await database.fetch_all(
        f"""SELECT DISTINCT JSON_UNQUOTE(JSON_EXTRACT(e.payload, '$.phase')) AS phase
           FROM events e
           JOIN task_runs tr ON tr.task_id = e.correlation_id
           {task_binding_join}
           WHERE tr.lottery_id = :lottery_id
             AND tr.task_mode = 'real_run'
             AND e.event_type = 'TaskPhaseCompleted'
             {binding_scope}""",
        values,
    )
    legacy_rows = await database.fetch_all(
        f"""SELECT DISTINCT tp.phase
           FROM task_phases tp
           JOIN task_runs tr ON tr.task_id = tp.task_id
           {task_binding_join}
           WHERE tp.lottery_id = :lottery_id
             AND tr.task_mode = 'real_run'
             {binding_scope}""",
        values,
    )
    return _build_real_run_completion_authority(
        platform,
        legacy_completed_actions=[
            *ledger_completed,
            *[row["phase"] for row in event_rows if row["phase"]],
            *[row["phase"] for row in legacy_rows if row["phase"]],
        ],
        external_action_rows=external_action_rows,
    )


async def completed_real_run_actions(
    lottery_id: int,
    platform: str,
    *,
    for_update: bool = False,
) -> list[str]:
    """Compatibility projection; locked callers may not ignore blockers."""

    authority = await load_real_run_completion_authority(
        lottery_id,
        platform,
        for_update=for_update,
    )
    if for_update:
        require_completion_authority_settled(authority)
    return list(authority.completed_actions)


async def load_real_run_completion_authorities_for_lotteries(
    lottery_platforms,
    *,
    execution_intents=None,
) -> dict[int, RealRunCompletionAuthority]:
    """Batch-load completion authority, optionally scoped to frozen intents."""

    platform_by_id = {
        int(lottery_id): str(platform)
        for lottery_id, platform in dict(lottery_platforms).items()
    }
    ids = list(platform_by_id)
    if not ids:
        return {}

    strict_scopes = execution_intents is not None
    invalid_scope_ids: set[int] = set()
    scope_by_id: dict[int, FrozenLotteryExecutionIntent] = {}
    if strict_scopes:
        supplied = dict(execution_intents or {})
        for lottery_id in ids:
            candidate = supplied.get(lottery_id)
            if isinstance(candidate, ExecutionIntentLoadFailure):
                invalid_scope_ids.add(lottery_id)
                continue
            try:
                frozen = coerce_frozen_execution_intent(candidate)
            except (
                ExecutionIntentError,
                ImportError,
                KeyError,
                PlatformModuleUnavailableError,
                TypeError,
                ValueError,
            ):
                invalid_scope_ids.add(lottery_id)
                continue
            if (
                frozen.lottery_id != lottery_id
                or frozen.platform != platform_by_id[lottery_id]
            ):
                invalid_scope_ids.add(lottery_id)
                continue
            scope_by_id[lottery_id] = frozen
        query_ids = [
            lottery_id
            for lottery_id in ids
            if lottery_id in scope_by_id
        ]
    else:
        query_ids = ids

    def unavailable_authority(reason: str) -> RealRunCompletionAuthority:
        return RealRunCompletionAuthority(
            completed_actions=(),
            blockers=(
                ExternalActionAuthorityBlocker(
                    intent_id="",
                    action="",
                    status="unavailable",
                    effect_certainty="unknown",
                    outcome="",
                    reason=reason,
                ),
            ),
        )

    authorities: dict[int, RealRunCompletionAuthority] = {
        lottery_id: unavailable_authority(
            "execution_intent_scope_unavailable"
        )
        for lottery_id in invalid_scope_ids
    }
    if not query_ids:
        return authorities

    lottery_clause, values = _sql_in_values(
        "completed_lottery",
        query_ids,
    )
    binding_scope = ""
    if strict_scopes:
        clauses = []
        for index, lottery_id in enumerate(query_ids):
            frozen = scope_by_id[lottery_id]
            clauses.append(
                "("
                f"execution_binding.lottery_id = :scope_lottery_{index} "
                f"AND execution_binding.intent_id = :scope_intent_{index} "
                f"AND execution_binding.account_id = :scope_account_{index}"
                ")"
            )
            values[f"scope_lottery_{index}"] = lottery_id
            values[f"scope_intent_{index}"] = frozen.intent_id
            values[f"scope_account_{index}"] = frozen.source_account_id
        binding_scope = " AND (" + " OR ".join(clauses) + ")"

    ledger_binding_join = (
        """JOIN task_execution_intent_bindings execution_binding
             ON execution_binding.task_id = ledger.task_id
            AND execution_binding.lottery_id = ledger.lottery_id
            AND execution_binding.account_id = ledger.account_id"""
        if strict_scopes
        else ""
    )
    ledger_rows = await database.fetch_all(
        f"""SELECT /*+ MAX_EXECUTION_TIME(2000) */
                   DISTINCT ledger.lottery_id, ledger.phase
            FROM bilibili_action_ledger ledger
            {ledger_binding_join}
            WHERE ledger.lottery_id IN ({lottery_clause})
              AND ledger.task_mode = 'real_run'
              AND ledger.ok = 1
              AND ledger.phase IS NOT NULL
              {binding_scope}""",
        values,
    )
    external_binding_join = (
        """JOIN task_execution_intent_bindings execution_binding
             ON execution_binding.task_id = eai.task_id
            AND execution_binding.lottery_id = eai.lottery_id
            AND execution_binding.account_id = eai.account_id"""
        if strict_scopes
        else ""
    )
    external_action_rows = await database.fetch_all(
        f"""SELECT /*+ MAX_EXECUTION_TIME(2000) */
                   DISTINCT tr.lottery_id, eai.intent_id,
                    eai.action AS phase,
                    eai.status, eai.effect_certainty, eai.outcome
            FROM external_action_intents eai
            JOIN task_runs tr
              ON tr.task_id = eai.task_id
             AND tr.lottery_id = eai.lottery_id
            {external_binding_join}
            WHERE tr.lottery_id IN ({lottery_clause})
              AND tr.task_mode = 'real_run'
              {binding_scope}""",
        values,
    )
    event_binding_join = (
        """JOIN task_execution_intent_bindings execution_binding
             ON execution_binding.task_id = tr.task_id
            AND execution_binding.lottery_id = tr.lottery_id
            AND execution_binding.account_id = tr.account_id"""
        if strict_scopes
        else ""
    )
    event_rows = await database.fetch_all(
        f"""SELECT /*+ MAX_EXECUTION_TIME(2000) */
                   DISTINCT tr.lottery_id,
                    JSON_UNQUOTE(JSON_EXTRACT(e.payload, '$.phase')) AS phase
            FROM events e
            JOIN task_runs tr ON tr.task_id = e.correlation_id
            {event_binding_join}
            WHERE tr.lottery_id IN ({lottery_clause})
              AND tr.task_mode = 'real_run'
              AND e.event_type = 'TaskPhaseCompleted'
              {binding_scope}""",
        values,
    )
    phase_binding_join = (
        """JOIN task_execution_intent_bindings execution_binding
             ON execution_binding.task_id = tr.task_id
            AND execution_binding.lottery_id = tr.lottery_id
            AND execution_binding.account_id = tr.account_id"""
        if strict_scopes
        else ""
    )
    legacy_rows = await database.fetch_all(
        f"""SELECT /*+ MAX_EXECUTION_TIME(2000) */
                   DISTINCT tp.lottery_id, tp.phase
            FROM task_phases tp
            JOIN task_runs tr ON tr.task_id = tp.task_id
            {phase_binding_join}
            WHERE tp.lottery_id IN ({lottery_clause})
              AND tr.task_mode = 'real_run'
              {binding_scope}""",
        values,
    )
    legacy_completed = {lottery_id: [] for lottery_id in query_ids}
    external_by_lottery = {lottery_id: [] for lottery_id in query_ids}
    for row in [
        *ledger_rows,
        *event_rows,
        *legacy_rows,
    ]:
        try:
            lottery_id = int(row["lottery_id"])
        except (KeyError, TypeError, ValueError):
            continue
        phase = row["phase"]
        if lottery_id in legacy_completed and phase:
            legacy_completed[lottery_id].append(phase)
    for row in external_action_rows:
        try:
            lottery_id = int(row["lottery_id"])
        except (KeyError, TypeError, ValueError):
            continue
        if lottery_id in external_by_lottery:
            external_by_lottery[lottery_id].append(row)
    for lottery_id in query_ids:
        try:
            authorities[lottery_id] = (
                _build_real_run_completion_authority(
                    platform_by_id[lottery_id],
                    legacy_completed_actions=legacy_completed[lottery_id],
                    external_action_rows=external_by_lottery[lottery_id],
                )
            )
        except (ImportError, PlatformModuleUnavailableError):
            # This is a cross-platform read model.  Keep the unavailable
            # platform fail-closed without hiding evidence for healthy peers.
            authorities[lottery_id] = unavailable_authority(
                "platform_module_unavailable"
            )
    return authorities


async def completed_real_run_actions_for_lotteries(
    lottery_platforms,
) -> dict[int, list[str]]:
    """Compatibility projection of the structured batch authority."""

    authorities = await load_real_run_completion_authorities_for_lotteries(
        lottery_platforms
    )
    return {
        lottery_id: list(authority.completed_actions)
        for lottery_id, authority in authorities.items()
    }


async def build_lottery_repair_plan(
    lottery,
    *,
    completed_actions: list[str] | None = None,
    completion_authority: RealRunCompletionAuthority | None = None,
    execution_intent: (
        FrozenLotteryExecutionIntent
        | ExecutionIntentLoadFailure
        | dict
        | object
    ) = _LOAD_EXECUTION_INTENT,
    dispatch_runtime_ready: bool = False,
) -> dict:
    lottery_data = dict(lottery)
    platform = str(lottery_data.get("platform") or "")
    completion_platform_unavailable = bool(
        completion_authority is not None
        and any(
            blocker.reason == "platform_module_unavailable"
            for blocker in completion_authority.blockers
        )
    )
    dispatch_contract_supported = bool(REPAIR_DISPATCH_INTENT_BINDING_READY)
    dispatch_workers_ready = bool(dispatch_runtime_ready)
    dispatch_supported = bool(
        dispatch_contract_supported and dispatch_workers_ready
    )
    base_result = {
        "eligible": False,
        "dispatch_contract_supported": dispatch_contract_supported,
        "dispatch_workers_ready": dispatch_workers_ready,
        "dispatch_supported": dispatch_supported,
        "executable": False,
        "dispatch_blocker": (
            None if dispatch_supported else REPAIR_DISPATCH_BLOCKER
        ),
        "required_actions": [],
        "completed_actions": (
            []
            if completion_platform_unavailable
            else ordered_actions(platform, completed_actions or [])
        ),
        "missing_actions": [],
        "repair_action_plan": None,
        "repair_action_plan_hash": None,
        "requested_actions_hash": None,
        "execution_intent_contract_version": None,
        "execution_intent_id": None,
        "execution_intent_hash": None,
        "execution_intent_source_task_id": None,
        "full_action_plan_hash": None,
        "full_required_actions_hash": None,
        "integrity_blocker": None,
        "completion_authority_blockers": [],
    }
    if completion_platform_unavailable:
        result = dict(base_result)
        result["completion_authority_blockers"] = [
            blocker.public_view()
            for blocker in completion_authority.blockers
        ]
        result["reason"] = "reconciliation_required"
        return result
    if isinstance(execution_intent, ExecutionIntentLoadFailure):
        result = dict(base_result)
        result["reason"] = "execution_intent_invalid"
        result["integrity_blocker"] = execution_intent.code
        return result
    try:
        frozen_intent = (
            await load_lottery_execution_intent(
                database,
                int(lottery_data["id"]),
            )
            if execution_intent is _LOAD_EXECUTION_INTENT
            else (
                None
                if execution_intent is None
                else coerce_frozen_execution_intent(execution_intent)
            )
        )
    except (ExecutionIntentError, KeyError, TypeError, ValueError) as exc:
        result = dict(base_result)
        result["reason"] = "execution_intent_invalid"
        result["integrity_blocker"] = (
            exc.code
            if isinstance(exc, ExecutionIntentError)
            else "execution_intent_invalid"
        )
        return result
    if frozen_intent is None:
        result = dict(base_result)
        result["reason"] = "execution_intent_missing"
        result["integrity_blocker"] = "execution_intent_missing"
        return result

    base_result.update(frozen_intent.public_metadata())
    required_actions = list(frozen_intent.full_required_actions)
    base_result["required_actions"] = required_actions
    try:
        validate_lottery_execution_intent_binding(
            frozen_intent,
            lottery_data,
        )
    except ExecutionIntentError as exc:
        result = dict(base_result)
        result["reason"] = "execution_intent_lottery_binding_changed"
        result["integrity_blocker"] = exc.code
        return result

    if completion_authority is None and completed_actions is None:
        completion_authority = await load_real_run_completion_authority(
            int(lottery_data["id"]),
            platform,
            execution_intent=frozen_intent,
        )
    if completion_authority is not None:
        completed_actions = list(completion_authority.completed_actions)
        base_result["completion_authority_blockers"] = [
            blocker.public_view()
            for blocker in completion_authority.blockers
        ]
        if completion_authority.blockers:
            result = dict(base_result)
            result["completed_actions"] = completed_actions
            result["reason"] = "reconciliation_required"
            return result
    else:
        completed_actions = ordered_actions(platform, completed_actions)
    base_result["completed_actions"] = completed_actions
    missing_actions = missing_repair_actions(
        platform,
        required_actions,
        completed_actions,
    )
    base_result["missing_actions"] = missing_actions

    reason = "missing_actions_available"
    if not required_actions:
        reason = "required_actions_missing"
    elif not completed_actions:
        reason = "no_real_actions_completed"
    elif not missing_actions:
        reason = "no_missing_actions"

    if reason == "missing_actions_available":
        status = str(lottery_data.get("status") or "").strip().lower()
        execution_lock = str(lottery_data.get("execution_lock") or "").strip()
        if execution_lock or status in {"claimed", "running"}:
            reason = "execution_in_flight_or_reconciliation_required"
        elif status != "pending":
            reason = "lottery_not_pending"

    eligible = reason == "missing_actions_available"
    repair_action_plan = None
    repair_action_plan_hash = None
    requested_actions_hash = None
    if eligible:
        try:
            subset = build_repair_execution_subset(
                frozen_intent,
                missing_actions,
            )
        except ExecutionIntentError as exc:
            eligible = False
            reason = "execution_intent_repair_subset_invalid"
            base_result["integrity_blocker"] = exc.code
        else:
            repair_action_plan = subset.action_plan
            repair_action_plan_hash = subset.action_plan_hash
            requested_actions_hash = subset.requested_actions_hash

    base_result.update(
        {
            "eligible": eligible,
            "dispatch_contract_supported": dispatch_contract_supported,
            "dispatch_workers_ready": dispatch_workers_ready,
            "dispatch_supported": dispatch_supported,
            "executable": bool(eligible and dispatch_supported),
            "reason": reason,
            "repair_action_plan": repair_action_plan,
            "repair_action_plan_hash": repair_action_plan_hash,
            "requested_actions_hash": requested_actions_hash,
        }
    )
    return base_result


@router.get("/strategy/queue")
async def strategy_queue(limit: int = 20):
    try:
        return await asyncio.wait_for(
            _strategy_queue(limit),
            timeout=max(
                float(STRATEGY_EVALUATION_TIMEOUT_SECONDS),
                0.001,
            ),
        )
    except asyncio.TimeoutError as exc:
        structured_log(
            "warning",
            "strategy_evaluation_timeout",
            endpoint="queue",
            timeout_seconds=STRATEGY_EVALUATION_TIMEOUT_SECONDS,
        )
        raise HTTPException(
            503,
            detail={"code": "strategy_evaluation_timeout"},
        ) from exc


async def _strategy_queue(limit: int = 20):
    selector_config = await load_runtime_selector_config()
    real_run_enabled = await is_real_run_enabled()
    platform_knowledge = await load_strategy_platform_knowledge()
    strategy_limit = min(max(limit, 1), 100)
    try:
        rows = await asyncio.wait_for(
            database.fetch_all(
                f"""SELECT l.*,
                          {STRATEGY_TARGET_METRICS_SQL}
                   FROM lotteries l
                   WHERE l.status IN ('pending','claimed')
                     AND (l.expires_at IS NULL OR l.expires_at > UTC_TIMESTAMP())
                   ORDER BY l.value_score DESC, l.id ASC
                   LIMIT :limit""",
                {"limit": strategy_limit},
            ),
            timeout=max(float(STRATEGY_QUERY_TIMEOUT_SECONDS), 0.001),
        )
    except asyncio.TimeoutError as exc:
        structured_log(
            "warning",
            "strategy_queue_query_timeout",
            limit=strategy_limit,
            timeout_seconds=STRATEGY_QUERY_TIMEOUT_SECONDS,
        )
        raise HTTPException(
            503,
            detail={"code": "strategy_queue_query_timeout"},
        ) from exc
    candidate_prefilter = (
        await load_account_scoped_readiness_candidate_prefilter(rows)
    )
    # Strategy recommendations are actionable candidates, so do not rank an
    # account currently held by another operation. The generic projection
    # remains lease-neutral for advisory Learning compatibility.
    account_recommendations = (
        await load_strategy_account_recommendations(
            exclude_active_leases=True,
        )
    )
    breaker_statuses = await load_strategy_breaker_statuses(
        row["platform"] for row in rows
    )
    account_readiness = (
        await evaluate_account_scoped_real_run_readiness_batch(
            rows,
            account_candidates=account_recommendations,
            candidate_prefilter=candidate_prefilter,
            recommendation_blockers_by_platform=getattr(
                account_recommendations,
                "blockers_by_platform",
                {},
            ),
        )
    )

    items = []
    for row in rows:
        item = await compute_strategy_item(
            dict(row),
            selector_config=selector_config,
            real_run_enabled=real_run_enabled,
            platform_knowledge=platform_knowledge,
            account_recommendations=account_recommendations,
            account_readiness=account_readiness.get(int(row["id"])),
            breaker_status=breaker_statuses.get(str(row["platform"])),
        )
        items.append(item)

    items.sort(key=lambda row: row["strategy_score"], reverse=True)
    for rank, item in enumerate(items, start=1):
        item["rank"] = rank
    return {"items": items, "count": len(items)}


@router.get("/{lottery_id}/strategy/explain")
async def explain_lottery_strategy(lottery_id: int):
    try:
        return await asyncio.wait_for(
            _explain_lottery_strategy(lottery_id),
            timeout=max(
                float(STRATEGY_EVALUATION_TIMEOUT_SECONDS),
                0.001,
            ),
        )
    except asyncio.TimeoutError as exc:
        structured_log(
            "warning",
            "strategy_evaluation_timeout",
            endpoint="explain",
            lottery_id=lottery_id,
            timeout_seconds=STRATEGY_EVALUATION_TIMEOUT_SECONDS,
        )
        raise HTTPException(
            503,
            detail={"code": "strategy_evaluation_timeout"},
        ) from exc


async def _explain_lottery_strategy(lottery_id: int):
    selector_config = await load_runtime_selector_config()
    real_run_enabled = await is_real_run_enabled()
    row = await database.fetch_one(
        f"""SELECT l.*,
                  {STRATEGY_TARGET_METRICS_SQL}
           FROM lotteries l
           WHERE l.id = :lottery_id""",
        {"lottery_id": lottery_id},
    )
    if not row:
        raise HTTPException(404, detail="Lottery not found")

    item = dict(row)
    platform_knowledge = await load_strategy_platform_knowledge()
    candidate_prefilter = (
        await load_account_scoped_readiness_candidate_prefilter([item])
    )
    account_recommendations = await load_strategy_account_recommendations(
        item["platform"],
        exclude_active_leases=True,
    )
    breaker_statuses = await load_strategy_breaker_statuses(
        [item["platform"]]
    )
    account_readiness = (
        await evaluate_account_scoped_real_run_readiness_batch(
            [item],
            account_candidates=account_recommendations,
            candidate_prefilter=candidate_prefilter,
            recommendation_blockers_by_platform=getattr(
                account_recommendations,
                "blockers_by_platform",
                {},
            ),
        )
    )
    return await compute_strategy_item(
        item,
        selector_config=selector_config,
        real_run_enabled=real_run_enabled,
        platform_knowledge=platform_knowledge,
        account_recommendations=account_recommendations,
        account_readiness=account_readiness.get(int(item["id"])),
        breaker_status=breaker_statuses.get(str(item["platform"])),
        include_breakdown=True,
    )


@router.get("/sources")
async def list_tracked_sources():
    rows = await database.fetch_all("SELECT * FROM tracked_sources ORDER BY id DESC")
    sources = []
    for row in rows:
        source = dict(row)
        platform_module, module_error = (
            _platform_module_for_cross_platform_read(
                source.get("platform")
            )
        )
        validation_error = None
        if module_error == "platform_module_unavailable":
            validation_error = module_error
        elif platform_module is None:
            validation_error = "platform_discovery_source_platform_unsupported"
        else:
            try:
                platform_module.validate_discovery_source_config(
                    source.get("source_type"),
                    source.get("source_value"),
                )
            except PlatformCapabilityError as exc:
                validation_error = exc.code
            except Exception:
                # Historical rows are untrusted input. Keep this read endpoint
                # fail-closed without turning one malformed row into a 500 for
                # every platform's source list.
                validation_error = "platform_discovery_source_validation_failed"
        source["effective_active"] = bool(source.get("active")) and not validation_error
        source["validation_error"] = validation_error
        sources.append(source)
    return sources


@router.post("/sources")
async def create_tracked_source(data: TrackedSourceCreate, request: Request):
    actor = require_min_role(request, "operator")
    platform_module = get_platform_module(data.platform)
    if platform_module is None or not get_platform(platform_module.platform_id):
        raise HTTPException(400, detail=f"Unsupported platform: {data.platform}")
    try:
        source_type, source_value = platform_module.validate_discovery_source_config(
            data.source_type,
            data.source_value
        )
    except PlatformCapabilityError as exc:
        if exc.code == "platform_discovery_source_type_not_supported":
            # Preserve the pre-module API's string-shaped FastAPI detail.
            # Generated/legacy clients may not accept an object here.
            detail = (
                platform_module.discovery_source_type_error
                or f"source_type must be {', '.join(exc.allowed)}"
            )
        elif exc.code == "platform_discovery_source_value_required":
            detail = "source_value is required"
        else:
            detail = exc.code
        raise HTTPException(400, detail=detail) from exc
    if data.scan_interval_minutes < 1:
        raise HTTPException(400, detail="scan_interval_minutes must be >= 1")
    platform = platform_module.platform_id
    source_id = await database.execute(
        """INSERT INTO tracked_sources (platform, source_type, source_value, scan_interval_minutes, active)
           VALUES (:platform, :source_type, :source_value, :scan_interval_minutes, 1)
           ON DUPLICATE KEY UPDATE id = LAST_INSERT_ID(id),
                                   scan_interval_minutes = :scan_interval_minutes,
                                   active = 1""",
        {
            "platform": platform,
            "source_type": source_type,
            "source_value": source_value,
            "scan_interval_minutes": data.scan_interval_minutes,
        },
    )
    await _record_post_commit_event(
        aggregate="source",
        # Some database adapters can still omit lastrowid on an upsert. Never
        # place the operator-supplied source value in the VARCHAR(128) event
        # identity: it may exceed that boundary and it is not an identifier.
        aggregate_id=source_id
        or str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"dpms:source:{platform}:{source_type}:{source_value}",
            )
        ),
        event_type="DiscoverySourceCreated",
        payload={
            "platform": platform,
            "source_type": source_type,
            "scan_interval_minutes": data.scan_interval_minutes,
        },
        actor_type="operator",
        actor_id=actor["actor_id"],
    )
    return {"status": "created", "id": source_id}


@router.post("/sources/scan")
async def scan_tracked_sources(request: Request):
    actor = require_min_role(request, "operator")
    runtime_role = str(
        getattr(request.app.state, "runtime_role", "all") or "all"
    ).strip().casefold()
    stats = (
        await dispatch_manual_discovery_scan()
        if runtime_role == "control"
        else await run_discovery()
    )
    await _record_post_commit_event(
        aggregate="system",
        aggregate_id="discovery",
        event_type="DiscoveryScanCompleted",
        payload=stats,
        actor_type="operator",
        actor_id=actor["actor_id"],
    )
    return {"status": "scanned", **stats}


@router.post("/")
async def create_lottery(data: LotteryCreate, request: Request):
    actor = require_min_role(request, "operator")
    if not get_platform(data.platform):
        raise HTTPException(400, detail=f"Unsupported platform: {data.platform}")

    raw_url = data.raw_url.strip()
    target = validate_lottery_target(data.platform, raw_url)
    if not target.valid:
        raise HTTPException(400, detail=target.reason)
    try:
        canonical_url = await canonicalize_lottery_url(data.platform, raw_url)
    except Exception as exc:
        failure = _canonicalization_failure(exc)
        structured_log(
            "warning",
            "lottery_target_canonicalization_failed",
            platform=str(data.platform or "").strip().casefold(),
            reason_code=failure["reason_code"],
            retryable=failure["retryable"],
            exception_type=type(exc).__name__,
        )
        raise HTTPException(
            503 if failure["retryable"] else 400,
            detail=failure,
        ) from exc
    try:
        lottery_id = await database.execute(
            """INSERT INTO lotteries (platform, source_type, source_id, raw_url, canonical_url, value_score, expires_at, status)
               VALUES (:platform, :source_type, :source_id, :raw_url, :canonical_url, :value_score, :expires_at, 'pending')""",
            {
                "platform": data.platform,
                "source_type": data.source_type,
                "source_id": data.source_id,
                "raw_url": raw_url,
                "canonical_url": canonical_url,
                "value_score": data.value_score,
                "expires_at": data.expires_at,
            },
        )
    except Exception as exc:
        if not _is_mysql_duplicate_entry(exc):
            raise
        row = await database.fetch_one(
            """SELECT id FROM lotteries
               WHERE url_hash = SHA2(:canonical_url, 256)
                 AND canonical_url = :canonical_url""",
            {"canonical_url": canonical_url},
        )
        if row:
            return {"status": "exists", "id": row["id"]}
        raise
    await _record_post_commit_event(
        aggregate="lottery",
        aggregate_id=lottery_id,
        event_type="LotteryDiscovered",
        payload={
            "platform": data.platform,
            "source_type": data.source_type,
            "source_id": data.source_id,
            "raw_url": raw_url,
            "canonical_url": canonical_url,
            "value_score": data.value_score,
            "manual": True,
        },
        actor_type="operator",
        actor_id=actor["actor_id"],
    )
    return {"status": "created", "id": lottery_id}


@router.post("/targets/import")
async def import_lottery_targets(data: LotteryTargetImport, request: Request):
    actor = require_min_role(request, "operator")
    if not get_platform(data.platform):
        raise HTTPException(400, detail=f"Unsupported platform: {data.platform}")
    if not data.content.strip():
        raise HTTPException(400, detail="content is required")

    rows = parse_target_lines(data.content, data.platform, data.value_score)
    if not rows:
        raise HTTPException(400, detail="No valid target lines found")
    if len(rows) > MAX_IMPORT_TARGET_LINES:
        raise HTTPException(413, detail=f"Too many target lines; max={MAX_IMPORT_TARGET_LINES}")

    # A short URL requires bounded external redirect resolution. Keep that
    # budget per platform: mixed DPMS exports are an envelope of independent
    # platform batches, so adding a short-link format to one platform must not
    # consume another platform's allowance. The platform catalog itself is
    # finite, which also keeps total external work bounded before the first
    # canonicalization or database write.
    short_link_counts: dict[str, int] = {}
    short_link_policies: dict[str, tuple[int, str]] = {}
    validated_targets: dict[int, LotteryTargetValidation | Exception] = {}
    for row in rows:
        if row.get("error"):
            continue
        try:
            target = validate_lottery_target(row["platform"], row["raw_url"])
        except Exception as exc:
            validated_targets[int(row["line"])] = exc
            continue
        validated_targets[int(row["line"])] = target
        if not target.valid:
            continue
        if target.kind == "short_link":
            platform = str(row["platform"]).strip().casefold()
            platform_module = get_platform_module(platform)
            short_link_policies.setdefault(
                platform,
                (
                    platform_module.target_import_short_link_limit,
                    platform_module.target_import_short_link_error,
                ),
            )
            count = short_link_counts.get(platform, 0) + 1
            short_link_counts[platform] = count
    blocked_short_link_errors = {
        platform: short_link_policies[platform][1]
        for platform, count in short_link_counts.items()
        if count > short_link_policies[platform][0]
    }
    blocked_short_link_platforms = frozenset(blocked_short_link_errors)
    short_link_rows = []
    for row in rows:
        target = validated_targets.get(int(row["line"]))
        if (
            target is None
            or isinstance(target, Exception)
            or not target.valid
            or target.kind != "short_link"
        ):
            continue
        platform = str(row["platform"]).strip().casefold()
        if platform not in blocked_short_link_platforms:
            short_link_rows.append(row)

    # Each platform owns at most one bounded redirect chain. Resolve those
    # independent network reads concurrently so a valid four-platform export
    # remains within the existing proxy/request timeout instead of multiplying
    # it by the number of platforms. Direct targets stay on the ordered insert
    # path below and do not consume this external-work fan-out.
    short_link_results = await asyncio.gather(
        *(
            canonicalize_lottery_url(row["platform"], row["raw_url"])
            for row in short_link_rows
        ),
        return_exceptions=True,
    )
    short_link_canonical_urls = {
        int(row["line"]): result
        for row, result in zip(short_link_rows, short_link_results)
    }

    import_id = str(uuid.uuid4())
    created = []
    duplicates = []
    invalid = []
    for row in rows:
        if row.get("error"):
            invalid.append(row)
            continue
        target = validated_targets.get(int(row["line"]))
        if isinstance(target, Exception):
            invalid.append({"line": row["line"], "raw": row["raw"], "error": str(target) or "target validation failed"})
            continue
        if target is None:
            invalid.append({"line": row["line"], "raw": row["raw"], "error": "target validation failed"})
            continue
        if not target.valid:
            invalid.append({"line": row["line"], "raw": row["raw"], "error": target.reason})
            continue
        if (
            target.kind == "short_link"
            and str(row["platform"]).strip().casefold()
            in blocked_short_link_platforms
        ):
            # Reject only the unsafe short-link rows from this platform
            # sub-batch. Direct rows and every peer platform remain
            # independently importable, and no redirect is followed for the
            # over-budget platform.
            invalid.append(
                {
                    "line": row["line"],
                    "raw": row["raw"],
                    "platform": row["platform"],
                    "error": blocked_short_link_errors[
                        str(row["platform"]).strip().casefold()
                    ],
                }
            )
            continue
        canonical_url = short_link_canonical_urls.get(int(row["line"]))
        if canonical_url is None:
            try:
                canonical_url = await canonicalize_lottery_url(
                    row["platform"],
                    row["raw_url"],
                )
            except Exception as exc:
                failure = _canonicalization_failure(exc)
                invalid.append(
                    {
                        "line": row["line"],
                        "raw": row["raw"],
                        "platform": row["platform"],
                        "error": failure["code"],
                        "reason_code": failure["reason_code"],
                        "retryable": failure["retryable"],
                    }
                )
                continue
        elif isinstance(canonical_url, Exception):
            exc = canonical_url
            failure = _canonicalization_failure(exc)
            invalid.append(
                {
                    "line": row["line"],
                    "raw": row["raw"],
                    "platform": row["platform"],
                    "error": failure["code"],
                    "reason_code": failure["reason_code"],
                    "retryable": failure["retryable"],
                }
            )
            continue
        try:
            lottery_id = await database.execute(
                """INSERT INTO lotteries (platform, source_type, source_id, raw_url, canonical_url, value_score, expires_at, status)
                   VALUES (:platform, :source_type, :source_id, :raw_url, :canonical_url, :value_score, :expires_at, 'pending')""",
                {
                    "platform": row["platform"],
                    "source_type": data.source_type,
                    "source_id": data.source_id,
                    "raw_url": row["raw_url"],
                    "canonical_url": canonical_url,
                    "value_score": row["value_score"],
                    "expires_at": row["expires_at"],
                },
            )
        except Exception as exc:
            existing = None
            if _is_mysql_duplicate_entry(exc):
                try:
                    existing = await database.fetch_one(
                        """SELECT id FROM lotteries
                           WHERE url_hash = SHA2(:canonical_url, 256)
                             AND canonical_url = :canonical_url""",
                        {"canonical_url": canonical_url},
                    )
                except Exception:
                    existing = None
            if existing:
                duplicates.append({"line": row["line"], "id": existing["id"], "url": row["raw_url"], "platform": row["platform"]})
            else:
                invalid.append({"line": row["line"], "raw": row["raw"], "error": str(exc) or "insert failed"})
            continue
        created.append({"line": row["line"], "id": lottery_id, "url": row["raw_url"], "platform": row["platform"]})
        await _record_post_commit_event(
            aggregate="lottery",
            aggregate_id=lottery_id,
            event_type="LotteryDiscovered",
            payload={
                "platform": row["platform"],
                "source_type": data.source_type,
                "source_id": data.source_id,
                "raw_url": row["raw_url"],
                "canonical_url": canonical_url,
                "value_score": row["value_score"],
                "line": row["line"],
                "import_id": import_id,
            },
            correlation_id=import_id,
            actor_type="operator",
            actor_id=actor["actor_id"],
        )

    result = {
        "status": "imported",
        "received": len(rows),
        "created_count": len(created),
        "duplicate_count": len(duplicates),
        "invalid_count": len(invalid),
        "created": created[:50],
        "duplicates": duplicates[:50],
        "invalid": invalid[:50],
    }
    await _record_post_commit_event(
        aggregate="lottery_import",
        aggregate_id=import_id,
        event_type="LotteryTargetImportCompleted",
        payload={
            "default_platform": data.platform,
            "source_type": data.source_type,
            "received": len(rows),
            "created_count": len(created),
            "duplicate_count": len(duplicates),
            "invalid_count": len(invalid),
        },
        correlation_id=import_id,
        actor_type="operator",
        actor_id=actor["actor_id"],
    )
    return result


@router.post("/{lottery_id}/dispatch")
async def dispatch_lottery(lottery_id: int, data: DispatchTaskRequest, request: Request):
    actor = require_min_role(request, "operator")
    lottery = await database.fetch_one(
        """SELECT l.*,
                  IF(l.expires_at IS NOT NULL
                     AND l.expires_at <= UTC_TIMESTAMP(), 1, 0) AS target_expired
             FROM lotteries l
            WHERE l.id = :id""",
        {"id": lottery_id},
    )
    if not lottery:
        raise HTTPException(404, detail="Lottery not found")
    if int(dict(lottery).get("target_expired") or 0) == 1:
        raise HTTPException(
            409,
            detail={
                "message": "Lottery participation deadline has passed",
                "blockers": ["lottery_target_expired"],
            },
        )

    platform_module = get_platform_module(lottery["platform"])
    platform_cfg = get_platform(lottery["platform"])
    if platform_module is None or not platform_cfg:
        raise HTTPException(400, detail=f"Unsupported platform: {lottery['platform']}")

    selector_config = await load_runtime_selector_config()
    platform_selectors = selector_config.get(lottery["platform"], {})
    real_adapter_enabled = platform_cfg.get("action_adapter", False) or platform_has_runtime_real_adapter(selector_config, lottery["platform"])
    task_mode = resolve_task_mode(data)
    dry_run = task_mode != "real_run"
    stored_plan = parse_json_field(lottery["action_plan"])
    stored_execution_path = (
        str(stored_plan.get("execution_path_id") or "")
        if isinstance(stored_plan, dict)
        else ""
    )
    weibo_rip = ""
    manual_shadow_only = bool(
        not platform_module.dry_run_supported
        or platform_module.non_executable_error(stored_execution_path)
    )
    if manual_shadow_only and task_mode == "dry_run":
        # Manual-only plans must not fall through to the mutation-capable
        # selector flow. Douyin may also contain ``favorited``, which the
        # current task_phases schema cannot persist without a migration.
        raise HTTPException(
            409,
            detail={
                "message": f"{lottery['platform']} plans support manual-assisted shadow only",
                "blockers": [f"{lottery['platform']}_manual_shadow_only"],
            },
        )
    target = validate_lottery_target(lottery["platform"], lottery["raw_url"])
    if task_mode in {"shadow_run", "real_run"} and not target.valid:
        raise HTTPException(400, detail=target.reason)
    if task_mode == "shadow_run":
        shadow_contract = await validate_real_run_evidence(lottery, account_id=None)
        if not shadow_contract.get("action_plan_ready"):
            platform_label = platform_cfg.get("label", lottery["platform"])
            raise HTTPException(
                409,
                detail={
                    "message": f"Shadow-run requires an attested, exact {platform_label} Action Plan v2",
                    "blockers": [
                        blocker
                        for blocker in shadow_contract.get("blockers", [])
                        if blocker not in {"execution_account_scope_required", "exact_execution_evidence_required"}
                    ],
                },
            )
        missing_phases = missing_manual_shadow_selector_phases(
            lottery["platform"],
            (
                stored_plan.get("required_actions") or ()
                if isinstance(stored_plan, dict)
                else ()
            ),
            platform_selectors,
            stored_execution_path,
        )
        if missing_phases:
            raise HTTPException(
                409,
                detail={
                    "message": (
                        "Shadow observation selectors are incomplete "
                        f"for {lottery['platform']}"
                    ),
                    "blockers": ["manual_shadow_selector_config_incomplete"],
                    "missing_phases": list(missing_phases),
                },
            )

    if task_mode == "real_run":
        require_min_role(request, "admin")
        try:
            if not platform_module.real_run_supported:
                raise HTTPException(
                    409,
                    detail={
                        "message": (
                            f"{platform_cfg.get('label', lottery['platform'])} "
                            "supports manual-assisted shadow only"
                        ),
                        "blockers": [platform_module.real_run_blocker],
                    },
                )
            path_blockers = platform_module.execution_path_blockers(
                stored_execution_path
            )
            if path_blockers:
                raise HTTPException(
                    409,
                    detail={
                        "message": (
                            f"{platform_cfg.get('label', lottery['platform'])} "
                            "execution path does not authorize real actions"
                        ),
                        "blockers": path_blockers,
                    },
                )
            required_actions = set(
                stored_plan.get("required_actions") or []
                if isinstance(stored_plan, dict)
                else []
            )
            if platform_module.requires_public_ingress(
                required_actions=required_actions
            ):
                weibo_rip = trusted_weibo_rip(request)
            require_confirmation(request)
            if not data.confirm:
                raise HTTPException(409, detail="Real-run body confirmation required")
            if not await is_real_run_enabled():
                raise HTTPException(403, detail="Global real-run switch is disabled")
            breaker_allowed, breaker_reason = await circuit_breaker_allows(lottery["platform"])
            if not breaker_allowed:
                raise HTTPException(423, detail=f"Circuit breaker blocks real-run: {breaker_reason}")
            if not real_adapter_enabled:
                raise HTTPException(
                    400,
                    detail=f"Real actions for {lottery['platform']} are not implemented yet. Use dry run or wait for adapter calibration.",
                )
        except HTTPException as exc:
            await audit_event(
                request,
                action="lottery.dispatch.real",
                resource_type="lottery",
                resource_id=lottery_id,
                result="blocked",
                risk_level="critical",
                detail={"platform": lottery["platform"], "reason": exc.detail},
            )
            await _record_post_commit_event(
                aggregate="lottery",
                aggregate_id=lottery_id,
                event_type="RealRunDenied",
                payload={"platform": lottery["platform"], "reason": exc.detail},
                actor_type="operator",
                actor_id=actor["actor_id"],
                critical=True,
            )
            await emit_real_run_gate_notification(lottery, exc.detail, actor_id=actor["actor_id"])
            raise
    account = await pick_account(
        data.account_id,
        lottery["platform"],
        execution_path_id=account_execution_path_for_dispatch(
            lottery["platform"],
            task_mode=task_mode,
            stored_execution_path=stored_execution_path,
        ),
        required_actions=platform_module.account_required_actions_for_dispatch(
            required_actions=(
                tuple(stored_plan.get("required_actions") or ())
                if isinstance(stored_plan, dict)
                else ()
            ),
            task_mode=task_mode,
        ),
        require_account_capability=(task_mode == "real_run"),
    )
    if not account:
        await _record_post_commit_event(
            aggregate="lottery",
            aggregate_id=lottery_id,
            event_type="TaskDispatchBlocked",
            payload={"platform": lottery["platform"], "reason": "no_available_account", "dry_run": dry_run, "mode": task_mode},
            actor_type="operator",
            actor_id=actor["actor_id"],
        )
        raise HTTPException(400, detail="No available account. Create a ready account first.")

    # The Governance policy is now the single real-run authority (P1-4): a
    # decision is evaluated against the *chosen* account, recorded, and only an
    # "allow" outcome may dispatch. The recorded decision_id / policy_version are
    # bound to the task below, so every real run is traceable to the exact gate
    # decision that authorised it.
    decision_id = None
    policy_version = None
    decision_gate: dict = {}
    if task_mode == "real_run":
        decision = await evaluate_real_run_decision(lottery, account_id=account["id"], record=True)
        decision_id = decision["decision_id"]
        policy_version = decision["policy_version"]
        decision_gate = dict(decision.get("gate") or {})
        if not decision["allowed"]:
            await _record_post_commit_event(
                aggregate="lottery",
                aggregate_id=lottery_id,
                event_type="RealRunDenied",
                payload={
                    "platform": lottery["platform"],
                    "account_id": account["id"],
                    "blockers": decision["blockers"],
                    "failed_gates": decision["failed_gates"],
                    "decision_id": decision_id,
                    "policy_version": policy_version,
                },
                actor_type="operator",
                actor_id=actor["actor_id"],
                critical=True,
            )
            await emit_real_run_gate_notification(
                lottery,
                {
                    "message": "Real-run policy gate is not satisfied",
                    "blockers": decision["blockers"],
                    "failed_gates": decision["failed_gates"],
                    "account_id": account["id"],
                    "decision_id": decision_id,
                    "policy_version": policy_version,
                },
                actor_id=actor["actor_id"],
            )
            raise HTTPException(
                409,
                detail={
                    "message": "Real-run policy gate is not satisfied",
                    "blockers": decision["blockers"],
                    "failed_gates": decision["failed_gates"],
                    "decision_id": decision_id,
                    "policy_version": policy_version,
                },
            )

    task_id = str(uuid.uuid4())
    action_plan = parse_json_field(lottery["action_plan"]) or {}
    plan_binding = {
        "rule_snapshot_id": None,
        "rule_hash": None,
        "action_plan_hash": None,
        "execution_path_id": None,
        "target_hash": None,
        "config_hash": None,
        "execution_revision": None,
        "required_actions": (),
        "follow_target_handle": "",
        "action_plan": action_plan,
    }
    module_plan_binding = _translate_platform_policy_conflict(
        lambda: platform_module.build_dispatch_plan_binding(
            lottery=lottery,
            task_mode=task_mode,
            account=account,
            selector_config=platform_selectors,
            stored_execution_path=stored_execution_path,
            weibo_rip=weibo_rip,
        )
    )
    if module_plan_binding is not None:
        plan_binding = module_plan_binding
        action_plan = plan_binding["action_plan"]
    execution_evidence_id = (
        str(decision_gate.get("execution_evidence_id") or "").strip()
        if task_mode == "real_run"
        else ""
    )
    if (
        task_mode == "real_run"
        and platform_module.requires_exact_real_run_evidence
        and not execution_evidence_id
    ):
        raise HTTPException(
            409,
            detail={
                "message": "Real-run policy decision has no exact execution evidence binding",
                "blockers": ["exact_execution_evidence_required"],
            },
        )
    # Atomic dispatch (P1-2): task_runs row, lottery claim, and the queued
    # stream message (as an outbox row) commit together. The append-only account
    # lease fences this account across every task type.
    execution_intent_binding = None
    async with database.transaction():
        locked = await database.fetch_one(
            """SELECT status, execution_lock, platform, raw_url, canonical_url,
                      rule_text, action_plan, authoritative_rule_snapshot_id,
                      rule_hash, action_plan_hash
               FROM lotteries WHERE id = :id FOR UPDATE""",
            {"id": lottery_id},
        )
        require_dispatchable_lottery_state(locked)
        require_dispatch_snapshot_unchanged(locked, lottery)
        try:
            # Lock and lease the account before exact evidence is revalidated.
            # This closes the account-revision gap between evidence validation
            # and task/outbox persistence; a later validation failure rolls
            # the lease insert back with this transaction.
            account_lease = await acquire_account_operation_lease(
                int(account["id"]),
                operation_kind=(
                    lease_operation_kind_for_execution_intent(
                        FULL_EXECUTION_INTENT_KIND
                    )
                    if task_mode == "real_run"
                    else task_mode
                ),
                owner_id=task_id,
                expected_execution_revision=int(account["execution_revision"] or 0),
                expected_platform=str(lottery["platform"]),
                db=database,
            )
        except AccountOperationLeaseConflict as exc:
            raise HTTPException(
                409,
                detail={
                    "message": (
                        "Account changed during dispatch preflight"
                        if exc.code == "account_operation_account_changed"
                        else "Account is already leased by another operation"
                    ),
                    "account_id": exc.account_id,
                    "code": exc.code,
                },
            ) from exc
        if task_mode == "real_run":
            completion_authority = await load_real_run_completion_authority(
                lottery_id,
                lottery["platform"],
                for_update=True,
            )
            require_completion_authority_settled(completion_authority)
            completed_actions = list(
                completion_authority.completed_actions
            )
            require_no_completed_actions_for_full_real_dispatch(completed_actions)
            try:
                await platform_module.revalidate_exact_execution_evidence(
                    lottery=lottery,
                    lottery_id=lottery_id,
                    account=account,
                    plan_binding=plan_binding,
                    execution_evidence_id=execution_evidence_id,
                )
            except PlatformPolicyConflict as exc:
                raise HTTPException(
                    exc.status_code,
                    detail=exc.detail,
                ) from exc
        await database.execute(
            """INSERT INTO task_runs
                 (task_id, account_id, lottery_id, status, dry_run, task_mode,
                  decision_id, policy_version, rule_snapshot_id, rule_hash,
                  action_plan_hash, execution_evidence_id, execution_path_id,
                  target_hash, config_hash,
                  account_lease_id, account_lease_generation,
                  reconciliation_required)
               VALUES
                 (:task_id, :account_id, :lottery_id, 'queued', :dry_run, :task_mode,
                  :decision_id, :policy_version, :rule_snapshot_id, :rule_hash,
                  :action_plan_hash, :execution_evidence_id, :execution_path_id,
                  :target_hash, :config_hash,
                  :account_lease_id, :account_lease_generation, 0)""",
            {
                "task_id": task_id,
                "account_id": account["id"],
                "lottery_id": lottery_id,
                "dry_run": int(dry_run),
                "task_mode": task_mode,
                "decision_id": decision_id,
                "policy_version": policy_version,
                "rule_snapshot_id": plan_binding["rule_snapshot_id"],
                "rule_hash": plan_binding["rule_hash"],
                "action_plan_hash": plan_binding["action_plan_hash"],
                "execution_evidence_id": execution_evidence_id or None,
                "execution_path_id": plan_binding["execution_path_id"],
                "target_hash": plan_binding["target_hash"],
                "config_hash": plan_binding["config_hash"],
                "account_lease_id": account_lease.lease_id,
                "account_lease_generation": account_lease.generation,
            },
        )
        if task_mode == "real_run":
            try:
                execution_intent_binding = await persist_full_execution_intent(
                    database,
                    lottery=dict(lottery),
                    task_id=task_id,
                    account_id=int(account["id"]),
                    plan_binding=plan_binding,
                    execution_evidence_id=execution_evidence_id,
                    account_lease_id=account_lease.lease_id,
                    account_lease_generation=account_lease.generation,
                    # This call is reached only after the locked completion
                    # authority proved zero completed actions and no
                    # unsettled/unknown external-action intent.  That proof
                    # authorizes the newly reviewed complete business intent
                    # to supersede the current head (including account, plan,
                    # rule, or target changes) while preserving every
                    # historical immutable root.
                    allow_current_intent_supersede=True,
                )
            except ExecutionIntentError as exc:
                raise HTTPException(
                    409,
                    detail={
                        "message": (
                            "Full execution intent could not be durably bound"
                        ),
                        "blockers": [exc.code],
                    },
                ) from exc
        await bind_lease_to_task(account_lease, task_id, db=database)
        execution_intent_message = (
            execution_intent_binding.message_fields()
            if execution_intent_binding is not None
            else {}
        )
        message = build_lottery_task_message(
            task_id=task_id,
            account_id=account["id"],
            lottery_id=lottery_id,
            platform=lottery["platform"],
            raw_url=lottery["raw_url"],
            canonical_url=lottery["canonical_url"],
            task_mode=task_mode,
            dry_run=dry_run,
            platform_selectors=platform_selectors,
            action_plan=action_plan,
            rule_snapshot_id=plan_binding["rule_snapshot_id"],
            rule_hash=plan_binding["rule_hash"],
            action_plan_hash=plan_binding["action_plan_hash"],
            execution_evidence_id=execution_evidence_id,
            execution_path_id=plan_binding["execution_path_id"],
            target_hash=plan_binding["target_hash"],
            config_hash=plan_binding["config_hash"],
            execution_revision=plan_binding["execution_revision"],
            account_lease_id=account_lease.lease_id,
            account_lease_generation=account_lease.generation,
            **execution_intent_message,
            weibo_rip_encrypted=encrypt_weibo_rip(weibo_rip),
        )
        await database.execute(
            "UPDATE lotteries SET status = 'claimed', execution_lock = :task_id, locked_at = NOW() WHERE id = :id",
            {"task_id": task_id, "id": lottery_id},
        )
        await enqueue_outbox(
            message,
            task_stream_for_platform(lottery["platform"]),
            dedup_key=task_id,
        )
        if task_mode == "real_run":
            await audit_event(
                request,
                action="lottery.dispatch.real",
                resource_type="lottery",
                resource_id=lottery_id,
                result="queued",
                risk_level="critical",
                detail={
                    "platform": lottery["platform"],
                    "task_id": task_id,
                    "account_id": account["id"],
                    "decision_id": decision_id,
                    "policy_version": policy_version,
                    "execution_evidence_id": execution_evidence_id,
                    "rule_snapshot_id": plan_binding["rule_snapshot_id"],
                    "action_plan_hash": plan_binding["action_plan_hash"],
                    "config_hash": plan_binding["config_hash"],
                    "execution_revision": plan_binding["execution_revision"],
                    "account_lease_id": account_lease.lease_id,
                    "account_lease_generation": account_lease.generation,
                },
            )
        elif task_mode == "shadow_run":
            await audit_event(
                request,
                action="lottery.dispatch.shadow",
                resource_type="lottery",
                resource_id=lottery_id,
                result="queued",
                risk_level="medium",
                detail={
                    "platform": lottery["platform"],
                    "task_id": task_id,
                    "account_id": account["id"],
                    "rule_snapshot_id": plan_binding["rule_snapshot_id"],
                    "action_plan_hash": plan_binding["action_plan_hash"],
                    "config_hash": plan_binding["config_hash"],
                    "execution_revision": plan_binding["execution_revision"],
                    "account_lease_id": account_lease.lease_id,
                    "account_lease_generation": account_lease.generation,
                },
            )
    # Best-effort immediate relay so dispatch latency stays low; if Redis is
    # momentarily unavailable the outbox dispatcher loop retries the committed row.
    try:
        await try_flush_dedup(task_id)
    except Exception as exc:
        structured_log("warning", "dispatch_immediate_flush_failed", task_id=task_id, error=str(exc))
    await _record_post_commit_event(
        aggregate="task",
        aggregate_id=task_id,
        event_type="TaskDispatched",
        payload={
            "platform": lottery["platform"],
            "account_id": account["id"],
            "lottery_id": lottery_id,
            "dry_run": dry_run,
            "mode": task_mode,
            "raw_url": lottery["raw_url"],
            "action_plan": action_plan,
            "decision_id": decision_id,
            "policy_version": policy_version,
            "rule_snapshot_id": plan_binding["rule_snapshot_id"],
            "rule_hash": plan_binding["rule_hash"],
            "action_plan_hash": plan_binding["action_plan_hash"],
            "execution_evidence_id": execution_evidence_id or None,
            "execution_path_id": plan_binding["execution_path_id"],
            "target_hash": plan_binding["target_hash"],
            "config_hash": plan_binding["config_hash"],
            "execution_revision": plan_binding["execution_revision"],
            "account_lease_id": account_lease.lease_id,
            "account_lease_generation": account_lease.generation,
        },
        correlation_id=task_id,
        actor_type="operator",
        actor_id=actor["actor_id"],
        critical=(task_mode == "real_run"),
    )
    await _record_post_commit_event(
        aggregate="lottery",
        aggregate_id=lottery_id,
        event_type="LotteryDispatchQueued",
        payload={
            "task_id": task_id,
            "account_id": account["id"],
            "dry_run": dry_run,
            "mode": task_mode,
            "execution_evidence_id": execution_evidence_id or None,
            "config_hash": plan_binding["config_hash"],
            "execution_revision": plan_binding["execution_revision"],
            "account_lease_id": account_lease.lease_id,
            "account_lease_generation": account_lease.generation,
        },
        correlation_id=task_id,
        actor_type="operator",
        actor_id=actor["actor_id"],
        critical=(task_mode == "real_run"),
    )
    return {
        "status": "queued",
        "task_id": task_id,
        "account_id": account["id"],
        "lottery_id": lottery_id,
        "mode": task_mode,
        "decision_id": decision_id,
        "policy_version": policy_version,
        "execution_evidence_id": execution_evidence_id or None,
        "config_hash": plan_binding["config_hash"],
        "execution_revision": plan_binding["execution_revision"],
        "account_lease_id": account_lease.lease_id,
        "account_lease_generation": account_lease.generation,
        "execution_intent_id": (
            execution_intent_binding.intent_id
            if execution_intent_binding is not None
            else None
        ),
        "execution_intent_hash": (
            execution_intent_binding.intent_hash
            if execution_intent_binding is not None
            else None
        ),
        "execution_intent_binding_hash": (
            execution_intent_binding.binding_hash
            if execution_intent_binding is not None
            else None
        ),
    }


@router.get("/{lottery_id}/repair-plan")
async def get_lottery_repair_plan(lottery_id: int, request: Request):
    require_min_role(request, "viewer")
    lottery = await database.fetch_one("SELECT * FROM lotteries WHERE id = :id", {"id": lottery_id})
    if not lottery:
        raise HTTPException(404, detail="Lottery not found")
    repair_workers_ready = bool(
        REPAIR_DISPATCH_INTENT_BINDING_READY
        and await repair_dispatch_workers_ready(lottery["platform"])
    )
    return {
        "lottery_id": lottery_id,
        "platform": lottery["platform"],
        "repair_plan": await build_lottery_repair_plan(
            lottery,
            dispatch_runtime_ready=repair_workers_ready,
        ),
    }


async def _record_repair_rejection(
    request: Request,
    *,
    actor_id: str,
    lottery_id: int,
    platform: str,
    code: str,
    http_status: int,
) -> None:
    """Record a minimal structured denial without persisting request details."""

    detail = {
        "platform": platform,
        "code": code,
        "http_status": http_status,
    }
    await audit_event(
        request,
        action="lottery.dispatch.repair",
        resource_type="lottery",
        resource_id=lottery_id,
        result="blocked",
        risk_level="critical",
        detail=detail,
    )
    await _record_post_commit_event(
        aggregate="lottery",
        aggregate_id=lottery_id,
        event_type="LotteryRepairDenied",
        payload=detail,
        actor_type="operator",
        actor_id=actor_id,
        critical=True,
    )


@router.post("/{lottery_id}/repair-dispatch")
async def dispatch_lottery_repair(lottery_id: int, data: DispatchTaskRequest, request: Request):
    actor = require_min_role(request, "operator")
    lottery = await database.fetch_one("SELECT * FROM lotteries WHERE id = :id", {"id": lottery_id})
    if not lottery:
        raise HTTPException(404, detail="Lottery not found")

    repair_workers_ready = bool(
        REPAIR_DISPATCH_INTENT_BINDING_READY
        and await repair_dispatch_workers_ready(lottery["platform"])
    )
    repair_plan = await build_lottery_repair_plan(
        lottery,
        dispatch_runtime_ready=repair_workers_ready,
    )
    if not repair_plan["eligible"]:
        await _record_repair_rejection(
            request,
            actor_id=actor["actor_id"],
            lottery_id=lottery_id,
            platform=lottery["platform"],
            code="repair_plan_not_eligible",
            http_status=409,
        )
        raise HTTPException(409, detail={"message": "Lottery has no safe missing-action repair plan", "repair_plan": repair_plan})
    if not REPAIR_DISPATCH_INTENT_BINDING_READY:
        await _record_repair_rejection(
            request,
            actor_id=actor["actor_id"],
            lottery_id=lottery_id,
            platform=lottery["platform"],
            code="repair_intent_binding_unavailable",
            http_status=503,
        )
        raise HTTPException(
            503,
            detail={
                "code": REPAIR_DISPATCH_BLOCKER,
                "message": (
                    "Repair dispatch is blocked until Worker validates the "
                    "frozen full intent and executes only the exact subset"
                ),
                "repair_plan": repair_plan,
            },
        )
    if not repair_workers_ready:
        await _record_repair_rejection(
            request,
            actor_id=actor["actor_id"],
            lottery_id=lottery_id,
            platform=lottery["platform"],
            code="repair_worker_contract_unavailable",
            http_status=503,
        )
        raise HTTPException(
            503,
            detail={
                "code": REPAIR_DISPATCH_BLOCKER,
                "message": (
                    "Repair dispatch requires a capable, fresh Worker serving "
                    "this platform's exact repair lane"
                ),
                "repair_plan": repair_plan,
            },
        )
    try:
        frozen_intent = await load_lottery_execution_intent(
            database,
            lottery_id,
        )
        if frozen_intent is None:
            raise ExecutionIntentError("execution_intent_missing")
        validate_lottery_execution_intent_binding(
            frozen_intent,
            dict(lottery),
        )
    except ExecutionIntentError as exc:
        await _record_repair_rejection(
            request,
            actor_id=actor["actor_id"],
            lottery_id=lottery_id,
            platform=lottery["platform"],
            code=exc.code,
            http_status=409,
        )
        raise HTTPException(
            409,
            detail={
                "message": "Frozen execution intent is not repairable",
                "blockers": [exc.code],
                "repair_plan": repair_plan,
            },
        ) from exc
    if (
        data.account_id is not None
        and int(data.account_id) != frozen_intent.source_account_id
    ):
        await _record_repair_rejection(
            request,
            actor_id=actor["actor_id"],
            lottery_id=lottery_id,
            platform=lottery["platform"],
            code="execution_intent_repair_account_mismatch",
            http_status=409,
        )
        raise HTTPException(
            409,
            detail={
                "message": "Repair must use the frozen source account",
                "blockers": [
                    "execution_intent_repair_account_mismatch"
                ],
            },
        )

    platform_module = get_platform_module(lottery["platform"])
    platform_cfg = get_platform(lottery["platform"])
    if platform_module is None or not platform_cfg:
        raise HTTPException(400, detail=f"Unsupported platform: {lottery['platform']}")

    selector_config = await load_runtime_selector_config()
    platform_selectors = selector_config.get(lottery["platform"], {})
    real_adapter_enabled = platform_cfg.get("action_adapter", False) or platform_has_runtime_real_adapter(selector_config, lottery["platform"])
    task_mode = "real_run"
    dry_run = False
    repair_stored_plan = frozen_intent.full_action_plan
    repair_stored_execution_path = frozen_intent.execution_path_id
    requested_actions = tuple(repair_plan["missing_actions"])
    weibo_rip = ""
    target = validate_lottery_target(lottery["platform"], lottery["raw_url"])
    if not target.valid:
        raise HTTPException(400, detail=target.reason)

    require_min_role(request, "admin")
    try:
        if not platform_module.real_run_supported:
            raise HTTPException(
                409,
                detail={
                    "message": (
                        f"{platform_cfg.get('label', lottery['platform'])} "
                        "does not support real-run repair"
                    ),
                    "blockers": [platform_module.real_run_blocker],
                },
            )
        path_blockers = platform_module.execution_path_blockers(
            repair_stored_execution_path
        )
        if path_blockers:
            raise HTTPException(
                409,
                detail={
                    "message": "Repair execution path is not authorized",
                    "blockers": path_blockers,
                },
            )
        if platform_module.requires_public_ingress(
            required_actions=set(requested_actions)
        ):
            weibo_rip = trusted_weibo_rip(request)
        require_confirmation(request)
        if not data.confirm:
            raise HTTPException(409, detail="Repair real-run body confirmation required")
        if not await is_real_run_enabled():
            raise HTTPException(403, detail="Global real-run switch is disabled")
        breaker_allowed, breaker_reason = await circuit_breaker_allows(lottery["platform"])
        if not breaker_allowed:
            raise HTTPException(423, detail=f"Circuit breaker blocks repair-run: {breaker_reason}")
        if not real_adapter_enabled:
            raise HTTPException(400, detail=f"Real actions for {lottery['platform']} are not implemented yet.")
    except HTTPException as exc:
        rejection_code = (
            str(exc.detail.get("code") or "").strip()
            if isinstance(exc.detail, dict)
            else ""
        ) or f"repair_preflight_http_{exc.status_code}"
        await _record_repair_rejection(
            request,
            actor_id=actor["actor_id"],
            lottery_id=lottery_id,
            platform=lottery["platform"],
            code=rejection_code,
            http_status=exc.status_code,
        )
        await emit_real_run_gate_notification(lottery, exc.detail, actor_id=actor["actor_id"])
        raise

    account = await pick_account(
        frozen_intent.source_account_id,
        lottery["platform"],
        execution_path_id=platform_module.account_execution_path_for_dispatch(
            task_mode="real_run",
            stored_execution_path=repair_stored_execution_path,
            operation_kind="repair",
        ),
        required_actions=platform_module.account_required_actions_for_dispatch(
            required_actions=requested_actions,
            task_mode="real_run",
            operation_kind="repair",
        ),
        require_account_capability=True,
    )
    if (
        not account
        or int(account["id"]) != frozen_intent.source_account_id
    ):
        await _record_repair_rejection(
            request,
            actor_id=actor["actor_id"],
            lottery_id=lottery_id,
            platform=lottery["platform"],
            code="repair_source_account_unavailable",
            http_status=409,
        )
        raise HTTPException(
            409,
            detail={
                "message": "Frozen source account is not available for repair",
                "blockers": ["repair_source_account_unavailable"],
            },
        )

    decision = await evaluate_real_run_decision(
        lottery,
        account_id=account["id"],
        execution_required_actions=requested_actions,
        record=True,
    )
    decision_id = decision["decision_id"]
    policy_version = decision["policy_version"]
    decision_gate = dict(decision.get("gate") or {})
    if not decision["allowed"]:
        await _record_post_commit_event(
            aggregate="lottery",
            aggregate_id=lottery_id,
            event_type="LotteryRepairDenied",
            payload={
                "platform": lottery["platform"],
                "account_id": account["id"],
                "blockers": decision["blockers"],
                "failed_gates": decision["failed_gates"],
                "decision_id": decision_id,
                "policy_version": policy_version,
                "repair_plan": repair_plan,
            },
            actor_type="operator",
            actor_id=actor["actor_id"],
            critical=True,
        )
        raise HTTPException(
            409,
            detail={
                "message": "Repair policy gate is not satisfied",
                "blockers": decision["blockers"],
                "failed_gates": decision["failed_gates"],
                "decision_id": decision_id,
                "policy_version": policy_version,
                "repair_plan": repair_plan,
            },
        )

    plan_binding = _translate_platform_policy_conflict(
        lambda: platform_module.build_dispatch_plan_binding(
            lottery=lottery,
            task_mode="real_run",
            account=account,
            selector_config=platform_selectors,
            stored_execution_path=repair_stored_execution_path,
            weibo_rip=weibo_rip,
            execution_required_actions=requested_actions,
        )
    )
    if plan_binding is None or (
        plan_binding.get("rule_snapshot_id") != frozen_intent.rule_snapshot_id
        or plan_binding.get("rule_hash") != frozen_intent.rule_hash
        or plan_binding.get("action_plan_hash")
        != frozen_intent.full_action_plan_hash
        or plan_binding.get("execution_path_id")
        != frozen_intent.execution_path_id
        or plan_binding.get("target_hash") != frozen_intent.target_hash
        or plan_binding.get("required_actions")
        != frozen_intent.full_required_actions
        or plan_binding.get("action_plan")
        != frozen_intent.full_action_plan
    ):
        raise HTTPException(
            409,
            detail={
                "message": "Repair full-plan binding changed",
                "blockers": ["execution_intent_full_binding_mismatch"],
                "repair_plan": repair_plan,
            },
        )
    execution_evidence_id = str(
        decision_gate.get("execution_evidence_id") or ""
    ).strip()
    if (
        platform_module.requires_exact_real_run_evidence
        and not execution_evidence_id
    ):
        raise HTTPException(
            409,
            detail={
                "message": (
                    "Repair policy decision has no exact full-plan evidence"
                ),
                "blockers": ["exact_execution_evidence_required"],
                "repair_plan": repair_plan,
            },
        )

    task_id = str(uuid.uuid4())
    repair_action_plan = repair_plan["repair_action_plan"]

    async with database.transaction():
        locked = await database.fetch_one(
            """SELECT id, status, execution_lock, platform, raw_url,
                      canonical_url, rule_text, action_plan,
                      authoritative_rule_snapshot_id, rule_hash,
                      action_plan_hash
               FROM lotteries WHERE id = :id FOR UPDATE""",
            {"id": lottery_id},
        )
        require_dispatchable_lottery_state(locked, repair=True)
        require_dispatch_snapshot_unchanged(locked, lottery)
        locked_intent = await load_lottery_execution_intent(
            database,
            lottery_id,
            for_update=True,
        )
        if locked_intent is None:
            raise HTTPException(
                409,
                detail={
                    "message": "Frozen execution intent disappeared",
                    "blockers": ["execution_intent_missing"],
                },
            )
        try:
            validate_lottery_execution_intent_binding(
                locked_intent,
                dict(locked),
            )
            account_lease = await acquire_account_operation_lease(
                int(account["id"]),
                operation_kind=(
                    lease_operation_kind_for_execution_intent(
                        REPAIR_EXECUTION_INTENT_KIND
                    )
                ),
                owner_id=task_id,
                expected_execution_revision=int(
                    account["execution_revision"] or 0
                ),
                expected_platform=str(lottery["platform"]),
                db=database,
            )
        except ExecutionIntentError as exc:
            raise HTTPException(
                409,
                detail={
                    "message": "Frozen execution intent changed",
                    "blockers": [exc.code],
                },
            ) from exc
        except AccountOperationLeaseConflict as exc:
            raise HTTPException(
                409,
                detail={
                    "message": (
                        "Account changed during repair preflight"
                        if exc.code == "account_operation_account_changed"
                        else "Account is already leased by another operation"
                    ),
                    "account_id": exc.account_id,
                    "code": exc.code,
                },
            ) from exc
        locked_completion_authority = (
            await load_real_run_completion_authority(
                lottery_id,
                lottery["platform"],
                for_update=True,
                execution_intent=locked_intent,
            )
        )
        require_completion_authority_settled(
            locked_completion_authority
        )
        current_repair_plan = await build_lottery_repair_plan(
            locked,
            completion_authority=locked_completion_authority,
            execution_intent=locked_intent,
            dispatch_runtime_ready=repair_workers_ready,
        )
        require_repair_plan_unchanged(current_repair_plan, repair_plan)
        try:
            await platform_module.revalidate_exact_execution_evidence(
                lottery=locked,
                lottery_id=lottery_id,
                account=account,
                plan_binding=plan_binding,
                execution_evidence_id=execution_evidence_id,
                execution_required_actions=requested_actions,
            )
        except PlatformPolicyConflict as exc:
            raise HTTPException(
                exc.status_code,
                detail=exc.detail,
            ) from exc
        await database.execute(
            """INSERT INTO task_runs
                 (task_id, account_id, lottery_id, status, dry_run, task_mode,
                  decision_id, policy_version, rule_snapshot_id, rule_hash,
                  action_plan_hash, execution_evidence_id, execution_path_id,
                  target_hash, config_hash, account_lease_id,
                  account_lease_generation, reconciliation_required)
               VALUES
                 (:task_id, :account_id, :lottery_id, 'queued', :dry_run,
                  :task_mode, :decision_id, :policy_version,
                  :rule_snapshot_id, :rule_hash, :action_plan_hash,
                  :execution_evidence_id, :execution_path_id, :target_hash,
                  :config_hash, :account_lease_id,
                  :account_lease_generation, 0)""",
            {
                "task_id": task_id,
                "account_id": account["id"],
                "lottery_id": lottery_id,
                "dry_run": int(dry_run),
                "task_mode": task_mode,
                "decision_id": decision_id,
                "policy_version": policy_version,
                # task_runs/evidence remain authoritative for the frozen full
                # plan. The exact repair subset is stored separately below.
                "rule_snapshot_id": locked_intent.rule_snapshot_id,
                "rule_hash": locked_intent.rule_hash,
                "action_plan_hash": locked_intent.full_action_plan_hash,
                "execution_evidence_id": execution_evidence_id,
                "execution_path_id": locked_intent.execution_path_id,
                "target_hash": locked_intent.target_hash,
                "config_hash": plan_binding["config_hash"],
                "account_lease_id": account_lease.lease_id,
                "account_lease_generation": account_lease.generation,
            },
        )
        try:
            repair_binding = await persist_repair_execution_binding(
                database,
                intent=locked_intent,
                task_id=task_id,
                account_id=int(account["id"]),
                requested_actions=current_repair_plan["missing_actions"],
                execution_evidence_id=execution_evidence_id,
                config_hash=plan_binding["config_hash"],
                execution_revision=plan_binding["execution_revision"],
                account_lease_id=account_lease.lease_id,
                account_lease_generation=account_lease.generation,
            )
        except ExecutionIntentError as exc:
            raise HTTPException(
                409,
                detail={
                    "message": (
                        "Exact repair subset could not be durably bound"
                    ),
                    "blockers": [exc.code],
                },
            ) from exc
        await bind_lease_to_task(account_lease, task_id, db=database)
        message = build_lottery_task_message(
            task_id=task_id,
            account_id=account["id"],
            lottery_id=lottery_id,
            platform=lottery["platform"],
            raw_url=locked_intent.raw_url,
            canonical_url=locked_intent.canonical_url,
            task_mode=task_mode,
            dry_run=dry_run,
            platform_selectors=platform_selectors,
            # The queue's legacy plan fields stay full/evidence-bound. Worker
            # may execute only the separately bound requested_actions subset.
            action_plan=locked_intent.full_action_plan,
            rule_snapshot_id=locked_intent.rule_snapshot_id,
            rule_hash=locked_intent.rule_hash,
            action_plan_hash=locked_intent.full_action_plan_hash,
            execution_evidence_id=execution_evidence_id,
            execution_path_id=locked_intent.execution_path_id,
            target_hash=locked_intent.target_hash,
            config_hash=plan_binding["config_hash"],
            execution_revision=plan_binding["execution_revision"],
            account_lease_id=account_lease.lease_id,
            account_lease_generation=account_lease.generation,
            **repair_binding.message_fields(),
            weibo_rip_encrypted=encrypt_weibo_rip(weibo_rip),
        )
        await database.execute(
            "UPDATE lotteries SET status = 'claimed', execution_lock = :task_id, locked_at = NOW() WHERE id = :id",
            {"task_id": task_id, "id": lottery_id},
        )
        await enqueue_outbox(
            message,
            repair_task_stream_for_platform(lottery["platform"]),
            dedup_key=task_id,
        )
        await audit_event(
            request,
            action="lottery.dispatch.repair",
            resource_type="lottery",
            resource_id=lottery_id,
            result="queued",
            risk_level="critical",
            detail={
                "platform": lottery["platform"],
                "task_id": task_id,
                "account_id": account["id"],
                "decision_id": decision_id,
                "policy_version": policy_version,
                "execution_intent_id": repair_binding.intent_id,
                "execution_intent_hash": repair_binding.intent_hash,
                "execution_intent_binding_hash": repair_binding.binding_hash,
                "requested_actions_hash": (
                    repair_binding.requested_actions_hash
                ),
                "requested_action_plan_hash": (
                    repair_binding.bound_action_plan_hash
                ),
                "repair_plan": repair_plan,
            },
        )

    try:
        await try_flush_dedup(task_id)
    except Exception as exc:
        structured_log("warning", "repair_dispatch_immediate_flush_failed", task_id=task_id, error=str(exc))

    await _record_post_commit_event(
        aggregate="task",
        aggregate_id=task_id,
        event_type="TaskDispatched",
        payload={
            "platform": lottery["platform"],
            "account_id": account["id"],
            "lottery_id": lottery_id,
            "dry_run": dry_run,
            "mode": task_mode,
            "raw_url": lottery["raw_url"],
            "action_plan": frozen_intent.full_action_plan,
            "action_plan_hash": frozen_intent.full_action_plan_hash,
            "repair_action_plan": repair_action_plan,
            "repair_plan": repair_plan,
            "decision_id": decision_id,
            "policy_version": policy_version,
            "execution_evidence_id": execution_evidence_id,
            "execution_intent_id": repair_binding.intent_id,
            "execution_intent_hash": repair_binding.intent_hash,
            "execution_intent_binding_hash": repair_binding.binding_hash,
            "requested_actions": list(repair_binding.requested_actions),
            "requested_actions_hash": repair_binding.requested_actions_hash,
            "requested_action_plan_hash": (
                repair_binding.bound_action_plan_hash
            ),
            "account_lease_id": account_lease.lease_id,
            "account_lease_generation": account_lease.generation,
        },
        correlation_id=task_id,
        actor_type="operator",
        actor_id=actor["actor_id"],
        critical=True,
    )
    await _record_post_commit_event(
        aggregate="lottery",
        aggregate_id=lottery_id,
        event_type="LotteryRepairQueued",
        payload={
            "task_id": task_id,
            "account_id": account["id"],
            "mode": task_mode,
            "repair_plan": repair_plan,
            "execution_intent_id": repair_binding.intent_id,
            "execution_intent_binding_hash": repair_binding.binding_hash,
            "requested_actions": list(repair_binding.requested_actions),
        },
        correlation_id=task_id,
        actor_type="operator",
        actor_id=actor["actor_id"],
        critical=True,
    )
    return {
        "status": "queued",
        "task_id": task_id,
        "account_id": account["id"],
        "lottery_id": lottery_id,
        "mode": task_mode,
        "repair_plan": repair_plan,
        "decision_id": decision_id,
        "policy_version": policy_version,
        "execution_evidence_id": execution_evidence_id,
        "execution_intent_id": repair_binding.intent_id,
        "execution_intent_hash": repair_binding.intent_hash,
        "execution_intent_binding_hash": repair_binding.binding_hash,
        "requested_actions": list(repair_binding.requested_actions),
        "requested_actions_hash": repair_binding.requested_actions_hash,
        "requested_action_plan_hash": repair_binding.bound_action_plan_hash,
        "account_lease_id": account_lease.lease_id,
        "account_lease_generation": account_lease.generation,
    }


@router.get("/{lottery_id}/action-plan/suggest")
async def suggest_lottery_action_plan(lottery_id: int, request: Request, rule_text: str | None = None):
    require_min_role(request, "viewer")
    lottery = await database.fetch_one("SELECT id, platform, rule_text FROM lotteries WHERE id = :id", {"id": lottery_id})
    if not lottery:
        raise HTTPException(404, detail="Lottery not found")

    text = rule_text if rule_text is not None else (lottery["rule_text"] or "")
    plan = parse_lottery_rule(text, lottery["platform"])
    return {"lottery_id": lottery_id, "platform": lottery["platform"], "rule_text": text, "suggested_action_plan": plan}


@router.get("/{lottery_id}/rule-hydration")
async def get_lottery_rule_hydration(lottery_id: int, request: Request):
    """Fetch bounded source text and author identity without platform writes."""

    require_min_role(request, "viewer")
    lottery = await database.fetch_one(
        "SELECT * FROM lotteries WHERE id = :id",
        {"id": lottery_id},
    )
    if not lottery:
        raise HTTPException(404, detail="Lottery not found")
    try:
        return await hydrate_lottery_rule(dict(lottery))
    except LotteryRuleHydrationError as exc:
        raise HTTPException(
            503 if exc.retryable else 409,
            detail={"code": exc.code, "retryable": exc.retryable},
        ) from exc


@router.put("/{lottery_id}/action-plan")
async def update_lottery_action_plan(lottery_id: int, data: LotteryActionPlanUpdate, request: Request):
    actor = require_min_role(request, "operator")
    submitted_actions = [str(action).strip() for action in data.required_actions]
    async with database.transaction():
        lottery = await database.fetch_one(
            """SELECT l.id, l.platform, l.source_type, l.source_id,
                      l.raw_url, l.canonical_url, l.rule_text, l.status,
                      l.execution_lock,
                      head.current_intent_id
               FROM lotteries AS l
               LEFT JOIN lottery_execution_intent_heads AS head
                 ON head.lottery_id = l.id
               WHERE l.id = :id
               FOR UPDATE""",
            {"id": lottery_id},
        )
        if not lottery:
            raise HTTPException(404, detail="Lottery not found")
        require_lottery_not_executing(lottery, operation="change its action plan")
        if str(dict(lottery).get("current_intent_id") or "").strip():
            current_intent = await load_lottery_execution_intent(
                database,
                lottery_id,
                for_update=True,
            )
            completion_authority = await load_real_run_completion_authority(
                lottery_id,
                str(lottery["platform"]),
                for_update=True,
                execution_intent=current_intent,
            )
            require_action_plan_mutation_safe(completion_authority)
        platform_action_order = action_order_for_platform(lottery["platform"])
        platform_action_set = frozenset(platform_action_order)
        invalid = [
            action for action in submitted_actions if action not in platform_action_set
        ]
        if invalid:
            raise HTTPException(400, detail={"message": "Unsupported lottery actions", "actions": invalid})
        if not submitted_actions:
            raise HTTPException(400, detail="At least one required action must be selected")
        if len(submitted_actions) != len(set(submitted_actions)):
            raise HTTPException(400, detail="Required actions must not contain duplicates")
        required_actions = [
            action for action in platform_action_order if action in set(submitted_actions)
        ]

        rule_text = protected_source_rule_text(lottery["rule_text"], data.rule_text)
        parsed_rule = parse_lottery_rule(rule_text, lottery["platform"])
        unsupported_actions = list(parsed_rule.get("unsupported_actions") or [])
        content_requirements = dict(
            parsed_rule.get("content_requirements")
            or {
                "follow_targets": [],
                "commented": {"topic_tags": [], "mentions": []},
                "reposted": {"topic_tags": [], "mentions": []},
            }
        )
        source_content_requirements = {
            "follow_targets": list(content_requirements.get("follow_targets") or []),
            "commented": dict(content_requirements.get("commented") or {}),
            "reposted": dict(content_requirements.get("reposted") or {}),
        }
        try:
            friend_mention_requirements = validate_friend_mention_requirements(
                parsed_rule.get("friend_mention_requirements", {})
            )
        except ActionPlanV2Error as exc:
            friend_mention_requirements = {}
            # A parser-produced invalid shape is a contract defect, never a
            # reason to silently discard an operator-visible source rule.
            payload_validation_errors = [exc.code]
        else:
            payload_validation_errors = []
        ambiguity_patterns = list(parsed_rule.get("ambiguity_patterns") or [])
        parsed_required_actions = {
            str(action)
            for action in (parsed_rule.get("required_actions") or [])
            if str(action)
        }
        selected_required_actions = set(required_actions)
        platform_module = get_platform_module(lottery["platform"])

        raw_payloads = dict(data.action_payloads or {})
        if set(raw_payloads) != selected_required_actions:
            payload_validation_errors.append("action_plan_payload_binding_mismatch")
        if set(raw_payloads) - platform_action_set:
            payload_validation_errors.append("action_plan_payload_unknown_action")
        action_payloads: dict[str, dict] = {}
        for action in required_actions:
            raw_payload = raw_payloads.get(action, {})
            try:
                action_payloads[action] = validate_action_payload(
                    action,
                    raw_payload,
                    allow_empty_repost_text=bool(
                        platform_module
                        and platform_module.allow_empty_repost_text
                    ),
                    platform=lottery["platform"],
                )
            except ActionPlanV2Error as exc:
                action_payloads[action] = dict(raw_payload) if isinstance(raw_payload, dict) else {}
                payload_validation_errors.append(exc.code)
        payload_validation_errors = list(dict.fromkeys(payload_validation_errors))

        if platform_module is not None:
            content_requirements, payload_validation_errors = (
                platform_module.apply_action_plan_authoring_policy(
                    required_actions=required_actions,
                    action_payloads=action_payloads,
                    content_requirements=content_requirements,
                    friend_mention_requirements=friend_mention_requirements,
                    source_content_requirements=source_content_requirements,
                    selected_required_actions=selected_required_actions,
                    payload_validation_errors=payload_validation_errors,
                )
            )

        represented_requirements, unresolved_requirements, capability_blockers = (
            semantic_requirement_status(
                unsupported_actions,
                action_payloads,
                content_requirements,
                friend_mention_requirements=friend_mention_requirements,
                source_content_requirements=source_content_requirements,
                media_capability_blocker=(
                    platform_module.media_submission_blocker
                    if platform_module
                    else "bilibili_media_submission_unsupported"
                ),
            )
        )
        execution_path_id = str(
            data.execution_path_id
            if "execution_path_id" in data.model_fields_set
            else default_execution_path_for_platform(lottery["platform"])
        ).strip()
        if platform_module is None:
            capability_blockers.append("platform_execution_path_not_bound")
        else:
            capability_blockers.extend(
                platform_module.execution_path_blockers(execution_path_id)
            )
        capability_blockers = list(dict.fromkeys(capability_blockers))
        runtime_capability_requirements = (
            platform_module.build_runtime_capability_requirements(
                tuple(required_actions),
                execution_path_id,
            )
            if platform_module is not None
            else None
        ) or {}

        snapshot = await ensure_rule_snapshot(
            dict(lottery),
            rule_text,
            complete=bool(data.rule_complete_confirmed),
            actor_id=actor["actor_id"],
            db=database,
        )
        semantic_review_blocked = bool(
            not parsed_rule.get("is_lottery")
            or parsed_required_actions != selected_required_actions
            or ambiguity_patterns
            or unresolved_requirements
            or payload_validation_errors
        )
        review_required = bool(
            not data.reviewed
            or not snapshot["is_complete"]
            or semantic_review_blocked
        )
        executable = bool(not review_required and not capability_blockers)
        plan = {
            "version": 2,
            "platform": lottery["platform"],
            "is_lottery": True,
            "required_actions": required_actions,
            "action_payloads": action_payloads,
            "content_requirements": content_requirements,
            "source_content_requirements": source_content_requirements,
            "friend_mention_requirements": friend_mention_requirements,
            "runtime_capability_requirements": runtime_capability_requirements,
            "execution_path_id": execution_path_id,
            "rule_snapshot_id": snapshot["id"],
            "rule_hash": snapshot["rule_hash"],
            "review_required": review_required,
            "executable": executable,
            "confidence": 1.0 if not review_required else 0.5,
            "source": "operator_complete_attestation" if snapshot["is_complete"] else "operator_draft",
            "reviewed_by": actor["actor_id"] if data.reviewed else None,
            "rule_complete_confirmed": bool(snapshot["is_complete"]),
            "unsupported_actions": unsupported_actions,
            "represented_requirements": represented_requirements,
            "unresolved_requirements": unresolved_requirements,
            "ambiguity_patterns": ambiguity_patterns,
            "payload_validation_errors": payload_validation_errors,
            "capability_blockers": capability_blockers,
        }
        plan["plan_hash"] = compute_action_plan_hash(plan)
        await database.execute(
            """UPDATE lotteries
               SET rule_text = :rule_text,
                   action_plan = :action_plan,
                   authoritative_rule_snapshot_id = :authoritative_rule_snapshot_id,
                   rule_hash = :rule_hash,
                   action_plan_hash = :action_plan_hash
               WHERE id = :id""",
            {
                "id": lottery_id,
                "rule_text": rule_text,
                "action_plan": json.dumps(plan, ensure_ascii=False),
                "authoritative_rule_snapshot_id": snapshot["id"] if snapshot["is_complete"] else None,
                "rule_hash": snapshot["rule_hash"],
                "action_plan_hash": plan["plan_hash"],
            },
        )
        # The high-risk review write and its audit record commit atomically.
        # Otherwise an audit failure returns 500 after the plan already changed,
        # making an operator retry ambiguous and leaving an unaudited approval.
        await audit_event(
            request,
            action="lottery.action_plan.update",
            resource_type="lottery",
            resource_id=lottery_id,
            result="saved",
            risk_level="high",
            detail={
                "platform": lottery["platform"],
                "required_actions": required_actions,
                "reviewed": data.reviewed,
                "rule_complete_confirmed": bool(snapshot["is_complete"]),
                "semantic_review_blocked": semantic_review_blocked,
                "unsupported_actions": unsupported_actions,
                "unresolved_requirements": unresolved_requirements,
                "payload_validation_errors": payload_validation_errors,
                "capability_blockers": capability_blockers,
                "rule_snapshot_id": snapshot["id"],
                "rule_hash": snapshot["rule_hash"],
                "action_plan_hash": plan["plan_hash"],
            },
        )
    await _record_post_commit_event(
        aggregate="lottery",
        aggregate_id=lottery_id,
        event_type="LotteryActionPlanReviewed" if not plan["review_required"] else "LotteryActionPlanUpdated",
        payload={
            "required_actions": required_actions,
            "reviewed": data.reviewed,
            "review_required": plan["review_required"],
            "executable": plan["executable"],
            "unsupported_actions": unsupported_actions,
            "represented_requirements": represented_requirements,
            "unresolved_requirements": unresolved_requirements,
            "capability_blockers": capability_blockers,
            "rule_snapshot_id": snapshot["id"],
            "rule_hash": snapshot["rule_hash"],
            "action_plan_hash": plan["plan_hash"],
            "rule_text": rule_text,
        },
        actor_type="operator",
        actor_id=actor["actor_id"],
    )
    return {"status": "saved", "lottery_id": lottery_id, "action_plan": plan}


@router.get("/tasks/runs")
async def list_task_runs(limit: int = 50):
    limit = clamp_limit(limit)
    rows = await database.fetch_all(
        """SELECT tr.*, l.raw_url, l.platform
           FROM task_runs tr
           JOIN lotteries l ON tr.lottery_id = l.id
           ORDER BY tr.id DESC LIMIT :limit""",
        {"limit": limit},
    )
    return [dict(r) for r in rows]


@router.get("/probes")
async def list_adapter_probes(limit: int = 50):
    limit = clamp_limit(limit)
    rows = await database.fetch_all(
        """SELECT ac.*, l.raw_url
           FROM adapter_calibrations ac
           LEFT JOIN lotteries l ON ac.lottery_id = l.id
           ORDER BY ac.id DESC LIMIT :limit""",
        {"limit": limit},
    )
    result = []
    for row in rows:
        item = dict(row)
        item["result"] = parse_json_field(item.get("result"))
        result.append(item)
    return result


@router.post("/{lottery_id}/probe")
async def probe_lottery_adapter(lottery_id: int, data: AdapterProbeRequest, request: Request):
    actor = require_min_role(request, "operator")
    lottery = await database.fetch_one("SELECT * FROM lotteries WHERE id = :id", {"id": lottery_id})
    if not lottery:
        raise HTTPException(404, detail="Lottery not found")
    platform_module = get_platform_module(lottery["platform"])
    if platform_module is None or not get_platform(lottery["platform"]):
        raise HTTPException(400, detail=f"Unsupported platform: {lottery['platform']}")
    target = validate_lottery_target(lottery["platform"], lottery["raw_url"])
    if not target.valid:
        raise HTTPException(400, detail=target.reason)

    account = await pick_account(
        data.account_id,
        lottery["platform"],
        execution_path_id=platform_module.account_execution_path_for_dispatch(
            task_mode="shadow_run",
            stored_execution_path=platform_module.default_execution_path_id,
        ),
        require_account_capability=False,
    )
    if not account:
        raise HTTPException(400, detail=f"No calibrated ready account is available for {lottery['platform']}")

    plan_binding = {
        "rule_snapshot_id": None,
        "rule_hash": None,
        "action_plan_hash": None,
        "execution_path_id": f"{lottery['platform']}_selector_v1",
        "execution_revision": int(account["execution_revision"] or 0),
    }
    runtime_selector_config = await load_runtime_selector_config()
    platform_selector_config = runtime_selector_config.get(lottery["platform"], {})
    if not isinstance(platform_selector_config, dict):
        platform_selector_config = {}
    config_hash = compute_config_hash(
        {
            "platform": lottery["platform"],
            "execution_revision": int(account["execution_revision"] or 0),
            "selector_config": platform_selector_config,
        }
    )
    if platform_module.probe_requires_plan_binding:
        plan_readiness = await validate_real_run_evidence(lottery, account_id=None)
        if not plan_readiness.get("action_plan_ready"):
            raise HTTPException(
                409,
                detail={
                    "message": platform_module.probe_plan_error_message,
                    "blockers": [
                        blocker
                        for blocker in plan_readiness.get("blockers", [])
                        if blocker not in platform_module.probe_ignored_blockers
                    ],
                },
            )
        bound_probe_plan = _translate_platform_policy_conflict(
            lambda: platform_module.build_dispatch_plan_binding(
                lottery=lottery,
                task_mode="shadow_run",
                account=account,
                selector_config=platform_selector_config,
                stored_execution_path=platform_module.default_execution_path_id,
            )
        )
        if bound_probe_plan is None:
            raise HTTPException(409, detail="Platform probe plan binding is unavailable")
        plan_binding = bound_probe_plan
        config_hash = plan_binding["config_hash"]

    probe_id = str(uuid.uuid4())
    target_url = validated_probe_navigation_url(lottery["raw_url"])
    target_hash = compute_target_hash(str(lottery["canonical_url"] or ""))
    message = {
        "probe_id": probe_id,
        "platform": lottery["platform"],
        "account_id": str(account["id"]),
        "lottery_id": str(lottery_id),
        "target_url": target_url,
        "canonical_url": str(lottery["canonical_url"] or ""),
        "execution_path_id": str(plan_binding["execution_path_id"] or ""),
        "target_hash": target_hash,
        "rule_snapshot_id": str(plan_binding["rule_snapshot_id"] or ""),
        "rule_hash": str(plan_binding["rule_hash"] or ""),
        "action_plan_hash": str(plan_binding["action_plan_hash"] or ""),
        "config_hash": config_hash,
        "execution_revision": str(plan_binding["execution_revision"] or ""),
    }
    outbox_key = f"adapter-probe:{probe_id}"
    # The canonical queued row and its stream intent commit together. Redis can
    # be temporarily unavailable without stranding a probe that can never be
    # reclaimed or safely retried.
    async with database.transaction():
        locked = await database.fetch_one(
            """SELECT id, platform, raw_url, canonical_url, rule_text, action_plan,
                      authoritative_rule_snapshot_id, rule_hash, action_plan_hash
               FROM lotteries WHERE id = :id FOR UPDATE""",
            {"id": lottery_id},
        )
        require_dispatch_snapshot_unchanged(locked, lottery)
        try:
            account_lease = await acquire_account_operation_lease(
                int(account["id"]),
                operation_kind="adapter_probe",
                owner_id=probe_id,
                expected_execution_revision=int(account["execution_revision"] or 0),
                expected_platform=str(lottery["platform"]),
                db=database,
            )
        except AccountOperationLeaseConflict as exc:
            raise HTTPException(
                409,
                detail={
                    "message": (
                        "Account changed during probe preflight"
                        if exc.code == "account_operation_account_changed"
                        else "Account is already leased by another operation"
                    ),
                    "account_id": exc.account_id,
                    "code": exc.code,
                },
            ) from exc
        message["account_lease_id"] = account_lease.lease_id
        message["account_lease_generation"] = str(account_lease.generation)
        await database.execute(
            """INSERT INTO adapter_calibrations
                 (probe_id, platform, account_id, lottery_id, target_url, status,
                  execution_path_id, target_hash, rule_snapshot_id, rule_hash,
                  action_plan_hash, config_hash, account_lease_id,
                  account_lease_generation)
               VALUES
                 (:probe_id, :platform, :account_id, :lottery_id, :target_url, 'queued',
                  :execution_path_id, :target_hash, :rule_snapshot_id, :rule_hash,
                  :action_plan_hash, :config_hash, :account_lease_id,
                  :account_lease_generation)""",
            {
                "probe_id": probe_id,
                "platform": lottery["platform"],
                "account_id": account["id"],
                "lottery_id": lottery_id,
                "target_url": target_url,
                "execution_path_id": plan_binding["execution_path_id"],
                "target_hash": target_hash,
                "rule_snapshot_id": plan_binding["rule_snapshot_id"],
                "rule_hash": plan_binding["rule_hash"],
                "action_plan_hash": plan_binding["action_plan_hash"],
                "config_hash": config_hash,
                "account_lease_id": account_lease.lease_id,
                "account_lease_generation": account_lease.generation,
            },
        )
        await enqueue_outbox(
            message,
            adapter_probe_stream_for_platform(lottery["platform"]),
            dedup_key=outbox_key,
        )
    try:
        await try_flush_dedup(outbox_key)
    except Exception as exc:
        structured_log(
            "warning",
            "adapter_probe_immediate_flush_failed",
            probe_id=probe_id,
            error=str(exc),
        )
    await _record_post_commit_event(
        aggregate="lottery",
        aggregate_id=lottery_id,
        event_type="AdapterProbeQueued",
        payload={
            "probe_id": probe_id,
            "platform": lottery["platform"],
            "account_id": account["id"],
            "target_url": target_url,
            "execution_path_id": plan_binding["execution_path_id"],
            "target_hash": target_hash,
            "rule_snapshot_id": plan_binding["rule_snapshot_id"],
            "rule_hash": plan_binding["rule_hash"],
            "action_plan_hash": plan_binding["action_plan_hash"],
            "config_hash": config_hash,
            "execution_revision": plan_binding["execution_revision"],
            "account_lease_id": account_lease.lease_id,
            "account_lease_generation": account_lease.generation,
        },
        correlation_id=probe_id,
        actor_type="operator",
        actor_id=actor["actor_id"],
    )
    return {
        "status": "queued",
        "probe_id": probe_id,
        "account_id": account["id"],
        "execution_path_id": plan_binding["execution_path_id"],
        "config_hash": config_hash,
        "execution_revision": plan_binding["execution_revision"],
        "account_lease_id": account_lease.lease_id,
        "account_lease_generation": account_lease.generation,
    }


@router.post("/probes/{probe_id}/apply-config")
async def apply_probe_recommended_config(probe_id: str, request: Request):
    """Apply a succeeded probe's recommended selector config to the runtime.

    Closes the last manual step: instead of copy-pasting ``_recommended_config``
    from a probe result into ``PUT /adapters/config``, an admin confirms here and
    the platform's recommendation is saved through the same path with the same
    completeness bar. This only *configures* the adapter 鈥?every other real-run
    gate (global switch, calibrated account, recent shadow-run, reviewed plan,
    no recent risk) still applies, so it can never by itself enable a real run.
    """
    actor = require_min_role(request, "admin")
    require_confirmation(request)
    probe = await database.fetch_one(
        "SELECT probe_id, platform, status, result FROM adapter_calibrations WHERE probe_id = :probe_id",
        {"probe_id": probe_id},
    )
    if not probe:
        raise HTTPException(404, detail="Probe not found")
    if probe["status"] != "succeeded":
        raise HTTPException(409, detail=f"Probe is not succeeded (status: {probe['status']})")

    platform = probe["platform"]
    required_phases = selector_phases_for_platform(platform)
    recommended = recommended_config_from_probe(probe["result"], platform)
    if not recommended:
        raise HTTPException(409, detail="Probe has no recommended selector config to apply")
    if not selector_config_complete(platform, recommended):
        raise HTTPException(
            409,
            detail={
                "message": "Recommended config is incomplete; re-probe or finish it by hand before applying",
                "phases": {phase: phase_configured(platform, recommended, phase) for phase in required_phases},
            },
        )

    await save_platform_selector_config(platform, recommended)
    phase_status = {phase: phase_configured(platform, recommended, phase) for phase in required_phases}
    await audit_event(
        request,
        action="adapter_selector_config.apply_probe",
        resource_type="adapter_selector_config",
        resource_id=platform,
        result="saved",
        risk_level="high",
        detail={"probe_id": probe_id, "platform": platform, "phases": phase_status},
    )
    await _record_post_commit_event(
        aggregate="platform",
        aggregate_id=platform,
        event_type="AdapterSelectorConfigSaved",
        payload={"configured": True, "phases": phase_status, "source": "probe", "probe_id": probe_id},
        actor_type="operator",
        actor_id=actor["actor_id"],
    )
    return {"status": "saved", "platform": platform, "probe_id": probe_id, "configured": True, "phases": phase_status}


@router.get("/probes/{probe_id}/screenshot")
async def get_probe_screenshot(probe_id: str):
    row = await database.fetch_one(
        """SELECT screenshot_path, platform
           FROM adapter_calibrations
           WHERE probe_id = :probe_id""",
        {"probe_id": probe_id},
    )
    if not row or not row["screenshot_path"]:
        raise HTTPException(404, detail="Probe screenshot not found")

    return _evidence_png_response(
        row["screenshot_path"],
        allowed_roots=_platform_evidence_roots(
            row["platform"],
            "adapter-probes",
        ),
    )


@router.get("/tasks/runs/{task_id}/screenshot")
async def get_task_screenshot(task_id: str):
    row = await database.fetch_one(
        """SELECT tr.screenshot_path, l.platform
           FROM task_runs tr
           JOIN lotteries l ON l.id = tr.lottery_id
           WHERE tr.task_id = :task_id""",
        {"task_id": task_id},
    )
    if not row or not row["screenshot_path"]:
        raise HTTPException(404, detail="Task screenshot not found")

    return _evidence_png_response(
        row["screenshot_path"],
        allowed_roots=_platform_evidence_roots(
            row["platform"],
            "task-failures",
            "shadow-runs",
        ),
    )


@router.put("/{lottery_id}/result")
async def update_lottery_result(lottery_id: int, data: LotteryResultUpdate, request: Request):
    actor = require_min_role(request, "operator")
    if data.status not in {"participated", "won", "lost", "expired"}:
        raise HTTPException(400, detail="status must be participated, won, lost, or expired")

    async with database.transaction():
        lottery = await database.fetch_one(
            "SELECT id, status, execution_lock FROM lotteries WHERE id = :id FOR UPDATE",
            {"id": lottery_id},
        )
        require_lottery_not_executing(lottery, operation="record a result")
        await database.execute(
            "UPDATE lotteries SET status = :status WHERE id = :id",
            {"id": lottery_id, "status": data.status},
        )
        if data.note:
            await database.execute(
                """INSERT INTO notify_logs (channel, title, content, success)
                   VALUES ('manual', :title, :content, 1)""",
                {"title": f"Lottery {lottery_id} result", "content": data.note},
            )
        await audit_event(
            request,
            action="lottery.result.update",
            resource_type="lottery",
            resource_id=lottery_id,
            result="saved",
            risk_level="high",
            detail={"status": data.status},
        )
    event_type = {
        "participated": "LotteryJoined",
        "won": "LotteryWon",
        "lost": "LotteryLost",
        "expired": "LotteryExpired",
    }[data.status]
    await _record_post_commit_event(
        aggregate="lottery",
        aggregate_id=lottery_id,
        event_type=event_type,
        payload={"status": data.status, "note": data.note},
        actor_type="operator",
        actor_id=actor["actor_id"],
    )
    return {"status": "updated", "lottery_id": lottery_id, "result": data.status}


ACCOUNT_PICK_SELECT = """SELECT a.*,
       c.calibration_id AS latest_calibration_id,
       c.status AS latest_calibration_status,
       c.result AS latest_calibration_result,
       (c.created_at >= DATE_SUB(NOW(), INTERVAL 24 HOUR))
         AS latest_calibration_fresh
  FROM accounts a
  LEFT JOIN account_calibrations c
    ON c.id = (
      SELECT latest.id
      FROM account_calibrations latest
      WHERE latest.account_id = a.id
        AND latest.platform = a.platform
      ORDER BY latest.id DESC
      LIMIT 1
    )
 WHERE a.platform = :platform
   AND a.status = 'ready'
   AND a.deleted_at IS NULL
   AND OCTET_LENGTH(a.encrypted_credential) > 0
   AND c.status = 'succeeded'
   AND NOT EXISTS (
     SELECT 1 FROM account_operation_leases lease
     WHERE lease.account_id = a.id
       AND lease.released_at IS NULL
       AND lease.expires_at > NOW()
   )"""


async def _account_candidate_is_available(
    row,
    *,
    platform: str,
    execution_path_id: str,
    required_actions: tuple[str, ...],
    require_account_capability: bool,
) -> bool:
    if not row:
        return False
    values = dict(row)
    platform_module = get_platform_module(platform)
    if platform_module is None or not platform_module.account_candidate_supports_execution(
        row=values,
        execution_path_id=execution_path_id,
        required_actions=required_actions,
        require_capability=require_account_capability,
    ):
        return False
    risk = await recent_account_risk(int(values["id"]))
    return not risk["has_recent_risk"]


async def _fetch_account_candidate(account_id: int, platform: str):
    return await database.fetch_one(
        f"{ACCOUNT_PICK_SELECT} AND a.id = :id LIMIT 1",
        {"id": account_id, "platform": platform},
    )


async def pick_account(
    account_id: int | None,
    platform: str,
    *,
    execution_path_id: str = "",
    required_actions: tuple[str, ...] | list[str] = (),
    require_account_capability: bool = True,
    require_weibo_capability: bool | None = None,
):
    if require_weibo_capability is not None:
        # Compatibility for internal/tests written before account validation
        # became a platform-module policy.
        require_account_capability = require_weibo_capability
    actions = tuple(required_actions)
    if account_id is not None:
        row = await _fetch_account_candidate(account_id, platform)
        if await _account_candidate_is_available(
            row,
            platform=platform,
            execution_path_id=execution_path_id,
            required_actions=actions,
            require_account_capability=require_account_capability,
        ):
            return row
        return None

    recommendations = await load_strategy_account_recommendations(
        platform,
        exclude_active_leases=True,
    )
    recommended = first_or_none(recommendations.get(platform, []))
    if recommended:
        try:
            recommended_id = int(recommended["account_id"])
        except (KeyError, TypeError, ValueError):
            recommended_id = None
        if recommended_id is not None:
            row = await _fetch_account_candidate(recommended_id, platform)
            if await _account_candidate_is_available(
                row,
                platform=platform,
                execution_path_id=execution_path_id,
                required_actions=actions,
                require_account_capability=require_account_capability,
            ):
                return row

    candidates = await database.fetch_all(
        f"{ACCOUNT_PICK_SELECT} ORDER BY a.daily_task_count ASC, a.id ASC LIMIT 25",
        {"platform": platform},
    )
    for row in candidates:
        if await _account_candidate_is_available(
            row,
            platform=platform,
            execution_path_id=execution_path_id,
            required_actions=actions,
            require_account_capability=require_account_capability,
        ):
            return row
    return None


def resolve_task_mode(data: DispatchTaskRequest) -> str:
    if data.mode:
        return data.mode.value if hasattr(data.mode, "value") else str(data.mode)
    return "dry_run" if data.dry_run else "real_run"


def account_execution_path_for_dispatch(
    platform: str,
    *,
    task_mode: str,
    stored_execution_path: str,
) -> str:
    """Choose credential transport independently from a Weibo plan's path."""

    platform_module = get_platform_module(platform)
    if platform_module is None:
        return ""
    return platform_module.account_execution_path_for_dispatch(
        task_mode=task_mode,
        stored_execution_path=stored_execution_path,
    )


def strategy_readiness_view(readiness: dict) -> dict:
    """Expose decision evidence without leaking credential/calibration rows."""

    return {
        key: readiness.get(key)
        for key in (
            "allowed",
            "blockers",
            "action_plan_ready",
            "rule_snapshot_ready",
            "execution_evidence_bound",
            "execution_evidence_id",
            "execution_path_id",
            "execution_revision",
            "execution_mode",
            "real_run_supported",
            "oauth_capability_ready",
            "oauth_capability_denied_actions",
            "oauth_dry_run_ready",
            "oauth_dry_run_task_id",
            "account_risk",
        )
        if key in readiness
    }


async def load_strategy_breaker_statuses(
    platforms,
) -> dict[str, tuple[bool, str | None]]:
    """Evaluate each platform breaker once for a multi-target strategy read."""

    requested_platforms = tuple(
        dict.fromkeys(
            str(platform).strip().casefold()
            for platform in platforms
            if str(platform).strip()
        )
    )

    async def load_platform(platform: str):
        try:
            status = await asyncio.wait_for(
                circuit_breaker_allows(platform),
                timeout=max(float(STRATEGY_QUERY_TIMEOUT_SECONDS), 0.001),
            )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            structured_log(
                "warning",
                STRATEGY_BREAKER_QUERY_TIMEOUT_BLOCKER,
                platform=platform,
                timeout_seconds=STRATEGY_QUERY_TIMEOUT_SECONDS,
            )
            status = (False, STRATEGY_BREAKER_QUERY_TIMEOUT_BLOCKER)
        except Exception as exc:
            structured_log(
                "error",
                STRATEGY_BREAKER_QUERY_FAILED_BLOCKER,
                platform=platform,
                error=str(exc),
            )
            status = (False, STRATEGY_BREAKER_QUERY_FAILED_BLOCKER)
        return platform, status

    statuses = await asyncio.gather(
        *(load_platform(platform) for platform in requested_platforms)
    )
    return dict(statuses)


async def compute_strategy_item(
    item: dict,
    *,
    selector_config: dict,
    real_run_enabled: bool,
    platform_knowledge: dict[str, dict],
    account_recommendations: dict[str, list[dict]],
    account_readiness: dict | None = None,
    breaker_status: tuple[bool, str | None] | None = None,
    include_breakdown: bool = False,
) -> dict:
    """Score a single lottery row against the strategy gate and ranking model.

    Shared by ``/strategy/queue`` (ranking many targets) and
    ``/{lottery_id}/strategy/explain`` (explaining one target), so both
    surfaces stay consistent. ``include_breakdown`` adds the named score
    components used by the explain endpoint.
    """
    platform = item["platform"]
    platform_module_blocker = None
    try:
        platform_module = get_platform_module(platform)
    except PlatformModuleUnavailableError:
        platform_module = None
        platform_module_blocker = "platform_module_unavailable"
    except PlatformCapabilityError as exc:
        platform_module = None
        platform_module_blocker = exc.code
    cfg = get_platform(platform) or {}
    safe_accounts = int(item.get("safe_accounts") or 0)
    active_runs = int(item.get("active_runs") or 0)
    dry_success = int(item.get("dry_success") or 0)
    shadow_success = int(item.get("shadow_success") or 0)
    failed_runs = int(item.get("failed_runs") or 0)
    recent_risk = int(item.get("recent_platform_risk") or 0)
    adapter_kind = platform_real_adapter_kind(selector_config, platform)
    adapter_ready = bool(cfg.get("action_adapter")) or platform_has_runtime_real_adapter(selector_config, platform)
    if breaker_status is None:
        breaker_allowed, breaker_reason = await circuit_breaker_allows(
            platform
        )
    else:
        breaker_allowed, breaker_reason = breaker_status
    if platform_module_blocker:
        target = LotteryTargetValidation(
            False,
            reason=platform_module_blocker,
        )
    else:
        try:
            target = validate_lottery_identity(
                platform,
                item.get("raw_url"),
                item.get("canonical_url"),
            )
        except PlatformModuleUnavailableError:
            platform_module = None
            platform_module_blocker = "platform_module_unavailable"
            target = LotteryTargetValidation(
                False,
                reason=platform_module_blocker,
            )
        except PlatformCapabilityError as exc:
            platform_module = None
            platform_module_blocker = exc.code
            target = LotteryTargetValidation(
                False,
                reason=platform_module_blocker,
            )
    target_real_valid = bool(
        platform_module
        and platform_module.strategy_target_is_real_valid(target)
    )
    target_error = (
        platform_module.strategy_target_error(target)
        if platform_module is not None
        else (
            platform_module_blocker
            or target.reason
            or "invalid_lottery_target"
        )
    )
    readiness = (
        dict(account_readiness.get("readiness") or {})
        if isinstance(account_readiness, dict)
        else {}
    )
    execution_readiness_ready = readiness.get("allowed") is True
    selected_account_id = (
        account_readiness.get("account_id")
        if isinstance(account_readiness, dict)
        else None
    )
    recommended_accounts = account_recommendations.get(platform, [])
    recommended_account = next(
        (
            account
            for account in recommended_accounts
            if str(account.get("account_id")) == str(selected_account_id)
        ),
        first_or_none(recommended_accounts),
    )
    raw_readiness_blockers = readiness.get("blockers")
    execution_readiness_blockers = (
        [str(blocker) for blocker in raw_readiness_blockers]
        if isinstance(raw_readiness_blockers, list)
        else ["account_scoped_real_run_readiness_unavailable"]
    )

    if target_real_valid:
        recommended_mode, reason_codes, blockers = choose_strategy_mode(
            safe_accounts=safe_accounts,
            active_runs=active_runs,
            dry_success=dry_success,
            shadow_success=shadow_success,
            adapter_ready=adapter_ready,
            execution_readiness_ready=execution_readiness_ready,
            real_run_enabled=real_run_enabled,
            breaker_allowed=breaker_allowed,
            breaker_reason=breaker_reason,
            manual_assisted=cfg.get("execution_mode") == "manual_assisted",
        )
    else:
        recommended_mode = "blocked"
        reason_codes = ["invalid_lottery_target"]
        blockers = [target_error]
    if recent_risk:
        reason_codes.append("recent_platform_risk")
    if failed_runs:
        reason_codes.append("recent_failures")
    if item["value_score"] >= 70:
        reason_codes.append("high_value")

    knowledge = platform_knowledge.get(platform) or empty_platform_knowledge(platform)
    account_reputation_score = recommended_account["reputation_score"] if recommended_account else 0
    win_probability_breakdown = estimate_win_probability_breakdown(
        knowledge.get("win_rate"),
        knowledge.get("knowledge_confidence", 0),
    )
    estimated_win_probability = win_probability_breakdown["blended"]
    trust_score_breakdown = estimate_trust_score_breakdown(
        account_reputation_score=account_reputation_score,
        recent_platform_risk=recent_risk,
        knowledge_confidence=knowledge.get("knowledge_confidence", 0),
    )
    trust_score = trust_score_breakdown["trust_score"]
    expected_value = round(int(item["value_score"] or 0) * estimated_win_probability * trust_score, 4)
    if knowledge.get("knowledge_confidence", 0) >= 40:
        reason_codes.append("platform_knowledge_used")
    elif knowledge.get("total_lotteries", 0) or knowledge.get("total_runs", 0):
        reason_codes.append("low_knowledge_confidence")
    if knowledge.get("win_rate") is not None:
        reason_codes.append("historical_win_rate_used")
    if recommended_account:
        reason_codes.append("account_reputation_used")
        if account_reputation_score < 60:
            reason_codes.append("account_reputation_low")

    score_breakdown = strategy_score_breakdown(
        value_score=int(item["value_score"] or 0),
        recommended_mode=recommended_mode,
        dry_success=dry_success,
        shadow_success=shadow_success,
        failed_runs=failed_runs,
        recent_risk=recent_risk,
        knowledge_confidence=knowledge.get("knowledge_confidence", 0),
        estimated_win_probability=estimated_win_probability,
        account_reputation_score=account_reputation_score,
        expected_value=expected_value,
    )

    result = {
        "lottery_id": item["id"],
        "platform": platform,
        "raw_url": item["raw_url"],
        "status": item["status"],
        "value_score": item["value_score"],
        "strategy_score": score_breakdown["total"],
        "priority_tier": priority_tier(score_breakdown["total"]) if recommended_mode != "blocked" else "hold",
        "expected_value": expected_value,
        "estimated_win_probability": estimated_win_probability,
        "trust_score": trust_score,
        "recommended_mode": recommended_mode,
        "reason_codes": reason_codes,
        "blockers": blockers,
        "platform_knowledge": knowledge,
        "recommended_account": recommended_account,
        "safe_accounts": safe_accounts,
        "active_runs": active_runs,
        "dry_success": dry_success,
        "shadow_success": shadow_success,
        "failed_runs": failed_runs,
        "recent_platform_risk": recent_risk,
        "adapter_ready": adapter_ready,
        # Compatibility alias: this now reflects the platform validator's
        # account-bound proof, never an unbound selector probe summary.
        "probe_ready": bool(readiness.get("probe_ready")),
        "execution_readiness_ready": execution_readiness_ready,
        "execution_readiness_blockers": execution_readiness_blockers,
        "execution_readiness": strategy_readiness_view(readiness),
        "real_run_enabled": real_run_enabled,
        "breaker_allowed": breaker_allowed,
        "target_valid": target_real_valid,
        "target_kind": target.kind,
    }
    if include_breakdown:
        result["explain"] = {
            "mode": {
                "recommended_mode": recommended_mode,
                "reason_codes": reason_codes,
                "blockers": blockers,
                "breaker_allowed": breaker_allowed,
                "breaker_reason": breaker_reason,
                "target_valid": target_real_valid,
                "target_kind": target.kind,
                "target_error": None
                if target_real_valid
                else target_error,
                "execution_readiness_ready": execution_readiness_ready,
                "execution_readiness_blockers": (
                    execution_readiness_blockers
                ),
            },
            "score": score_breakdown,
            "win_probability": win_probability_breakdown,
            "trust_score": trust_score_breakdown,
            "knowledge_confidence": strategy_knowledge_confidence_breakdown(knowledge),
        }
    return result


async def load_strategy_platform_knowledge(window_days: int = 30) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for platform in get_platforms():
        result[platform] = empty_platform_knowledge(platform)
    bounded_window_days = min(
        max(int(window_days or 1), 1),
        STRATEGY_HISTORY_WINDOW_DAYS_MAX,
    )

    async def load_history():
        values = {"window_days": bounded_window_days}
        rows = await database.fetch_all(
            """SELECT platform,
                      COUNT(*) AS total_lotteries,
                      SUM(status = 'pending') AS pending_lotteries,
                      SUM(status = 'won') AS won_lotteries,
                      SUM(status = 'lost') AS lost_lotteries,
                      SUM(value_score >= 70) AS high_value_lotteries,
                      AVG(value_score) AS avg_value_score
               FROM lotteries
               WHERE extracted_at >= DATE_SUB(
                     NOW(), INTERVAL :window_days DAY
                   )
               GROUP BY platform""",
            values,
        )
        task_rows = await database.fetch_all(
            """SELECT l.platform,
                      COUNT(*) AS total_runs,
                      SUM(tr.status = 'succeeded') AS succeeded_runs,
                      SUM(tr.status = 'failed') AS failed_runs,
                      SUM(COALESCE(
                            tr.task_mode,
                            IF(tr.dry_run = 1, 'dry_run', 'real_run')
                          ) = 'shadow_run'
                          AND tr.status = 'succeeded') AS shadow_success,
                      SUM(COALESCE(
                            tr.task_mode,
                            IF(tr.dry_run = 1, 'dry_run', 'real_run')
                          ) = 'real_run'
                          AND tr.status = 'succeeded') AS real_success
               FROM task_runs tr
               JOIN lotteries l ON l.id = tr.lottery_id
               WHERE tr.created_at >= DATE_SUB(
                     NOW(), INTERVAL :window_days DAY
                   )
               GROUP BY l.platform""",
            values,
        )
        risk_rows = await database.fetch_all(
            """SELECT a.platform,
                      COUNT(*) AS risk_events
               FROM risk_events r
               JOIN accounts a ON a.id = r.account_id
               WHERE r.created_at >= DATE_SUB(
                     NOW(), INTERVAL :window_days DAY
                   )
               GROUP BY a.platform""",
            values,
        )
        return rows, task_rows, risk_rows

    try:
        rows, task_rows, risk_rows = await asyncio.wait_for(
            load_history(),
            timeout=max(float(STRATEGY_QUERY_TIMEOUT_SECONDS), 0.001),
        )
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError:
        structured_log(
            "warning",
            "strategy_platform_knowledge_query_timeout",
            window_days=bounded_window_days,
        )
        return result

    for row in rows:
        platform = row["platform"]
        result.setdefault(platform, empty_platform_knowledge(platform))
        won = as_int(row["won_lotteries"])
        lost = as_int(row["lost_lotteries"])
        result[platform].update(
            {
                "total_lotteries": as_int(row["total_lotteries"]),
                "pending_lotteries": as_int(row["pending_lotteries"]),
                "won_lotteries": won,
                "lost_lotteries": lost,
                "high_value_lotteries": as_int(row["high_value_lotteries"]),
                "avg_value_score": round(as_float(row["avg_value_score"]), 2),
                "win_rate": safe_ratio(won, won + lost),
            }
        )
    for row in task_rows:
        platform = row["platform"]
        result.setdefault(platform, empty_platform_knowledge(platform))
        succeeded = as_int(row["succeeded_runs"])
        total_runs = as_int(row["total_runs"])
        result[platform].update(
            {
                "total_runs": total_runs,
                "succeeded_runs": succeeded,
                "failed_runs": as_int(row["failed_runs"]),
                "task_success_rate": safe_ratio(succeeded, total_runs),
                "shadow_success": as_int(row["shadow_success"]),
                "real_success": as_int(row["real_success"]),
            }
        )
    for row in risk_rows:
        platform = row["platform"]
        result.setdefault(platform, empty_platform_knowledge(platform))
        result[platform]["risk_events"] = as_int(row["risk_events"])

    for item in result.values():
        item["knowledge_confidence"] = strategy_knowledge_confidence(item)
    return result


async def load_strategy_account_recommendations(
    platform: str | None = None,
    window_days: int = 30,
    *,
    exclude_active_leases: bool = False,
) -> StrategyAccountRecommendations:
    lease_filter = (
        """AND NOT EXISTS (
                SELECT 1
                FROM account_operation_leases lease
                WHERE lease.account_id = a.id
                  AND lease.released_at IS NULL
                  AND lease.expires_at > NOW()
              )"""
        if exclude_active_leases
        else ""
    )
    bounded_window_days = min(
        max(int(window_days or 1), 1),
        STRATEGY_HISTORY_WINDOW_DAYS_MAX,
    )
    requested_platforms = [
        str(platform).strip().casefold()
    ] if platform else list(get_platforms())

    async def load_platform(platform_key: str):
        async def load_bounded_rows():
            account_rows = await database.fetch_all(
                f"""SELECT a.id,
                           a.platform,
                           a.status,
                           a.risk_score,
                           a.daily_task_count,
                           a.last_active_at,
                           latest_calibration.status
                             AS latest_calibration_status
                    FROM accounts a
                    JOIN account_calibrations latest_calibration
                      ON latest_calibration.id = (
                        SELECT candidate.id
                        FROM account_calibrations candidate
                        WHERE candidate.account_id = a.id
                          AND candidate.platform = a.platform
                        ORDER BY candidate.id DESC
                        LIMIT 1
                      )
                    WHERE a.platform = :strategy_platform
                      AND a.status = 'ready'
                      AND a.deleted_at IS NULL
                      AND OCTET_LENGTH(a.encrypted_credential) > 0
                      AND latest_calibration.status = 'succeeded'
                      {lease_filter}
                    ORDER BY a.daily_task_count ASC, a.id ASC
                    LIMIT :strategy_account_limit""",
                {
                    "strategy_platform": platform_key,
                    "strategy_account_limit": (
                        STRATEGY_ACCOUNT_ROWS_PER_PLATFORM
                    ),
                },
            )
            account_rows = list(account_rows)[
                :STRATEGY_ACCOUNT_ROWS_PER_PLATFORM
            ]
            if not account_rows:
                return [], None

            account_ids = [
                int(dict(row)["id"])
                for row in account_rows
            ]
            account_clause, account_values = _sql_in_values(
                "strategy_account",
                account_ids,
            )
            account_values["window_days"] = bounded_window_days
            task_values = dict(account_values)
            task_values["strategy_task_history_limit"] = (
                STRATEGY_TASK_HISTORY_ROWS_PER_PLATFORM + 1
            )
            task_rows = await database.fetch_all(
                f"""SELECT id, account_id, status, dry_run, task_mode,
                           created_at
                    FROM task_runs
                    WHERE account_id IN ({account_clause})
                      AND created_at >= DATE_SUB(
                            NOW(), INTERVAL :window_days DAY
                          )
                    LIMIT :strategy_task_history_limit""",
                task_values,
            )
            if (
                len(task_rows)
                > STRATEGY_TASK_HISTORY_ROWS_PER_PLATFORM
            ):
                structured_log(
                    "warning",
                    "strategy_account_history_budget_exhausted",
                    platform=platform_key,
                    account_count=len(account_ids),
                    history_kind="task_runs",
                    row_budget=STRATEGY_TASK_HISTORY_ROWS_PER_PLATFORM,
                )
                return [], STRATEGY_ACCOUNT_TASK_HISTORY_BUDGET_BLOCKER

            risk_values = dict(account_values)
            risk_values["strategy_risk_history_limit"] = (
                STRATEGY_RISK_HISTORY_ROWS_PER_PLATFORM + 1
            )
            risk_rows = await database.fetch_all(
                f"""SELECT id, account_id, created_at
                    FROM risk_events
                    WHERE account_id IN ({account_clause})
                      AND created_at >= DATE_SUB(
                            NOW(), INTERVAL :window_days DAY
                          )
                    LIMIT :strategy_risk_history_limit""",
                risk_values,
            )
            if len(risk_rows) > STRATEGY_RISK_HISTORY_ROWS_PER_PLATFORM:
                structured_log(
                    "warning",
                    "strategy_account_history_budget_exhausted",
                    platform=platform_key,
                    account_count=len(account_ids),
                    history_kind="risk_events",
                    row_budget=STRATEGY_RISK_HISTORY_ROWS_PER_PLATFORM,
                )
                return [], STRATEGY_ACCOUNT_RISK_HISTORY_BUDGET_BLOCKER

            stats = {
                account_id: {
                    "total_runs": 0,
                    "succeeded_runs": 0,
                    "failed_runs": 0,
                    "dry_runs": 0,
                    "shadow_runs": 0,
                    "real_runs": 0,
                    "latest_run_at": None,
                    "risk_events": 0,
                    "latest_risk_at": None,
                }
                for account_id in account_ids
            }

            def newest(current, candidate):
                if current is None:
                    return candidate
                if candidate is None:
                    return current
                try:
                    return candidate if candidate > current else current
                except TypeError:
                    return (
                        candidate
                        if str(candidate) > str(current)
                        else current
                    )

            for raw_row in task_rows:
                row = dict(raw_row)
                account_id = int(row["account_id"])
                if account_id not in stats:
                    raise RuntimeError(
                        "strategy task history escaped account scope"
                    )
                item = stats[account_id]
                item["total_runs"] += 1
                status = str(row.get("status") or "")
                if status == "succeeded":
                    item["succeeded_runs"] += 1
                elif status == "failed":
                    item["failed_runs"] += 1
                mode = str(row.get("task_mode") or "")
                if not mode:
                    mode = (
                        "dry_run"
                        if as_int(row.get("dry_run")) == 1
                        else "real_run"
                    )
                if mode in {"dry_run", "shadow_run", "real_run"}:
                    item[f"{mode.replace('_run', '')}_runs"] += 1
                item["latest_run_at"] = newest(
                    item["latest_run_at"],
                    row.get("created_at"),
                )

            for raw_row in risk_rows:
                row = dict(raw_row)
                account_id = int(row["account_id"])
                if account_id not in stats:
                    raise RuntimeError(
                        "strategy risk history escaped account scope"
                    )
                item = stats[account_id]
                item["risk_events"] += 1
                item["latest_risk_at"] = newest(
                    item["latest_risk_at"],
                    row.get("created_at"),
                )

            recommendations = []
            for raw_account in account_rows:
                item = dict(raw_account)
                item.update(stats[int(item["id"])])
                reputation = account_reputation(
                    status=item["status"],
                    risk_score=as_int(item["risk_score"]),
                    latest_calibration_status=item.get(
                        "latest_calibration_status"
                    ),
                    total_runs=as_int(item["total_runs"]),
                    succeeded_runs=as_int(item["succeeded_runs"]),
                    failed_runs=as_int(item["failed_runs"]),
                    shadow_runs=as_int(item["shadow_runs"]),
                    real_runs=as_int(item["real_runs"]),
                    risk_events=as_int(item["risk_events"]),
                )
                recommendations.append(
                    {
                        "account_id": item["id"],
                        "platform": item["platform"],
                        "reputation_score": reputation,
                        "account_tier": account_tier(reputation),
                        "risk_score": as_int(item["risk_score"]),
                        "risk_events": as_int(item["risk_events"]),
                        "daily_task_count": as_int(
                            item["daily_task_count"]
                        ),
                        "total_runs": as_int(item["total_runs"]),
                        "succeeded_runs": as_int(
                            item["succeeded_runs"]
                        ),
                        "failed_runs": as_int(item["failed_runs"]),
                        "dry_runs": as_int(item["dry_runs"]),
                        "shadow_runs": as_int(item["shadow_runs"]),
                        "real_runs": as_int(item["real_runs"]),
                        "task_success_rate": safe_ratio(
                            as_int(item["succeeded_runs"]),
                            as_int(item["total_runs"]),
                        ),
                        "latest_run_at": item["latest_run_at"],
                        "latest_risk_at": item["latest_risk_at"],
                        "last_active_at": item["last_active_at"],
                    }
                )
            recommendations.sort(
                key=lambda account: (
                    account["reputation_score"],
                    -account["daily_task_count"],
                    -account["risk_events"],
                    -account["failed_runs"],
                ),
                reverse=True,
            )
            return recommendations, None

        try:
            rows, blocker = await asyncio.wait_for(
                load_bounded_rows(),
                timeout=max(
                    float(STRATEGY_QUERY_TIMEOUT_SECONDS),
                    0.001,
                ),
            )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            structured_log(
                "warning",
                "strategy_account_query_timeout",
                platform=platform_key,
                window_days=bounded_window_days,
            )
            rows = []
            blocker = STRATEGY_ACCOUNT_QUERY_TIMEOUT_BLOCKER
        except Exception as exc:
            # Strategy is an advisory projection. Keep a single platform's
            # database/index drift local and omit its candidates so it cannot
            # accidentally authorize a real run.
            structured_log(
                "error",
                "strategy_account_query_failed",
                platform=platform_key,
                error=str(exc),
            )
            rows = []
            blocker = STRATEGY_ACCOUNT_QUERY_FAILED_BLOCKER
        return platform_key, rows, blocker

    platform_rows = await asyncio.gather(
        *(load_platform(platform_key) for platform_key in requested_platforms)
    )
    grouped = {
        platform_key: rows
        for platform_key, rows, _blocker in platform_rows
        if rows
    }
    blockers_by_platform = {
        platform_key: blocker
        for platform_key, _rows, blocker in platform_rows
        if blocker
    }
    return StrategyAccountRecommendations(
        grouped,
        blockers_by_platform=blockers_by_platform,
    )


def safe_ratio(numerator: int, denominator: int) -> float | None:
    if not denominator:
        return None
    return round(numerator / denominator, 4)


def as_int(value) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def as_float(value) -> float:
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def serialize_lottery(row) -> dict:
    item = dict(row)
    item["action_plan"] = parse_json_field(item.get("action_plan"))
    item["target_identity"] = target_identity_from_lottery(item)
    return item


async def save_platform_selector_config(platform: str, config: dict):
    row = await database.fetch_one(
        "SELECT id FROM adapter_selector_configs WHERE platform = :platform",
        {"platform": platform},
    )
    values = {"platform": platform, "config_json": json.dumps(config, ensure_ascii=False)}
    if row:
        await database.execute(
            "UPDATE adapter_selector_configs SET config_json = :config_json, updated_at = NOW() WHERE platform = :platform",
            values,
        )
        return
    await database.execute(
        "INSERT INTO adapter_selector_configs (platform, config_json) VALUES (:platform, :config_json)",
        values,
    )


def parse_target_lines(content: str, default_platform: str, default_score: int):
    rows = []
    for index, raw_line in enumerate(content.splitlines(), start=1):
        raw = raw_line.strip()
        if not raw or raw.startswith("#"):
            continue
        parts = [part.strip() for part in raw.replace("\t", ",").split(",")]
        parts = [part for part in parts if part]
        platform = default_platform
        url = parts[0] if parts else ""
        score = default_score
        expires_at = None

        if len(parts) >= 2 and not looks_like_url(parts[0]):
            platform = parts[0]
            url = parts[1]
            if len(parts) >= 3:
                score = parse_int(parts[2], default_score)
            if len(parts) >= 4:
                expires_at = parts[3]
        elif len(parts) >= 2:
            score = parse_int(parts[1], default_score)
            if len(parts) >= 3:
                expires_at = parts[2]

        if not get_platform(platform):
            rows.append({"line": index, "raw": raw, "error": f"Unsupported platform: {platform}"})
            continue
        if not looks_like_url(url):
            rows.append({"line": index, "raw": raw, "error": "Invalid URL"})
            continue
        if not 0 <= score <= 100:
            rows.append(
                {
                    "line": index,
                    "raw": raw,
                    "error": "Value score must be between 0 and 100",
                }
            )
            continue
        if len(url) > LOTTERY_RAW_URL_MAX_LENGTH:
            rows.append(
                {
                    "line": index,
                    "raw": raw,
                    "error": "Target URL exceeds storage limit",
                }
            )
            continue

        rows.append(
            {
                "line": index,
                "raw": raw,
                "platform": platform,
                "raw_url": url,
                "value_score": score,
                "expires_at": expires_at,
            }
        )
    return rows


def looks_like_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


async def canonicalize_lottery_url(platform: str, raw_url: str) -> str:
    return await canonicalize_platform_url(platform, raw_url)


def parse_int(value: str, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default
