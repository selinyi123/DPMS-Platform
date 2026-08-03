
import time, psutil, json, asyncio, math
from datetime import datetime, timezone
from dataclasses import replace

from fastapi import APIRouter, HTTPException, Query, Request

from fastapi.responses import PlainTextResponse, StreamingResponse

from app.config import settings
from app.adapter_config import (
    load_runtime_selector_config,
    platform_has_runtime_real_adapter,
    platform_probe_ready_for_real_actions,
    platform_real_adapter_kind,
    selector_config_complete,
)
from app.db import database, redis
from app.event_store.service import record_event
from app.api.notify import (
    VALID_NOTIFICATION_CHANNELS,
    configured_channels,
    notification_config_revision,
)
from app.models.schemas import (
    AutopilotHeartbeatReport,
    RealRunSettingUpdate,
    RuntimeRollbackRequest,
)
from app.platforms import get_platforms
from app.autopilot import PLAN_REQUIRED_VALIDATION_PLATFORMS
from app.action_plan import action_order_for_platform
from app.services.real_run_readiness import (
    EXACT_REAL_CANDIDATE_TARGET_METRICS_SQL,
    evaluate_exact_real_candidate_observation,
    load_account_scoped_real_run_readiness_batch,
    validate_real_run_evidence,
    validate_weibo_oauth_capability_attestation,
)
from app.services.task_transport_health import (
    REPAIR_CONSUMER_MAX_IDLE_MILLISECONDS,
    repair_consumer_idle_by_name,
    worker_rows_support_task_lane,
)
from app.security import (
    audit_event,
    is_real_run_enabled,
    parse_bool,
    require_confirmation,
    require_min_role,
    set_runtime_setting,
)
from app.task_streams import (
    LEGACY_TASK_FANOUT_CONSUMER_NAME,
    LEGACY_TASK_STREAM_KEY,
    task_stream_bindings,
)
from app.adapter_probe_streams import (
    LEGACY_ADAPTER_PROBE_FANOUT_CONSUMER_NAME,
    LEGACY_ADAPTER_PROBE_FANOUT_GROUP_NAME,
    LEGACY_ADAPTER_PROBE_STREAM_KEY,
    adapter_probe_stream_bindings,
)
from app.account_calibration_streams import (
    LEGACY_ACCOUNT_CALIBRATION_FANOUT_CONSUMER_NAME,
    LEGACY_ACCOUNT_CALIBRATION_FANOUT_GROUP_NAME,
    LEGACY_ACCOUNT_CALIBRATION_STREAM_KEY,
    account_calibration_stream_bindings,
)
from shared.redis_consumer_groups import (
    MAX_OBSERVED_CONSUMER_GROUPS,
    MAX_OBSERVED_CONSUMERS_PER_GROUP,
    evaluate_consumer_group_governance,
    normalized_consumer_group_name,
)
from shared.discovery_scan_streams import DISCOVERY_SCAN_STREAM_BINDINGS
from shared.platform_ids import PLATFORM_IDS

from app.utils.log import structured_log



router = APIRouter()


def _prometheus_name(value: str) -> str:
    normalized = "".join(
        character.lower() if character.isalnum() else "_"
        for character in str(value or "")
    ).strip("_")
    return f"dpms_{normalized or 'metric'}"


def _prometheus_value(value) -> str | None:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if math.isfinite(float(value)):
            return str(value)
    return None


def _prometheus_label(value: str) -> str:
    """Escape a label without allowing backslash/newline syntax injection."""

    return (
        str(value)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
    )


def _prometheus_lines(payload: dict) -> list[str]:
    lines: list[str] = []
    emitted: set[str] = set()

    def emit(name: str, value, labels: dict[str, str] | None = None) -> None:
        metric = _prometheus_name(name)
        rendered = _prometheus_value(value)
        if rendered is None:
            return
        if metric not in emitted:
            lines.append(f"# TYPE {metric} gauge")
            emitted.add(metric)
        suffix = ""
        if labels:
            suffix = "{" + ",".join(
                f'{key}="{_prometheus_label(label)}"'
                for key, label in sorted(labels.items())
            ) + "}"
        lines.append(f"{metric}{suffix} {rendered}")

    def walk(prefix: str, value, labels: dict[str, str] | None = None) -> None:
        scalar = _prometheus_value(value)
        if scalar is not None:
            emit(prefix, value, labels)
            return
        if isinstance(value, list):
            if value and "consumer_group_retention" in prefix:
                emit(prefix, len(value), labels)
            return
        if isinstance(value, dict):
            if value and set(value).issubset(set(PLATFORM_IDS)):
                for platform, item in value.items():
                    platform_labels = {
                        **(labels or {}),
                        "platform": platform,
                    }
                    if _prometheus_value(item) is not None:
                        emit(prefix, item, platform_labels)
                    elif isinstance(item, dict):
                        for key, child in item.items():
                            if _prometheus_value(child) is not None:
                                emit(
                                    f"{prefix}_{key}",
                                    child,
                                    platform_labels,
                                )
                return
            for key, child in value.items():
                if isinstance(child, dict) and all(
                    _prometheus_value(item) is not None for item in child.values()
                ):
                    for label, item in child.items():
                        emit(prefix, item, {**(labels or {}), "platform": label})
                elif isinstance(child, (int, float, bool)):
                    emit(f"{prefix}_{key}", child, labels)
                elif isinstance(child, dict):
                    walk(f"{prefix}_{key}", child, labels)

    for key, value in payload.items():
        walk(key, value)
    return lines


EXTERNAL_ACTION_INTENT_STATUSES = frozenset(
    {"pending", "prepared", "started", "succeeded", "failed", "unknown"}
)
# A healthy relay normally clears a lane every five seconds.  Keep a generous
# window so a newly committed row does not flap readiness while still making a
# platform-local relay stall visible before the general task-recovery windows.
TASK_OUTBOX_STALE_SECONDS = 120
# Metrics and readiness must never inherit an unbounded driver/network wait
# from one platform lane. Shared infrastructure can still be degraded, but the
# endpoint returns a fail-closed observation instead of hanging indefinitely.
TASK_METRICS_OPERATION_TIMEOUT_SECONDS = 5.0
# Extra groups are not on the normal execution path, but an abandoned group
# can retain every stream entry indefinitely.  Bound that diagnostic fan-out
# across the *whole* HTTP request, not once per task/probe/calibration stream.
CONSUMER_GROUP_OBSERVATION_MAX_CALLS = 64
CONSUMER_GROUP_OBSERVATION_MAX_CONCURRENCY = 8
AUTOPILOT_HEARTBEAT_WORKER_ID = "core-autopilot"
AUTOPILOT_HEARTBEAT_SERVICE_NAME = "core-autopilot"
AUTOPILOT_HEARTBEAT_MIN_STALE_SECONDS = 90
AUTOPILOT_HEARTBEAT_DETAIL_VERSION = 1
AUTOPILOT_TARGET_READINESS_LIMIT = 100
AUTOPILOT_EXACT_CANDIDATE_TIMEOUT_SECONDS = 8.0
NOTIFICATION_DELIVERY_WINDOW_HOURS = 24
REAL_RUN_FINAL_AUTHORIZATION_GATE_CODES = frozenset({
    "real_run_global_switch",
    "global_circuit_breaker_closed",
    "autopilot_real_run_authorized",
})
REAL_RUN_REQUIRED_TECHNICAL_P0_CHECK_CODES = frozenset({
    "worker_online",
    "all_platform_task_transports_ready",
    "real_run_deployment_capability",
    "autopilot_heartbeat_fresh",
    "autopilot_dispatch_configured",
    "notification_ready",
    "target_pool_ready",
    "autopilot_target_plan_ready",
    "autopilot_exact_real_candidate_ready",
    "all_platforms_dry_ready",
    "real_run_available",
})
REAL_RUN_READINESS_CONTRACT_INVALID_BLOCKER_CODE = (
    "production_readiness_contract_invalid"
)


class _ConsumerGroupObservationBudget:
    """Request-scoped hard cap for non-primary XINFO CONSUMERS calls.

    The timeout covers both waiting for a concurrency slot and the Redis call,
    so a stalled first wave cannot make later observations queue for dozens of
    seconds.  A callable is used to avoid creating an un-awaited Redis
    coroutine when the request budget has already been exhausted.
    """

    def __init__(
        self,
        *,
        max_calls: int = CONSUMER_GROUP_OBSERVATION_MAX_CALLS,
        max_concurrency: int = (
            CONSUMER_GROUP_OBSERVATION_MAX_CONCURRENCY
        ),
        timeout_seconds: float | None = None,
    ):
        if max_calls <= 0 or max_concurrency <= 0:
            raise ValueError("consumer_group_observation_budget_invalid")
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise ValueError("consumer_group_observation_timeout_invalid")
        self._max_calls = int(max_calls)
        self._max_concurrency = int(max_concurrency)
        self._remaining = int(max_calls)
        self._semaphore = asyncio.Semaphore(self._max_concurrency)
        self._lock = asyncio.Lock()
        self._timeout_seconds = float(
            timeout_seconds
            if timeout_seconds is not None
            else TASK_METRICS_OPERATION_TIMEOUT_SECONDS
        )
        self._calls_started = 0
        self._refused = 0

    async def observe(self, *, stream_key: str, group_name: str) -> dict:
        async with self._lock:
            if self._remaining <= 0:
                self._refused += 1
                return {
                    "available": False,
                    "consumers": None,
                    "warning_code": (
                        "consumer_group_observation_budget_exhausted"
                    ),
                }
            self._remaining -= 1

        try:
            async with asyncio.timeout(self._timeout_seconds):
                async with self._semaphore:
                    async with self._lock:
                        self._calls_started += 1
                    consumers = await redis.xinfo_consumers(
                        stream_key,
                        group_name,
                    )
            return {
                "available": True,
                "consumers": consumers,
                "warning_code": None,
            }
        except TimeoutError as exc:
            structured_log(
                "warning",
                "consumer_group_member_metrics_timeout",
                stream=stream_key,
                group=group_name,
                exception=exc,
            )
            return {
                "available": False,
                "consumers": None,
                "warning_code": (
                    "consumer_group_observation_capacity_exhausted"
                ),
            }
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            structured_log(
                "warning",
                "consumer_group_member_metrics_unavailable",
                stream=stream_key,
                group=group_name,
                exception=exc,
            )
            return {
                "available": False,
                "consumers": None,
                "warning_code": (
                    "consumer_group_consumer_metrics_unavailable"
                ),
            }

    def snapshot(self) -> dict:
        return {
            "max_calls": self._max_calls,
            "max_concurrency": self._max_concurrency,
            "calls_started": self._calls_started,
            "calls_reserved": self._max_calls - self._remaining,
            "calls_refused": self._refused,
            "exhausted": self._refused > 0,
        }


def _intent_observation(row):
    item = dict(row)
    # Remote references are written from third-party responses.  Even a
    # superficially harmless token may embed a session identifier, so the
    # general metrics API exposes presence only.  A future platform-specific
    # reconciliation flow can define a strict typed remote-reference schema.
    item.pop("remote_ref", None)
    item["remote_ref_redacted"] = bool(item.pop("has_remote_ref", 0))
    item["reconciliation_required"] = bool(item.get("reconciliation_required"))
    item["has_error"] = bool(item.get("has_error"))
    return item


async def _real_run_inflight_counts():
    row = await database.fetch_one(
        """SELECT COALESCE(SUM(status = 'queued'), 0) AS queued,
                  COALESCE(SUM(status = 'running'), 0) AS running
           FROM task_runs
           WHERE task_mode = 'real_run'"""
    )
    return {
        "queued": int(row["queued"] or 0) if row else 0,
        "running": int(row["running"] or 0) if row else 0,
    }


def _worker_gate_contract():
    return {
        "authoritative_source": (
            "process_env.REAL_RUN_ENABLED AND runtime_settings.real_run_enabled"
        ),
        "process_capability_required": True,
        "recheck_points": ["before_task_claim", "before_each_external_mutation"],
        "setting_change_cancels_tasks": False,
    }


def _real_run_technical_prerequisite_blocker_codes(
    readiness_snapshot: dict,
) -> list[str]:
    """Validate and return failed technical P0 codes, fail-closed on drift."""

    checks = (
        readiness_snapshot.get("production_checks")
        if isinstance(readiness_snapshot, dict)
        else None
    )
    if not isinstance(checks, list):
        return ["production_readiness_observation_unavailable"]

    blocker_codes = []
    observed_technical_codes = set()
    observed_p0_codes = set()
    for check in checks:
        if not isinstance(check, dict):
            return [REAL_RUN_READINESS_CONTRACT_INVALID_BLOCKER_CODE]
        if str(check.get("priority") or "").upper() != "P0":
            continue
        raw_code = check.get("code")
        if not isinstance(raw_code, str) or not raw_code.strip():
            return [REAL_RUN_READINESS_CONTRACT_INVALID_BLOCKER_CODE]
        code = raw_code.strip()
        if code in observed_p0_codes:
            return [REAL_RUN_READINESS_CONTRACT_INVALID_BLOCKER_CODE]
        observed_p0_codes.add(code)
        if code in REAL_RUN_FINAL_AUTHORIZATION_GATE_CODES:
            continue
        if code not in REAL_RUN_REQUIRED_TECHNICAL_P0_CHECK_CODES:
            return [REAL_RUN_READINESS_CONTRACT_INVALID_BLOCKER_CODE]
        if not isinstance(check.get("passed"), bool):
            return [REAL_RUN_READINESS_CONTRACT_INVALID_BLOCKER_CODE]
        observed_technical_codes.add(code)
        if check["passed"] is not True:
            blocker_codes.append(code)

    if observed_technical_codes != REAL_RUN_REQUIRED_TECHNICAL_P0_CHECK_CODES:
        return [REAL_RUN_READINESS_CONTRACT_INVALID_BLOCKER_CODE]
    return blocker_codes


def weibo_oauth_capability_summary(rows, *, now: datetime | None = None) -> dict:
    """Summarize generic platform readiness without overstating partial grants."""

    evaluation_now = now or datetime.now(timezone.utc)
    actions = action_order_for_platform("weibo")
    action_accounts = {action: 0 for action in actions}
    any_action_accounts = 0
    full_action_accounts = 0
    for row in rows:
        ready_actions = []
        for action in actions:
            attestation = validate_weibo_oauth_capability_attestation(
                row["result"],
                required_actions=(action,),
                account_id=int(row["id"]),
                execution_revision=int(row["execution_revision"] or 0),
                calibration_fresh=bool(row["calibration_fresh"]),
                now=evaluation_now,
            )
            if attestation["ready"]:
                ready_actions.append(action)
                action_accounts[action] += 1
        any_action_accounts += int(bool(ready_actions))
        full_attestation = validate_weibo_oauth_capability_attestation(
            row["result"],
            required_actions=actions,
            account_id=int(row["id"]),
            execution_revision=int(row["execution_revision"] or 0),
            calibration_fresh=bool(row["calibration_fresh"]),
            now=evaluation_now,
        )
        full_action_accounts += int(full_attestation["ready"])
    return {
        "full_action_accounts": full_action_accounts,
        "any_action_accounts": any_action_accounts,
        "action_accounts": action_accounts,
    }


async def _collect_transport_metrics_for_request():
    """Share one extra-group budget across every transport subsystem."""

    consumer_group_budget = _ConsumerGroupObservationBudget()
    task_stream_metrics, control_stream_metrics = await asyncio.gather(
        collect_task_stream_metrics(
            consumer_group_budget=consumer_group_budget,
        ),
        collect_control_stream_metrics(
            consumer_group_budget=consumer_group_budget,
        ),
    )
    return (
        task_stream_metrics,
        control_stream_metrics,
        consumer_group_budget,
    )


def _empty_global_circuit_breaker_status(*, available: bool) -> dict:
    return {
        "available": available,
        "status": "unknown",
        "reason": None,
        "opened_at": None,
        "updated_at": None,
        "allows_real_run": False,
    }


async def _global_circuit_breaker_runtime_status() -> dict:
    """Return a bounded, fail-closed projection of the global breaker."""

    try:
        row = await asyncio.wait_for(
            database.fetch_one(
                """SELECT status, reason, opened_at, updated_at
                     FROM circuit_breakers
                    WHERE scope = 'global'"""
            ),
            timeout=TASK_METRICS_OPERATION_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        structured_log(
            "warning",
            "global_circuit_breaker_status_unavailable",
            cause_type=type(exc).__name__,
        )
        return _empty_global_circuit_breaker_status(available=False)
    if not row:
        return _empty_global_circuit_breaker_status(available=True)

    status = str(row["status"] or "").strip().casefold()
    if status not in {"closed", "open", "half_open"}:
        status = "unknown"
    return {
        "available": True,
        "status": status,
        "reason": row["reason"],
        "opened_at": row["opened_at"],
        "updated_at": row["updated_at"],
        "allows_real_run": status == "closed",
    }


def _autopilot_allowlist(summary_or_status: dict | None) -> tuple[str, ...]:
    """Return only the authenticated, validated Autopilot platform scope."""

    source = summary_or_status if isinstance(summary_or_status, dict) else {}
    autopilot = source.get("autopilot", source)
    if not isinstance(autopilot, dict):
        return ()
    raw_allowlist = autopilot.get("platform_allowlist")
    if (
        autopilot.get("platform_allowlist_valid") is not True
        or not isinstance(raw_allowlist, list)
        or not raw_allowlist
    ):
        return ()
    normalized = [
        str(platform).strip().casefold()
        for platform in raw_allowlist
        if isinstance(platform, str)
    ]
    if (
        len(normalized) != len(raw_allowlist)
        or any(platform not in PLATFORM_IDS for platform in normalized)
        or len(set(normalized)) != len(normalized)
    ):
        return ()
    return tuple(sorted(normalized))


def _autopilot_scope_items(platforms, summary: dict) -> list[dict]:
    """Scope automatic-production gates to the declared Autopilot allowlist.

    Direct legacy callers that do not provide any Autopilot observation retain
    their historical all-platform view. The live readiness endpoint always
    supplies an observation and therefore fails closed to an empty scope when
    the allowlist is unavailable or invalid.
    """

    if "autopilot" not in summary:
        return list(platforms)
    allowlist = set(_autopilot_allowlist(summary))
    return [
        item
        for item in platforms
        if str(item.get("platform") or "").strip().casefold() in allowlist
    ]


def _autopilot_scope_resolution(platforms, summary: dict) -> dict:
    autopilot = summary.get("autopilot")
    raw_allowlist = (
        autopilot.get("platform_allowlist")
        if isinstance(autopilot, dict)
        else []
    )
    declared_raw = sorted({
        str(platform).strip().casefold()
        for platform in (
            raw_allowlist if isinstance(raw_allowlist, list) else []
        )
        if isinstance(platform, str) and str(platform).strip()
    })
    declared = list(_autopilot_allowlist(summary))
    resolved_items = _autopilot_scope_items(platforms, summary)
    resolved = sorted({
        str(item.get("platform") or "").strip().casefold()
        for item in resolved_items
        if str(item.get("platform") or "").strip()
    })
    missing = sorted(set(declared_raw) - set(resolved))
    unexpected = sorted(set(resolved) - set(declared_raw))
    complete = bool(
        declared
        and declared_raw == declared
        and resolved == declared
        and len(resolved_items) == len(declared)
        and not missing
        and not unexpected
    )
    return {
        "complete": complete,
        "declared_platforms": declared_raw,
        "resolved_platforms": resolved,
        "missing_platforms": missing,
        "unexpected_platforms": unexpected,
        "declared_count": len(declared_raw),
        "resolved_count": len(resolved),
    }


def _empty_notification_delivery_status(
    *,
    available: bool,
    configured_channels: tuple[str, ...],
    blocker_code: str | None,
) -> dict:
    return {
        "available": available,
        "blocker_code": blocker_code,
        "window_hours": NOTIFICATION_DELIVERY_WINDOW_HOURS,
        "configured_channels": list(configured_channels),
        "sent_count_24h": 0,
        "last_success_at": None,
        "last_success_channel": None,
        "ready": False,
    }


async def _notification_delivery_status(channel_ids) -> dict:
    """Read recent delivery proof for currently configured real channels."""

    configured = tuple(sorted({
        str(channel).strip().casefold()
        for channel in channel_ids or ()
        if str(channel).strip().casefold() in VALID_NOTIFICATION_CHANNELS
    }))
    if not configured:
        return _empty_notification_delivery_status(
            available=True,
            configured_channels=(),
            blocker_code="notification_channel_not_configured",
        )

    revision_pairs = []
    values: dict[str, object] = {}
    try:
        current_revisions = await asyncio.wait_for(
            asyncio.gather(*(
                notification_config_revision(channel)
                for channel in configured
            )),
            timeout=TASK_METRICS_OPERATION_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        structured_log(
            "warning",
            "notification_config_revision_unavailable",
            cause_type=type(exc).__name__,
        )
        return _empty_notification_delivery_status(
            available=False,
            configured_channels=configured,
            blocker_code="notification_config_revision_unavailable",
        )
    if any(
        not isinstance(revision, str) or not revision
        for revision in current_revisions
    ):
        return _empty_notification_delivery_status(
            available=False,
            configured_channels=configured,
            blocker_code="notification_config_revision_unavailable",
        )
    for index, (channel, revision) in enumerate(
        zip(configured, current_revisions, strict=True)
    ):
        channel_key = f"notification_channel_{index}"
        revision_key = f"notification_revision_{index}"
        revision_pairs.append(
            f"(channel = :{channel_key} AND config_revision = :{revision_key})"
        )
        values[channel_key] = channel
        values[revision_key] = revision
    try:
        row = await asyncio.wait_for(
            database.fetch_one(
                f"""SELECT channel, created_at,
                           COUNT(*) OVER() AS sent_count_24h
                      FROM notify_logs
                     WHERE success = 1
                       AND ({' OR '.join(revision_pairs)})
                       AND channel NOT IN ('manual', 'dispatch')
                       AND created_at >= DATE_SUB(
                             UTC_TIMESTAMP(),
                             INTERVAL {NOTIFICATION_DELIVERY_WINDOW_HOURS} HOUR
                           )
                       AND created_at <= UTC_TIMESTAMP()
                     ORDER BY created_at DESC, id DESC
                     LIMIT 1""",
                values,
            ),
            timeout=TASK_METRICS_OPERATION_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        structured_log(
            "warning",
            "notification_delivery_status_unavailable",
            cause_type=type(exc).__name__,
        )
        return _empty_notification_delivery_status(
            available=False,
            configured_channels=configured,
            blocker_code="notification_delivery_status_unavailable",
        )
    if not row:
        return _empty_notification_delivery_status(
            available=True,
            configured_channels=configured,
            blocker_code="notification_recent_success_required",
        )

    sent_count = max(int(row["sent_count_24h"] or 0), 0)
    last_channel = str(row["channel"] or "").strip().casefold()
    ready = bool(sent_count > 0 and last_channel in configured)
    return {
        "available": True,
        "blocker_code": (
            None if ready else "notification_recent_success_required"
        ),
        "window_hours": NOTIFICATION_DELIVERY_WINDOW_HOURS,
        "configured_channels": list(configured),
        "sent_count_24h": sent_count,
        "last_success_at": row["created_at"] if ready else None,
        "last_success_channel": last_channel if ready else None,
        "ready": ready,
    }


def _empty_autopilot_target_readiness(
    *,
    available: bool,
    scope_configured: bool,
    blocker_code: str | None,
) -> dict:
    return {
        "available": available,
        "scope_configured": scope_configured,
        "blocker_code": blocker_code,
        "pending_targets": 0,
        "observed_targets": 0,
        "observation_limit": AUTOPILOT_TARGET_READINESS_LIMIT,
        "observation_truncated": False,
        "plan_required_targets": 0,
        "plan_ready_targets": 0,
        "eligible_targets": 0,
        "eligible_by_platform": {},
        "missing_plan_targets": 0,
        "missing_plan_target_ids": [],
        "plan_blocker_counts": {},
        "exact_real_candidate": {
            "available": False,
            "ready": False,
            "blocker_code": (
                blocker_code
                or "autopilot_exact_candidate_observation_unavailable"
            ),
            "candidate_count": 0,
            "candidate": None,
            "observed_targets": 0,
            "observation_limit": AUTOPILOT_TARGET_READINESS_LIMIT,
            "observation_truncated": False,
            "account_candidate_truncated_platforms": [],
            "blocker_counts": {},
        },
    }


async def _autopilot_target_readiness(autopilot_status: dict) -> dict:
    """Evaluate target plan prerequisites with one bounded, read-only batch.

    This deliberately evaluates Action Plan v2 and the authoritative rule
    snapshot through the same validators used by dispatch. It does not select
    accounts, grant authorization, or mutate a target.
    """

    allowlist = _autopilot_allowlist(autopilot_status)
    if not allowlist:
        return _empty_autopilot_target_readiness(
            available=True,
            scope_configured=False,
            blocker_code="autopilot_platform_scope_invalid",
        )

    placeholders = []
    values: dict[str, object] = {
        "target_limit": AUTOPILOT_TARGET_READINESS_LIMIT + 1,
    }
    for index, platform in enumerate(allowlist):
        key = f"autopilot_platform_{index}"
        placeholders.append(f":{key}")
        values[key] = platform
    query = f"""SELECT l.*,
                         {EXACT_REAL_CANDIDATE_TARGET_METRICS_SQL},
                         COUNT(*) OVER() AS scoped_pending_count
                  FROM lotteries l
                 WHERE l.platform IN ({', '.join(placeholders)})
                   AND l.status IN ('pending', 'claimed')
                   AND (l.expires_at IS NULL OR l.expires_at > UTC_TIMESTAMP())
                  ORDER BY l.value_score DESC, l.id ASC
                 LIMIT :target_limit"""
    try:
        loaded_rows = await asyncio.wait_for(
            database.fetch_all(query, values),
            timeout=TASK_METRICS_OPERATION_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        structured_log(
            "warning",
            "autopilot_target_readiness_unavailable",
            cause_type=type(exc).__name__,
        )
        return _empty_autopilot_target_readiness(
            available=False,
            scope_configured=True,
            blocker_code="autopilot_target_readiness_unavailable",
        )

    rows = [dict(row) for row in loaded_rows]
    pending_targets = int(
        rows[0].get("scoped_pending_count") or 0
    ) if rows else 0
    observation_truncated = pending_targets > AUTOPILOT_TARGET_READINESS_LIMIT
    observed_rows = rows[:AUTOPILOT_TARGET_READINESS_LIMIT]
    async def observe_exact_real_candidate():
        try:
            return await asyncio.wait_for(
                evaluate_exact_real_candidate_observation(
                    observed_rows,
                    observation_limit=AUTOPILOT_TARGET_READINESS_LIMIT,
                    source_observation_truncated=observation_truncated,
                ),
                timeout=max(
                    AUTOPILOT_EXACT_CANDIDATE_TIMEOUT_SECONDS,
                    0.001,
                ),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            structured_log(
                "warning",
                "autopilot_exact_candidate_observation_unavailable",
                cause_type=type(exc).__name__,
            )
            return {
                "available": False,
                "ready": False,
                "blocker_code": (
                    "autopilot_exact_candidate_observation_unavailable"
                ),
                "candidate_count": 0,
                "candidate": None,
                "observed_targets": len(observed_rows),
                "observation_limit": AUTOPILOT_TARGET_READINESS_LIMIT,
                "observation_truncated": observation_truncated,
                "account_candidate_truncated_platforms": [],
                "blocker_counts": {},
            }

    # Plan-only evidence and exact account evidence are independent read
    # projections. Run them concurrently so fail-closed DB deadlines cannot
    # stack past the dashboard's request timeout.
    exact_candidate_task = asyncio.create_task(
        observe_exact_real_candidate()
    )

    async def cancel_exact_candidate_task():
        if not exact_candidate_task.done():
            exact_candidate_task.cancel()
        await asyncio.gather(
            exact_candidate_task,
            return_exceptions=True,
        )
    plan_rows = [
        row
        for row in observed_rows
        if str(row.get("platform") or "").strip().casefold()
        in PLAN_REQUIRED_VALIDATION_PLATFORMS
    ]
    non_plan_rows = [
        row
        for row in observed_rows
        if str(row.get("platform") or "").strip().casefold()
        not in PLAN_REQUIRED_VALIDATION_PLATFORMS
    ]
    non_plan_eligible = len(non_plan_rows)
    eligible_by_platform: dict[str, int] = {}
    for row in non_plan_rows:
        platform = str(row.get("platform") or "").strip().casefold()
        if platform in PLATFORM_IDS:
            eligible_by_platform[platform] = (
                eligible_by_platform.get(platform, 0) + 1
            )
    if not plan_rows:
        exact_real_candidate = await exact_candidate_task
        result = _empty_autopilot_target_readiness(
            available=True,
            scope_configured=True,
            blocker_code=None,
        )
        result.update({
            "pending_targets": pending_targets,
            "observed_targets": len(observed_rows),
            "observation_truncated": observation_truncated,
            "eligible_targets": non_plan_eligible,
            "eligible_by_platform": dict(
                sorted(eligible_by_platform.items())
            ),
            "exact_real_candidate": exact_real_candidate,
        })
        return result

    try:
        evidence_batch = await asyncio.wait_for(
            load_account_scoped_real_run_readiness_batch(
                plan_rows,
                account_ids=(),
            ),
            timeout=TASK_METRICS_OPERATION_TIMEOUT_SECONDS,
        )
    except asyncio.CancelledError:
        await cancel_exact_candidate_task()
        raise
    except Exception as exc:
        structured_log(
            "warning",
            "autopilot_target_plan_evidence_unavailable",
            cause_type=type(exc).__name__,
        )
        exact_real_candidate = await exact_candidate_task
        result = _empty_autopilot_target_readiness(
            available=False,
            scope_configured=True,
            blocker_code="autopilot_target_plan_evidence_unavailable",
        )
        result.update({
            "pending_targets": pending_targets,
            "observed_targets": len(observed_rows),
            "observation_truncated": observation_truncated,
            "plan_required_targets": len(plan_rows),
            "exact_real_candidate": exact_real_candidate,
        })
        return result

    plan_ready_targets = 0
    missing_plan_target_ids: list[int] = []
    blocker_counts: dict[str, int] = {}
    for row in plan_rows:
        try:
            readiness = await validate_real_run_evidence(
                row,
                account_id=None,
                evidence_batch=evidence_batch,
            )
        except asyncio.CancelledError:
            await cancel_exact_candidate_task()
            raise
        except Exception as exc:
            structured_log(
                "warning",
                "autopilot_target_plan_evaluation_failed",
                lottery_id=row.get("id"),
                platform=row.get("platform"),
                cause_type=type(exc).__name__,
            )
            readiness = {
                "action_plan_ready": False,
                "rule_snapshot_ready": False,
                "blockers": ["autopilot_target_plan_evaluation_failed"],
            }
        plan_ready = bool(
            readiness.get("action_plan_ready") is True
            and readiness.get("rule_snapshot_ready") is True
        )
        if plan_ready:
            plan_ready_targets += 1
            platform = str(row.get("platform") or "").strip().casefold()
            if platform in PLATFORM_IDS:
                eligible_by_platform[platform] = (
                    eligible_by_platform.get(platform, 0) + 1
                )
            continue
        if len(missing_plan_target_ids) < 20:
            try:
                missing_plan_target_ids.append(int(row.get("id")))
            except (TypeError, ValueError):
                pass
        raw_blockers = readiness.get("blockers")
        blockers = raw_blockers if isinstance(raw_blockers, list) else []
        relevant = [
            str(code)
            for code in blockers
            if (
                str(code).startswith("action_plan_")
                or str(code).startswith("lottery_action_plan_")
                or str(code).startswith("lottery_rule_")
                or str(code).startswith("authoritative_rule_")
                or str(code) == "autopilot_target_plan_evaluation_failed"
            )
        ] or ["target_plan_prerequisite_not_ready"]
        for code in dict.fromkeys(relevant):
            blocker_counts[code] = blocker_counts.get(code, 0) + 1

    exact_real_candidate = await exact_candidate_task
    result = _empty_autopilot_target_readiness(
        available=True,
        scope_configured=True,
        blocker_code=None,
    )
    result.update({
        "pending_targets": pending_targets,
        "observed_targets": len(observed_rows),
        "observation_truncated": observation_truncated,
        "plan_required_targets": len(plan_rows),
        "plan_ready_targets": plan_ready_targets,
        "eligible_targets": non_plan_eligible + plan_ready_targets,
        "eligible_by_platform": dict(sorted(eligible_by_platform.items())),
        "missing_plan_targets": len(plan_rows) - plan_ready_targets,
        "missing_plan_target_ids": missing_plan_target_ids,
        "plan_blocker_counts": dict(sorted(blocker_counts.items())),
        "exact_real_candidate": exact_real_candidate,
    })
    return result



@router.get("/overview")

async def metrics_overview():

    (
        task_stream_metrics,
        control_stream_metrics,
        consumer_group_budget,
    ) = await _collect_transport_metrics_for_request()
    workers_online = task_stream_metrics["workers_online"]
    heartbeat_workers_online = task_stream_metrics[
        "worker_heartbeats_online"
    ]
    workers_online = max(workers_online, heartbeat_workers_online)

    accounts = await database.fetch_all("SELECT status, COUNT(*) as cnt FROM accounts GROUP BY status")

    status_map = {r["status"]: r["cnt"] for r in accounts}

    today_count = await redis.get("daily_limit:total") or 0

    mem = psutil.virtual_memory()



    return {

        "pending": task_stream_metrics["pending"],

        "pending_available": task_stream_metrics["pending_available"],

        "pending_by_platform": task_stream_metrics["pending_by_platform"],

        "lag_by_platform": task_stream_metrics["lag_by_platform"],

        "task_stream_length": task_stream_metrics["length"],

        "task_stream_length_available": task_stream_metrics[
            "length_available"
        ],

        "task_stream_length_by_platform": task_stream_metrics[
            "length_by_platform"
        ],

        "task_outbox_undelivered": task_stream_metrics[
            "outbox_undelivered"
        ],

        "task_outbox_undelivered_available": task_stream_metrics[
            "outbox_undelivered_available"
        ],

        "task_outbox_undelivered_by_platform": task_stream_metrics[
            "outbox_undelivered_by_platform"
        ],

        "task_outbox_stale_by_platform": task_stream_metrics[
            "outbox_stale_by_platform"
        ],

        "task_outbox_stale_after_seconds": task_stream_metrics[
            "outbox_stale_after_seconds"
        ],

        "task_transport_by_platform": task_stream_metrics[
            "transport_by_platform"
        ],

        "workers_online_by_platform": task_stream_metrics[
            "workers_online_by_platform"
        ],

        "legacy_pending": task_stream_metrics["legacy_pending"],

        "legacy_lag": task_stream_metrics["legacy_lag"],

        "legacy_outbox_undelivered": task_stream_metrics[
            "legacy_outbox_undelivered"
        ],

        "legacy_drain_complete": task_stream_metrics["legacy_drain_complete"],

        "task_streams": task_stream_metrics["task_streams"],

        "control_transport_by_platform": control_stream_metrics[
            "by_platform"
        ],

        "adapter_probe_transport_by_platform": control_stream_metrics[
            "adapter_probe"
        ]["by_platform"],

        "account_calibration_transport_by_platform": (
            control_stream_metrics["account_calibration"]["by_platform"]
        ),

        "discovery_scan_transport_by_platform": (
            control_stream_metrics.get("discovery_scan") or {}
        ).get("by_platform", {}),

        "redis_consumer_group_retention_alerts": [
            *(task_stream_metrics.get("consumer_group_retention_alerts") or ()),
            *(
                control_stream_metrics.get(
                    "consumer_group_retention_alerts"
                )
                or ()
            ),
        ],

        "redis_consumer_group_observation_budget": (
            consumer_group_budget.snapshot()
        ),

        "legacy_control_streams": {
            "adapter_probe": control_stream_metrics["adapter_probe"][
                "legacy"
            ],
            "account_calibration": control_stream_metrics[
                "account_calibration"
            ]["legacy"],
        },

        "legacy_control_stream_drain_complete": control_stream_metrics[
            "legacy_control_stream_drain_complete"
        ],

        "stale_running": task_stream_metrics["stale_running"],

        "workers_online": workers_online,

        "stream_consumer_metrics_available": task_stream_metrics[
            "workers_online_available"
        ],

        "worker_heartbeat_metrics_available": task_stream_metrics[
            "worker_heartbeats_available"
        ],

        "accounts_ready": status_map.get("ready", 0),

        "accounts_cooling": status_map.get("cooling", 0),

        "accounts_frozen": status_map.get("frozen", 0),

        "today_tasks": int(today_count),

        "memory_mb": round(mem.used / (1024*1024)),

        "memory_percent": mem.percent,

    }


@router.get("/prometheus", response_class=PlainTextResponse)
async def metrics_prometheus(request: Request):
    """Expose a bounded Prometheus text projection for external alerting."""

    if not settings.prometheus_enabled:
        raise HTTPException(status_code=404, detail="prometheus_metrics_disabled")
    require_min_role(request, "viewer")
    payload = await metrics_overview()
    lines = _prometheus_lines(payload)
    return "\n".join(lines) + ("\n" if lines else "")


@router.get("/readiness")
async def readiness():
    return await _readiness_snapshot()


async def _readiness_snapshot():

    (
        task_stream_metrics,
        control_stream_metrics,
        consumer_group_budget,
    ) = await _collect_transport_metrics_for_request()
    workers_online = task_stream_metrics["workers_online"]
    heartbeat_workers_online = task_stream_metrics[
        "worker_heartbeats_online"
    ]
    workers_online = max(workers_online, heartbeat_workers_online)
    (
        real_run_enabled,
        global_circuit_breaker,
        autopilot_status,
    ) = await asyncio.gather(
        is_real_run_enabled(),
        _global_circuit_breaker_runtime_status(),
        _autopilot_runtime_status(),
    )
    global_breaker_allows_real_run = bool(
        global_circuit_breaker.get("allows_real_run")
    )

    selector_config = await load_runtime_selector_config()
    platforms = []

    for platform, cfg in get_platforms().items():

        task_transport = task_stream_metrics[
            "transport_by_platform"
        ].get(
            platform,
            unavailable_platform_task_transport(platform),
        )
        if not cfg or cfg.get("module_available") is False:
            platforms.append(
                unavailable_platform_readiness_item(
                    platform,
                    cfg or {},
                    task_transport,
                    blocker_code="platform_module_unavailable",
                )
            )
            continue

        dry_run_supported = cfg.get("execution_mode") != "manual_assisted"

        try:
            safe_accounts = await asyncio.wait_for(
                database.fetch_one(

                    """SELECT COUNT(*) AS cnt FROM accounts a
                       WHERE a.platform = :platform
                         AND a.status = 'ready'
                         AND OCTET_LENGTH(a.encrypted_credential) > 0
                         AND (
                           SELECT c.status FROM account_calibrations c
                           WHERE c.account_id = a.id
                           ORDER BY c.created_at DESC
                           LIMIT 1
                         ) = 'succeeded'""",

                    {"platform": platform},
                ),
                timeout=TASK_METRICS_OPERATION_TIMEOUT_SECONDS,

            )

            latest_probe = await asyncio.wait_for(
                database.fetch_one(

                    """SELECT status, result, error_message, created_at
                       FROM adapter_calibrations
                       WHERE platform = :platform
                       ORDER BY id DESC
                       LIMIT 1""",

                    {"platform": platform},
                ),
                timeout=TASK_METRICS_OPERATION_TIMEOUT_SECONDS,

            )
            if safe_accounts is None:
                raise RuntimeError("platform_safe_account_count_missing")
        except Exception as exc:
            structured_log(
                "warning",
                "platform_readiness_database_unavailable",
                platform=platform,
                exception=exc,
            )
            platforms.append(
                unavailable_platform_readiness_item(
                    platform,
                    cfg,
                    task_transport,
                    blocker_code=(
                        "platform_readiness_database_unavailable"
                    ),
                )
            )
            continue

        probe_result = parse_json_field(latest_probe["result"]) if latest_probe and latest_probe["result"] else None

        probe_summary = probe_result.get("_summary") if isinstance(probe_result, dict) else None

        try:
            runtime_adapter_ready = platform_selectors_complete(
                selector_config,
                platform,
            )
            adapter_kind = platform_real_adapter_kind(
                selector_config,
                platform,
            )
            action_adapter_enabled = bool(
                cfg.get("action_adapter")
            ) or platform_has_runtime_real_adapter(
                selector_config,
                platform,
            )
            oauth_capability_accounts = 0
            oauth_any_capability_accounts = 0
            oauth_capability_actions = (
                {
                    action: 0
                    for action in action_order_for_platform("weibo")
                }
                if platform == "weibo"
                else {}
            )
            if platform == "weibo":
                oauth_rows = await asyncio.wait_for(
                    database.fetch_all(
                        """SELECT a.id, a.execution_revision, c.result,
                                  (c.created_at >= DATE_SUB(NOW(), INTERVAL 24 HOUR)) AS calibration_fresh
                             FROM accounts a
                             JOIN account_calibrations c
                               ON c.id = (
                                 SELECT latest.id
                                 FROM account_calibrations latest
                                 WHERE latest.account_id = a.id
                                   AND latest.platform = 'weibo'
                                 ORDER BY latest.id DESC
                                 LIMIT 1
                               )
                            WHERE a.platform = 'weibo'
                              AND a.status = 'ready'
                              AND a.deleted_at IS NULL
                              AND OCTET_LENGTH(a.encrypted_credential) > 0
                              AND c.status = 'succeeded'"""
                    ),
                    timeout=TASK_METRICS_OPERATION_TIMEOUT_SECONDS,
                )
                capability_summary = weibo_oauth_capability_summary(
                    oauth_rows
                )
                oauth_capability_accounts = capability_summary[
                    "full_action_accounts"
                ]
                oauth_any_capability_accounts = capability_summary[
                    "any_action_accounts"
                ]
                oauth_capability_actions = capability_summary[
                    "action_accounts"
                ]
            probe_ready = (
                oauth_capability_accounts > 0
                if adapter_kind == "oauth"
                else platform_probe_ready_for_real_actions(
                    platform,
                    probe_summary,
                )
            )
        except Exception as exc:
            structured_log(
                "warning",
                "platform_readiness_capability_unavailable",
                platform=platform,
                exception=exc,
            )
            platforms.append(
                unavailable_platform_readiness_item(
                    platform,
                    cfg,
                    task_transport,
                    blocker_code=(
                        "platform_readiness_capability_unavailable"
                    ),
                )
            )
            continue
        real_actions_ready = action_adapter_enabled and probe_ready

        blockers = []
        blocker_codes = []
        task_transport_blockers = list(
            task_transport.get("blocker_codes") or ()
        )

        if not safe_accounts["cnt"]:

            blockers.append("no calibrated ready account")
            blocker_codes.append("no_calibrated_ready_account")

        if not action_adapter_enabled:

            blockers.append("real adapter not enabled")
            blocker_codes.append("real_adapter_not_enabled")

        if adapter_kind == "oauth" and not oauth_capability_accounts:

            blockers.append("official OAuth capability evidence is missing")
            blocker_codes.append("weibo_oauth_capability_evidence_required")

        elif action_adapter_enabled and not real_actions_ready:

            blockers.append("adapter probe is incomplete")
            blocker_codes.append("adapter_probe_incomplete")

        if not real_run_enabled:

            blockers.append("global real-run switch is disabled")
            blocker_codes.append("global_real_run_disabled")

        if not global_breaker_allows_real_run:

            blockers.append("global circuit breaker is not closed")
            blocker_codes.append("global_circuit_breaker_not_closed")

        standard_transport_ready = bool(
            task_transport.get("standard_ready")
        )
        repair_transport_ready = bool(task_transport.get("repair_ready"))
        capability_ready_for_dry_run = (
            dry_run_supported and bool(safe_accounts["cnt"])
        )
        capability_ready_for_shadow_run = bool(safe_accounts["cnt"])
        capability_ready_for_real_run = (
            real_run_enabled
            and global_breaker_allows_real_run
            and real_actions_ready
            and bool(safe_accounts["cnt"])
        )

        platforms.append(

            {

                "platform": platform,

                "label": cfg["label"],
                "module_available": True,

                "safe_accounts": safe_accounts["cnt"],

                "qr_login": bool(cfg.get("qr_login")),

                "cookie_login": bool(cfg.get("cookie_login")),

                "adapter_status": (
                    cfg.get("adapter_status", "planned")
                    if adapter_kind in {"oauth", "manual_assisted"}
                    else (
                        "configured"
                        if action_adapter_enabled
                        else cfg.get("adapter_status", "planned")
                    )
                ),
                "adapter_kind": adapter_kind,

                "action_adapter": action_adapter_enabled,
                "selector_observation_configured": runtime_adapter_ready,
                "oauth_capability_accounts": oauth_capability_accounts,
                "oauth_any_capability_accounts": oauth_any_capability_accounts,
                "oauth_capability_actions": oauth_capability_actions,

                "real_actions_ready": real_actions_ready,

                "latest_probe": {

                    "status": latest_probe["status"],

                    "created_at": latest_probe["created_at"],

                    "ready_phase_count": probe_summary.get("ready_phase_count") if probe_summary else None,

                    "ready_for_real_actions": probe_summary.get("ready_for_real_actions") if probe_summary else False,

                    "error_message": latest_probe["error_message"],

                } if latest_probe else None,

                "blockers": blockers,
                "blocker_codes": blocker_codes,
                "task_transport": task_transport,
                "task_transport_ready": bool(task_transport.get("ready")),
                "task_transport_blocker_codes": task_transport_blockers,
                "repair_transport_ready": repair_transport_ready,

                "dry_run_supported": dry_run_supported,
                "capability_ready_for_dry_run": capability_ready_for_dry_run,
                "capability_ready_for_shadow_run": (
                    capability_ready_for_shadow_run
                ),
                "capability_ready_for_real_run": capability_ready_for_real_run,
                "ready_for_dry_run": (
                    capability_ready_for_dry_run
                    and standard_transport_ready
                ),
                "ready_for_shadow_run": (
                    capability_ready_for_shadow_run
                    and standard_transport_ready
                ),

                "ready_for_real_run": (
                    capability_ready_for_real_run
                    and standard_transport_ready
                ),
                "ready_for_repair_dispatch": (
                    capability_ready_for_real_run
                    and repair_transport_ready
                ),

            }

        )

    for item in platforms:
        platform = str(item.get("platform") or "").strip().casefold()
        control_transport = control_stream_metrics["by_platform"].get(
            platform,
            {
                "adapter_probe": None,
                "account_calibration": None,
                "available": False,
                "ready": False,
            },
        )
        item["control_transport"] = control_transport
        item["control_transport_ready"] = bool(
            control_transport.get("ready")
        )

    recent_risk = await database.fetch_one(

        """SELECT COUNT(*) AS cnt FROM risk_events
           WHERE created_at >= DATE_SUB(NOW(), INTERVAL 24 HOUR)"""

    )

    notification_channel_ids = await configured_channels()
    notification_configured = len(notification_channel_ids)
    notification_delivery = await _notification_delivery_status(
        notification_channel_ids
    )

    proxy_exits = await database.fetch_one("SELECT COUNT(*) AS cnt FROM proxies")
    active_proxy_exits = await database.fetch_one("SELECT COUNT(*) AS cnt FROM proxies WHERE status = 'active' AND (cooldown_until IS NULL OR cooldown_until < NOW())")
    proxied_safe_accounts = await database.fetch_one(
        """SELECT COUNT(*) AS cnt
           FROM accounts a
           JOIN proxies p ON p.id = a.proxy_id
           WHERE a.status = 'ready'
             AND OCTET_LENGTH(a.encrypted_credential) > 0
             AND p.status = 'active'
             AND (p.cooldown_until IS NULL OR p.cooldown_until < NOW())"""
    )
    pending_targets = await database.fetch_one(
        """SELECT COUNT(*) AS cnt
             FROM lotteries
            WHERE status IN ('pending', 'claimed')
               AND (expires_at IS NULL OR expires_at > UTC_TIMESTAMP())"""
    )
    autopilot_targets = await _autopilot_target_readiness(
        autopilot_status
    )
    autopilot_scope = _autopilot_scope_items(
        platforms,
        {"autopilot": autopilot_status},
    )
    autopilot_scope_resolution = _autopilot_scope_resolution(
        platforms,
        {"autopilot": autopilot_status},
    )
    autopilot_dry_supported = sum(
        1 for item in autopilot_scope if item.get("dry_run_supported")
    )
    autopilot_dry_ready = sum(
        1 for item in autopilot_scope if item.get("ready_for_dry_run")
    )
    autopilot_real_ready = sum(
        1 for item in autopilot_scope if item.get("ready_for_real_run")
    )
    autopilot_real_capability_ready = sum(
        1
        for item in autopilot_scope
        if (
            int(item.get("safe_accounts") or 0) > 0
            and item.get("real_actions_ready") is True
            and item.get("task_transport_ready") is True
        )
    )

    summary = {

        "platforms_total": len(platforms),

        "dry_run_supported": sum(1 for item in platforms if item["dry_run_supported"]),

        "dry_run_ready": sum(1 for item in platforms if item["ready_for_dry_run"]),

        "real_run_ready": sum(1 for item in platforms if item["ready_for_real_run"]),

        "safe_accounts_total": sum(item["safe_accounts"] for item in platforms),

        "notification_channels_configured": notification_configured,
        "notification_delivery": notification_delivery,
        "notification_recent_success_count": notification_delivery[
            "sent_count_24h"
        ],
        "notification_last_success_at": notification_delivery[
            "last_success_at"
        ],

        "recent_risk_events_24h": recent_risk["cnt"],

        "proxy_exits_total": proxy_exits["cnt"],
        "active_proxy_exits": active_proxy_exits["cnt"],
        "proxied_safe_accounts": proxied_safe_accounts["cnt"],
        "pending_targets": pending_targets["cnt"],
        "autopilot_scope_platforms": [
            item.get("platform") for item in autopilot_scope
        ],
        "autopilot_scope_platforms_total": len(autopilot_scope),
        "autopilot_scope_resolution": autopilot_scope_resolution,
        "autopilot_scope_dry_run_supported": autopilot_dry_supported,
        "autopilot_scope_dry_run_ready": autopilot_dry_ready,
        "autopilot_scope_real_run_ready": autopilot_real_ready,
        "autopilot_scope_real_action_capability_ready": (
            autopilot_real_capability_ready
        ),
        "autopilot_scope_safe_accounts": sum(
            int(item.get("safe_accounts") or 0)
            for item in autopilot_scope
        ),
        "autopilot_scope_task_transport_ready": sum(
            1
            for item in autopilot_scope
            if item.get("task_transport_ready")
        ),
        "autopilot_targets": autopilot_targets,
        "workers_online": workers_online,
        "worker_heartbeats_online": heartbeat_workers_online,
        "worker_heartbeat_metrics_available": task_stream_metrics[
            "worker_heartbeats_available"
        ],
        "workers_online_by_platform": task_stream_metrics[
            "workers_online_by_platform"
        ],
        "task_streams": task_stream_metrics["task_streams"],
        "control_transport_by_platform": control_stream_metrics[
            "by_platform"
        ],
        "adapter_probe_streams": control_stream_metrics[
            "adapter_probe"
        ]["streams"],
        "account_calibration_streams": control_stream_metrics[
            "account_calibration"
        ]["streams"],
        "discovery_scan_streams": (
            control_stream_metrics.get("discovery_scan") or {}
        ).get("streams", []),
        "legacy_control_streams": {
            "adapter_probe": control_stream_metrics["adapter_probe"][
                "legacy"
            ],
            "account_calibration": control_stream_metrics[
                "account_calibration"
            ]["legacy"],
        },
        "legacy_control_stream_drain_complete": control_stream_metrics[
            "legacy_control_stream_drain_complete"
        ],
        "task_transport_by_platform": task_stream_metrics[
            "transport_by_platform"
        ],
        "task_transport_ready": sum(
            1
            for item in platforms
            if item["task_transport_ready"]
        ),
        "task_outbox_undelivered": task_stream_metrics[
            "outbox_undelivered"
        ],
        "task_outbox_undelivered_available": task_stream_metrics[
            "outbox_undelivered_available"
        ],
        "task_outbox_undelivered_by_platform": task_stream_metrics[
            "outbox_undelivered_by_platform"
        ],
        "task_outbox_stale_by_platform": task_stream_metrics[
            "outbox_stale_by_platform"
        ],
        "task_outbox_stale_after_seconds": task_stream_metrics[
            "outbox_stale_after_seconds"
        ],
        "task_stream_pending_by_platform": task_stream_metrics[
            "pending_by_platform"
        ],
        "task_stream_pending_available": task_stream_metrics[
            "pending_available"
        ],
        "legacy_task_stream_pending": task_stream_metrics["legacy_pending"],
        "legacy_task_stream_lag": task_stream_metrics["legacy_lag"],
        "legacy_task_outbox_undelivered": task_stream_metrics[
            "legacy_outbox_undelivered"
        ],
        "legacy_task_stream_drain_complete": task_stream_metrics[
            "legacy_drain_complete"
        ],
        "redis_consumer_group_retention_alerts": [
            *(task_stream_metrics.get("consumer_group_retention_alerts") or ()),
            *(
                control_stream_metrics.get(
                    "consumer_group_retention_alerts"
                )
                or ()
            ),
        ],
        "redis_consumer_group_observation_budget": (
            consumer_group_budget.snapshot()
        ),
        "stale_running_tasks": task_stream_metrics["stale_running"],
        "deployment_real_run_enabled": bool(settings.real_run_enabled),
        "real_run_enabled": real_run_enabled,
        "global_circuit_breaker": global_circuit_breaker,
        "autopilot": autopilot_status,

    }

    production_checks = build_production_checks(platforms, summary)
    summary["production_ready"] = all(check["passed"] for check in production_checks if check["priority"] == "P0")
    strategy_advice = await build_strategy_advice(summary)

    return {

        "platforms": platforms,

        "summary": summary,

        "actions": build_next_actions(platforms, summary),
        "production_checks": production_checks,
        "strategy_advice": strategy_advice,

    }



async def log_stream():

    pubsub = redis.pubsub()

    await pubsub.subscribe("structured_logs")

    try:

        while True:

            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1)

            if message:

                yield f"data: {message['data']}\n\n"

            else:

                yield ": heartbeat\n\n"

            await asyncio.sleep(0.1)

    except asyncio.CancelledError:

        pass

    finally:

        try:

            await pubsub.unsubscribe("structured_logs")

            await pubsub.close()

        except Exception:

            pass



@router.get("/stream")

async def stream():

    return StreamingResponse(
        log_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
            "X-Content-Type-Options": "nosniff",
        },
    )



@router.get("/risk/events")

async def risk_events(limit: int = 50):

    rows = await database.fetch_all(

        "SELECT * FROM risk_events ORDER BY created_at DESC LIMIT :limit",

        {"limit": limit}

    )

    result = []
    for row in rows:
        item = dict(row)
        item["detail"] = parse_json_field(item.get("detail"))
        result.append(item)
    return result


def parse_json_field(value):

    if isinstance(value, (dict, list)) or value is None:

        return value

    if isinstance(value, bytes):

        value = value.decode("utf-8", errors="replace")

    try:

        return json.loads(value)

    except Exception:

        return value


def count_active_consumers(consumers, idle_limit_ms: int = 30000) -> int:
    active = 0
    for consumer in consumers or []:
        idle = consumer.get("idle") if isinstance(consumer, dict) else None
        pending = consumer.get("pending", 0) if isinstance(consumer, dict) else 0
        try:
            idle_ms = int(idle)
        except Exception:
            idle_ms = idle_limit_ms + 1
        try:
            pending_count = int(pending)
        except Exception:
            pending_count = 0
        if idle_ms <= idle_limit_ms or pending_count > 0:
            active += 1
    return active


def active_consumer_names(
    consumers,
    idle_limit_ms: int = 30000,
    *,
    namespace: str = "",
) -> set[str]:
    """Return active Redis consumer identities for cross-group de-duplication."""

    names = set()
    for index, consumer in enumerate(consumers or []):
        if not isinstance(consumer, dict):
            continue
        if count_active_consumers([consumer], idle_limit_ms=idle_limit_ms) == 0:
            continue
        raw_name = consumer.get("name")
        name = str(raw_name or "").strip()
        # Redis normally always returns a name. Keep an observable anonymous
        # consumer count if a proxy/client strips it, without collapsing
        # unrelated anonymous entries across groups.
        names.add(name or f"anonymous:{namespace}:{index}")
    return names


def _unavailable_consumer_group_governance() -> dict:
    return {
        "available": False,
        "groups_total": None,
        "groups_inspected": 0,
        "expected_groups": [],
        "missing_expected_groups": [],
        "unexpected_groups": [],
        "stale_groups": [],
        "xdel_blocked_groups": [],
        "retention_blocked_groups": [],
        "retention_alert": False,
        "consumer_inventory_alert": False,
        "warning_codes": ["consumer_group_inventory_unavailable"],
        "stale_consumer_entries": None,
        "groups": [],
    }


async def _consumer_group_governance_observation(
    *,
    stream_key: str,
    primary_group_name: str,
    primary_consumers,
    primary_consumers_available: bool,
    groups,
    groups_available: bool,
    stream_length: int | None,
    consumer_group_budget: _ConsumerGroupObservationBudget,
) -> dict:
    """Bound extra-group inspection so metrics cannot amplify group sprawl."""

    if not groups_available:
        return _unavailable_consumer_group_governance()
    group_rows = list(groups or ())
    consumers_by_group: dict[str, list | tuple | None] = {}
    if primary_consumers_available:
        consumers_by_group[primary_group_name] = list(
            primary_consumers or ()
        )

    names_to_observe = []
    oversized_names = set()
    if len(group_rows) <= MAX_OBSERVED_CONSUMER_GROUPS:
        for row in group_rows:
            raw_name = row.get("name") if isinstance(row, dict) else None
            name = normalized_consumer_group_name(raw_name)
            if (
                name is not None
                and name != primary_group_name
                and name not in names_to_observe
            ):
                try:
                    reported_consumers = int(row.get("consumers"))
                except (AttributeError, TypeError, ValueError):
                    reported_consumers = None
                if (
                    reported_consumers is not None
                    and reported_consumers
                    > MAX_OBSERVED_CONSUMERS_PER_GROUP
                ):
                    consumers_by_group[name] = None
                    oversized_names.add(name)
                else:
                    names_to_observe.append(name)

    forced_alert_codes: set[str] = set()
    if names_to_observe:
        observed_consumers = await asyncio.gather(
            *(
                consumer_group_budget.observe(
                    stream_key=stream_key,
                    group_name=name,
                )
                for name in names_to_observe
            )
        )
        for group_name, result in zip(
            names_to_observe,
            observed_consumers,
        ):
            consumers_by_group[group_name] = (
                result["consumers"] if result["available"] else None
            )
            warning_code = result.get("warning_code")
            if warning_code in {
                "consumer_group_observation_budget_exhausted",
                "consumer_group_observation_capacity_exhausted",
            }:
                forced_alert_codes.add(warning_code)
    observation = evaluate_consumer_group_governance(
        stream_key=stream_key,
        groups=group_rows,
        consumers_by_group=consumers_by_group,
        stale_after_milliseconds=(
            settings.redis_consumer_group_stale_seconds * 1000
        ),
        stream_length=stream_length,
    )
    if oversized_names:
        observation["available"] = False
        observation["consumer_inventory_alert"] = True
    if forced_alert_codes:
        observation["available"] = False
        observation["retention_alert"] = True
        observation["warning_codes"] = [
            *observation["warning_codes"],
            *(
                code
                for code in sorted(forced_alert_codes)
                if code not in observation["warning_codes"]
            ),
        ]
    return observation


async def _task_stream_observation(
    binding,
    *,
    excluded_consumer_names: frozenset[str] | None = None,
    consumer_group_budget: (
        _ConsumerGroupObservationBudget | None
    ) = None,
):
    if consumer_group_budget is None:
        consumer_group_budget = _ConsumerGroupObservationBudget()
    pending = None
    lag = None
    length = None
    consumers = []
    groups = []
    group = None
    pending_available = False
    consumers_available = False
    groups_available = False
    lag_available = False
    length_available = False

    try:
        length = int(
            await asyncio.wait_for(
                redis.xlen(binding.stream_key),
                timeout=TASK_METRICS_OPERATION_TIMEOUT_SECONDS,
            )
        )
        length_available = True
    except Exception as exc:
        structured_log(
            "warning",
            "task_stream_length_metrics_unavailable",
            stream=binding.stream_key,
            group=binding.group_name,
            exception=exc,
        )

    try:
        pending_info = await asyncio.wait_for(
            redis.xpending(
                binding.stream_key,
                binding.group_name,
            ),
            timeout=TASK_METRICS_OPERATION_TIMEOUT_SECONDS,
        )
        pending = int((pending_info or {}).get("pending", 0) or 0)
        pending_available = True
    except Exception as exc:
        structured_log(
            "warning",
            "task_stream_pending_metrics_unavailable",
            stream=binding.stream_key,
            group=binding.group_name,
            exception=exc,
        )

    try:
        groups = await asyncio.wait_for(
            redis.xinfo_groups(binding.stream_key),
            timeout=TASK_METRICS_OPERATION_TIMEOUT_SECONDS,
        )
        groups_available = True
        group = next(
            (
                item
                for item in groups or []
                if str(item.get("name") or "") == binding.group_name
            ),
            None,
        )
        if group is not None and group.get("lag") is not None:
            lag = int(group["lag"])
            lag_available = True
    except Exception as exc:
        structured_log(
            "warning",
            "task_stream_lag_metrics_unavailable",
            stream=binding.stream_key,
            group=binding.group_name,
            exception=exc,
        )

    try:
        reported_consumers = (
            int(group.get("consumers"))
            if group is not None and group.get("consumers") is not None
            else None
        )
    except (AttributeError, TypeError, ValueError):
        reported_consumers = None
    if (
        reported_consumers is not None
        and reported_consumers > MAX_OBSERVED_CONSUMERS_PER_GROUP
    ):
        structured_log(
            "warning",
            "task_stream_consumer_inventory_too_large",
            stream=binding.stream_key,
            group=binding.group_name,
            consumers_reported=reported_consumers,
        )
    else:
        try:
            consumers = await asyncio.wait_for(
                redis.xinfo_consumers(
                    binding.stream_key,
                    binding.group_name,
                ),
                timeout=TASK_METRICS_OPERATION_TIMEOUT_SECONDS,
            )
            consumers_available = True
        except Exception as exc:
            structured_log(
                "warning",
                "task_stream_consumer_metrics_unavailable",
                stream=binding.stream_key,
                group=binding.group_name,
                exception=exc,
            )

    excluded = (
        excluded_consumer_names
        if excluded_consumer_names is not None
        else frozenset(
            {
                LEGACY_TASK_FANOUT_CONSUMER_NAME,
                LEGACY_ADAPTER_PROBE_FANOUT_CONSUMER_NAME,
                LEGACY_ACCOUNT_CALIBRATION_FANOUT_CONSUMER_NAME,
                "recovery-daemon",
            }
        )
    )
    worker_consumers = [
        consumer
        for consumer in consumers or []
        if isinstance(consumer, dict)
        and str(consumer.get("name") or "")
        not in excluded
    ]
    consumer_group_governance = (
        await _consumer_group_governance_observation(
            stream_key=binding.stream_key,
            primary_group_name=binding.group_name,
            primary_consumers=consumers,
            primary_consumers_available=consumers_available,
            groups=groups,
            groups_available=groups_available,
            stream_length=length,
            consumer_group_budget=consumer_group_budget,
        )
    )
    observation = {
        "platform": binding.platform,
        "stream": binding.stream_key,
        "group": binding.group_name,
        "legacy": bool(getattr(binding, "legacy", False)),
        "repair": bool(getattr(binding, "repair", False)),
        "protocol_version": getattr(binding, "protocol_version", None),
        "pending": pending,
        "lag": lag,
        "length": length,
        "pending_available": pending_available,
        "consumers_available": consumers_available,
        "lag_available": lag_available,
        "length_available": length_available,
        "consumer_group_governance": consumer_group_governance,
        "available": (
            pending_available and consumers_available and lag_available
        ),
        "consumers_online": count_active_consumers(worker_consumers),
    }
    if (
        binding.platform is not None
        and not bool(getattr(binding, "legacy", False))
    ):
        # Kept private until the platform aggregate applies the exact lane
        # health contract. PEL ownership alone must never authorize dispatch.
        observation["_consumer_idle_milliseconds_by_name"] = (
            repair_consumer_idle_by_name(worker_consumers)
        )
    return observation, active_consumer_names(
        worker_consumers,
        namespace=binding.stream_key,
    )


async def _worker_heartbeat_observation(
    stale_seconds: int = 45,
) -> dict:
    """Load live process identities used to fence stale Redis consumers.

    A Redis PEL entry can keep an already-dead consumer looking active.  Lane
    readiness therefore requires both recent Redis activity and a fresh DB
    heartbeat for the exact consumer/worker identity.  The old aggregate
    worker metrics remain available separately for API compatibility.
    """

    try:
        rows = await asyncio.wait_for(
            database.fetch_all(
                """SELECT worker_id,
                          detail,
                          TIMESTAMPDIFF(
                            SECOND,
                            last_seen_at,
                            NOW()
                          ) AS heartbeat_age_seconds
                   FROM worker_heartbeats
                   WHERE service_name = 'worker'
                     AND status = 'ok'
                     AND TIMESTAMPDIFF(
                           SECOND,
                           last_seen_at,
                           NOW()
                         ) <= :stale_seconds""",
                {"stale_seconds": stale_seconds},
            ),
            timeout=TASK_METRICS_OPERATION_TIMEOUT_SECONDS,
        )
        names = set()
        for row in rows or ():
            name = str(row["worker_id"] or "").strip()
            if not name:
                raise RuntimeError("worker_heartbeat_identity_missing")
            names.add(name)
        return {
            "available": True,
            "names": names,
            "rows": list(rows or ()),
        }
    except Exception as exc:
        structured_log(
            "warning",
            "worker_heartbeat_metrics_unavailable",
            exception=exc,
        )
        return {
            "available": False,
            "names": set(),
            "rows": [],
        }


def _empty_lane_outbox_observation(
    binding,
    *,
    available: bool,
) -> dict:
    return {
        "platform": binding.platform,
        "stream": binding.stream_key,
        "repair": bool(getattr(binding, "repair", False)),
        "available": available,
        "undelivered": 0 if available else None,
        "stale_undelivered": 0 if available else None,
        "oldest_age_seconds": 0 if available else None,
    }


async def _platform_outbox_lane_observation(
    lane_bindings,
    *,
    stale_seconds: int,
) -> dict:
    """Query one platform's lanes so a slow peer cannot hide its metrics."""

    lane_bindings = tuple(lane_bindings)
    by_stream = {
        binding.stream_key: _empty_lane_outbox_observation(
            binding,
            available=True,
        )
        for binding in lane_bindings
    }
    if not lane_bindings:
        return {"available": True, "by_stream": by_stream}

    placeholders = []
    values = {"stale_seconds": int(stale_seconds)}
    for index, binding in enumerate(lane_bindings):
        name = f"task_outbox_stream_{index}"
        placeholders.append(f":{name}")
        values[name] = binding.stream_key
    try:
        rows = await asyncio.wait_for(
            database.fetch_all(
                """SELECT stream_key,
                          COUNT(*) AS undelivered,
                          COALESCE(
                            SUM(
                              updated_at <= (
                                NOW() - INTERVAL :stale_seconds SECOND
                              )
                            ),
                            0
                          ) AS stale_undelivered,
                          COALESCE(
                            MAX(
                              TIMESTAMPDIFF(
                                SECOND,
                                updated_at,
                                NOW()
                              )
                            ),
                            0
                          ) AS oldest_age_seconds
                   FROM outbox_events FORCE INDEX (
                     idx_outbox_stream_status_id
                   )
                   WHERE stream_key IN ("""
                + ", ".join(placeholders)
                + """)
                     AND status IN ('pending', 'sending')
                   GROUP BY stream_key""",
                values,
            ),
            timeout=TASK_METRICS_OPERATION_TIMEOUT_SECONDS,
        )
        for row in rows or ():
            stream_key = str(row["stream_key"] or "").strip()
            if stream_key not in by_stream:
                raise RuntimeError("task_outbox_stream_outside_scope")
            undelivered = int(row["undelivered"] or 0)
            stale_undelivered = int(row["stale_undelivered"] or 0)
            oldest_age_seconds = int(row["oldest_age_seconds"] or 0)
            if (
                undelivered < 0
                or stale_undelivered < 0
                or stale_undelivered > undelivered
                or oldest_age_seconds < 0
            ):
                raise RuntimeError("task_outbox_metrics_invalid")
            by_stream[stream_key].update(
                {
                    "undelivered": undelivered,
                    "stale_undelivered": stale_undelivered,
                    "oldest_age_seconds": oldest_age_seconds,
                }
            )
        return {
            "available": True,
            "by_stream": by_stream,
        }
    except Exception as exc:
        structured_log(
            "warning",
            "task_outbox_metrics_unavailable",
            platform=(
                lane_bindings[0].platform
                if lane_bindings
                else None
            ),
            exception=exc,
        )
        return {
            "available": False,
            "by_stream": {
                binding.stream_key: _empty_lane_outbox_observation(
                    binding,
                    available=False,
                )
                for binding in lane_bindings
            },
        }


async def _platform_outbox_observation(
    bindings,
    *,
    stale_seconds: int = TASK_OUTBOX_STALE_SECONDS,
) -> dict:
    """Count undelivered task rows with one failure domain per platform."""

    by_platform_bindings = {}
    for binding in bindings:
        if binding.platform is None or binding.legacy:
            continue
        by_platform_bindings.setdefault(binding.platform, []).append(binding)
    platform_names = tuple(by_platform_bindings)
    results = await asyncio.gather(
        *(
            _platform_outbox_lane_observation(
                by_platform_bindings[platform],
                stale_seconds=stale_seconds,
            )
            for platform in platform_names
        )
    )
    available_by_platform = {
        platform: bool(result["available"])
        for platform, result in zip(platform_names, results)
    }
    by_stream = {}
    for result in results:
        by_stream.update(result["by_stream"])
    return {
        "available": all(available_by_platform.values()),
        "available_by_platform": available_by_platform,
        "by_stream": by_stream,
    }


def unavailable_platform_task_transport(platform: str) -> dict:
    return {
        "platform": platform,
        "available": False,
        "ready": False,
        "standard_ready": False,
        "repair_ready": False,
        "workers_online": 0,
        "redis_consumers_online": 0,
        "lanes_total": 0,
        "lanes_ready": 0,
        "consumers_available": False,
        "worker_heartbeats_available": False,
        "outbox_available": False,
        "outbox_undelivered": None,
        "outbox_stale_undelivered": None,
        "outbox_oldest_age_seconds": None,
        "outbox_stale_after_seconds": TASK_OUTBOX_STALE_SECONDS,
        "blocker_codes": ["platform_task_transport_metrics_unavailable"],
        "lanes": [],
    }


def unavailable_platform_readiness_item(
    platform: str,
    cfg: dict,
    task_transport: dict,
    *,
    blocker_code: str,
) -> dict:
    """Return one failed platform without borrowing readiness from a peer."""

    normalized_blocker = str(
        blocker_code or "platform_readiness_unavailable"
    )
    return {
        "platform": platform,
        "label": cfg.get("label", platform),
        "module_available": bool(cfg.get("module_available", False)),
        "safe_accounts": 0,
        "qr_login": bool(cfg.get("qr_login")),
        "cookie_login": bool(cfg.get("cookie_login")),
        "adapter_status": cfg.get("adapter_status", "module_unavailable"),
        "adapter_kind": "unavailable",
        "action_adapter": False,
        "selector_observation_configured": False,
        "oauth_capability_accounts": 0,
        "oauth_any_capability_accounts": 0,
        "oauth_capability_actions": {},
        "real_actions_ready": False,
        "latest_probe": None,
        "blockers": [normalized_blocker],
        "blocker_codes": [normalized_blocker],
        "task_transport": task_transport,
        "task_transport_ready": bool(task_transport.get("ready")),
        "task_transport_blocker_codes": list(
            task_transport.get("blocker_codes") or ()
        ),
        "repair_transport_ready": bool(
            task_transport.get("repair_ready")
        ),
        "dry_run_supported": False,
        "capability_ready_for_dry_run": False,
        "capability_ready_for_shadow_run": False,
        "capability_ready_for_real_run": False,
        "ready_for_dry_run": False,
        "ready_for_shadow_run": False,
        "ready_for_real_run": False,
        "ready_for_repair_dispatch": False,
    }


def _task_transport_by_platform(
    observations,
    *,
    active_names_by_stream: dict[str, set[str]],
    worker_heartbeats: dict,
    outbox: dict,
) -> dict[str, dict]:
    """Build fail-closed platform health without cross-platform fallbacks."""

    platform_lanes: dict[str, list[dict]] = {}
    heartbeat_names = set(worker_heartbeats.get("names") or ())
    heartbeat_rows = list(worker_heartbeats.get("rows") or ())
    heartbeats_available = bool(worker_heartbeats.get("available"))
    outbox_by_stream = dict(outbox.get("by_stream") or {})
    binding_by_stream = {
        binding.stream_key: binding
        for binding in task_stream_bindings(include_legacy=False)
    }

    for item in observations:
        platform = item.get("platform")
        if platform is None:
            continue
        stream_key = str(item["stream"])
        redis_names = set(active_names_by_stream.get(stream_key) or ())
        verified_names = (
            redis_names & heartbeat_names if heartbeats_available else set()
        )
        lane_outbox = outbox_by_stream.get(stream_key)
        if lane_outbox is None:
            lane_outbox = {
                "available": False,
                "undelivered": None,
                "stale_undelivered": None,
                "oldest_age_seconds": None,
            }

        lane_kind = "repair" if item.get("repair") else "standard"
        blockers = []
        if not item.get("consumers_available"):
            blockers.append(
                f"{lane_kind}_task_consumer_metrics_unavailable"
            )
        elif not heartbeats_available:
            blockers.append("worker_heartbeat_metrics_unavailable")
        consumer_idle_milliseconds_by_name = dict(
            item.pop(
                "_consumer_idle_milliseconds_by_name",
                {},
            )
            or {}
        )
        active_lane_names = frozenset(
            name
            for name, idle_milliseconds in (
                consumer_idle_milliseconds_by_name.items()
            )
            if (
                idle_milliseconds
                <= REPAIR_CONSUMER_MAX_IDLE_MILLISECONDS
            )
        )
        binding = binding_by_stream.get(stream_key)
        task_lane_health_ready = bool(
            item.get("consumers_available")
            and heartbeats_available
            and binding is not None
            and worker_rows_support_task_lane(
                heartbeat_rows,
                binding=binding,
                active_consumer_names=active_lane_names,
                consumer_idle_milliseconds_by_name=(
                    consumer_idle_milliseconds_by_name
                ),
                require_repair_capability=bool(item.get("repair")),
            )
        )
        if (
            item.get("consumers_available")
            and heartbeats_available
            and not task_lane_health_ready
        ):
            if not verified_names:
                blockers.append(
                    f"{lane_kind}_task_consumer_heartbeat_missing"
                )
            blockers.append(f"{lane_kind}_task_lane_health_unready")
        consumer_group_governance = dict(
            item.get("consumer_group_governance") or {}
        )
        if consumer_group_governance.get("retention_alert"):
            blockers.append(
                f"{lane_kind}_task_consumer_group_retention_blocked"
            )
        if not lane_outbox.get("available"):
            blockers.append(f"{lane_kind}_task_outbox_metrics_unavailable")
        elif int(lane_outbox.get("stale_undelivered") or 0) > 0:
            blockers.append(f"{lane_kind}_task_outbox_stalled")

        lane_ready = not blockers
        item.update(
            {
                "consumers_online_verified": len(verified_names),
                "worker_heartbeats_available": heartbeats_available,
                "outbox_available": bool(lane_outbox.get("available")),
                "outbox_undelivered": lane_outbox.get("undelivered"),
                "outbox_stale_undelivered": lane_outbox.get(
                    "stale_undelivered"
                ),
                "outbox_oldest_age_seconds": lane_outbox.get(
                    "oldest_age_seconds"
                ),
                "transport_ready": lane_ready,
                "transport_blocker_codes": blockers,
                "task_lane_health_ready": task_lane_health_ready,
            }
        )
        platform_lanes.setdefault(str(platform), []).append(
            {
                "stream": stream_key,
                "group": item["group"],
                "repair": bool(item.get("repair")),
                "protocol_version": item.get("protocol_version"),
                "consumers_online": item["consumers_online"],
                "consumers_online_verified": len(verified_names),
                "consumers_available": item["consumers_available"],
                "outbox_available": bool(lane_outbox.get("available")),
                "outbox_undelivered": lane_outbox.get("undelivered"),
                "outbox_stale_undelivered": lane_outbox.get(
                    "stale_undelivered"
                ),
                "outbox_oldest_age_seconds": lane_outbox.get(
                    "oldest_age_seconds"
                ),
                "ready": lane_ready,
                "blocker_codes": blockers,
                "task_lane_health_ready": task_lane_health_ready,
                "consumer_group_governance": (
                    consumer_group_governance
                ),
                "_redis_names": redis_names,
                "_verified_names": verified_names,
            }
        )

    result = {}
    for platform, lanes in platform_lanes.items():
        standard_lanes = [lane for lane in lanes if not lane["repair"]]
        repair_lanes = [lane for lane in lanes if lane["repair"]]
        redis_names = set().union(
            *(lane["_redis_names"] for lane in lanes)
        )
        verified_names = set().union(
            *(lane["_verified_names"] for lane in lanes)
        )
        outbox_available = all(lane["outbox_available"] for lane in lanes)
        blockers = list(
            dict.fromkeys(
                blocker
                for lane in lanes
                for blocker in lane["blocker_codes"]
            )
        )
        public_lanes = []
        for lane in lanes:
            public_lane = dict(lane)
            public_lane.pop("_redis_names", None)
            public_lane.pop("_verified_names", None)
            public_lanes.append(public_lane)
        result[platform] = {
            "platform": platform,
            "available": bool(
                lanes
                and all(lane["consumers_available"] for lane in lanes)
                and heartbeats_available
                and outbox_available
            ),
            "ready": bool(lanes and all(lane["ready"] for lane in lanes)),
            "standard_ready": bool(
                standard_lanes
                and all(lane["ready"] for lane in standard_lanes)
            ),
            "repair_ready": bool(
                repair_lanes
                and all(lane["ready"] for lane in repair_lanes)
            ),
            "workers_online": len(verified_names),
            "redis_consumers_online": len(redis_names),
            "lanes_total": len(lanes),
            "lanes_ready": sum(1 for lane in lanes if lane["ready"]),
            "consumers_available": all(
                lane["consumers_available"] for lane in lanes
            ),
            "worker_heartbeats_available": heartbeats_available,
            "outbox_available": outbox_available,
            "outbox_undelivered": (
                sum(int(lane["outbox_undelivered"] or 0) for lane in lanes)
                if outbox_available
                else None
            ),
            "outbox_stale_undelivered": (
                sum(
                    int(lane["outbox_stale_undelivered"] or 0)
                    for lane in lanes
                )
                if outbox_available
                else None
            ),
            "outbox_oldest_age_seconds": (
                max(
                    (
                        int(lane["outbox_oldest_age_seconds"] or 0)
                        for lane in lanes
                    ),
                    default=0,
                )
                if outbox_available
                else None
            ),
            "outbox_stale_after_seconds": TASK_OUTBOX_STALE_SECONDS,
            "blocker_codes": blockers,
            "lanes": public_lanes,
        }
    return result


async def collect_task_stream_metrics(
    *,
    consumer_group_budget: (
        _ConsumerGroupObservationBudget | None
    ) = None,
) -> dict:
    """Observe every isolated lane plus the legacy drain lane.

    Metrics always include the legacy stream even when consumption is disabled.
    Operators must not infer a safe drain from missing data: the compatibility
    lane is complete only when pending and undispatched lag are both observed
    and equal to zero.
    """

    if consumer_group_budget is None:
        consumer_group_budget = _ConsumerGroupObservationBudget()
    bindings = task_stream_bindings(include_legacy=True)
    (
        observed,
        legacy_outbox,
        platform_outbox,
        stale_running,
        worker_heartbeats,
    ) = await asyncio.gather(
        asyncio.gather(
            *(
                _task_stream_observation(
                    binding,
                    consumer_group_budget=consumer_group_budget,
                )
                for binding in bindings
            )
        ),
        _legacy_outbox_observation(),
        _platform_outbox_observation(bindings),
        _stale_running_task_observation(),
        _worker_heartbeat_observation(),
    )
    observations = [item[0] for item in observed]
    active_names_by_stream = {
        item[0]["stream"]: set(item[1])
        for item in observed
    }
    active_names = set()
    for _observation, names in observed:
        active_names.update(names)
    transport_by_platform = _task_transport_by_platform(
        observations,
        active_names_by_stream=active_names_by_stream,
        worker_heartbeats=worker_heartbeats,
        outbox=platform_outbox,
    )

    platform_observations = {}
    for item in observations:
        platform = item["platform"]
        if platform is not None:
            platform_observations.setdefault(platform, []).append(item)
    legacy = next(item for item in observations if item["legacy"])
    pending_by_platform = {
        platform: (
            sum(item["pending"] for item in lane_items)
            if all(item["pending_available"] for item in lane_items)
            else None
        )
        for platform, lane_items in platform_observations.items()
    }
    lag_by_platform = {
        platform: (
            sum(item["lag"] for item in lane_items)
            if all(item["lag"] is not None for item in lane_items)
            else None
        )
        for platform, lane_items in platform_observations.items()
    }
    length_by_platform = {
        platform: (
            sum(item["length"] for item in lane_items)
            if all(item["length_available"] for item in lane_items)
            else None
        )
        for platform, lane_items in platform_observations.items()
    }
    return {
        "pending": (
            sum(item["pending"] for item in observations)
            if all(item["pending_available"] for item in observations)
            else None
        ),
        "pending_available": all(
            item["pending_available"] for item in observations
        ),
        "workers_online": len(active_names),
        "worker_heartbeats_online": len(
            worker_heartbeats.get("names") or ()
        ),
        "worker_heartbeats_available": bool(
            worker_heartbeats.get("available")
        ),
        "workers_online_available": all(
            item["consumers_available"] for item in observations
        ),
        "workers_online_by_platform": {
            platform: item["workers_online"]
            for platform, item in transport_by_platform.items()
        },
        "transport_by_platform": transport_by_platform,
        "pending_by_platform": pending_by_platform,
        "lag_by_platform": lag_by_platform,
        "length": (
            sum(item["length"] for item in observations)
            if all(item["length_available"] for item in observations)
            else None
        ),
        "length_available": all(
            item["length_available"] for item in observations
        ),
        "length_by_platform": length_by_platform,
        "outbox_undelivered": (
            sum(
                int(item["outbox_undelivered"] or 0)
                for item in transport_by_platform.values()
            )
            if platform_outbox["available"]
            else None
        ),
        "outbox_undelivered_available": bool(
            platform_outbox["available"]
        ),
        "outbox_undelivered_by_platform": {
            platform: item["outbox_undelivered"]
            for platform, item in transport_by_platform.items()
        },
        "outbox_stale_by_platform": {
            platform: item["outbox_stale_undelivered"]
            for platform, item in transport_by_platform.items()
        },
        "outbox_stale_after_seconds": TASK_OUTBOX_STALE_SECONDS,
        "consumer_group_retention_alerts": [
            {
                "stream": item["stream"],
                "platform": item["platform"],
                "repair": bool(item.get("repair")),
                "retention_alert": bool(
                    item["consumer_group_governance"].get(
                        "retention_alert"
                    )
                ),
                "consumer_inventory_alert": bool(
                    item["consumer_group_governance"].get(
                        "consumer_inventory_alert"
                    )
                ),
                "warning_codes": list(
                    item["consumer_group_governance"].get(
                        "warning_codes"
                    )
                    or ()
                ),
                "stale_groups": list(
                    item["consumer_group_governance"].get(
                        "stale_groups"
                    )
                    or ()
                ),
                "retention_blocked_groups": list(
                    item["consumer_group_governance"].get(
                        "retention_blocked_groups"
                    )
                    or ()
                ),
                "stale_consumer_entries": (
                    item["consumer_group_governance"].get(
                        "stale_consumer_entries"
                    )
                ),
            }
            for item in observations
            if (
                item["consumer_group_governance"].get("retention_alert")
                or item["consumer_group_governance"].get(
                    "consumer_inventory_alert"
                )
            )
        ],
        "legacy_pending": legacy["pending"],
        "legacy_lag": legacy["lag"],
        "legacy_outbox_undelivered": legacy_outbox["undelivered"],
        "legacy_drain_complete": bool(
            legacy["available"]
            and legacy["pending"] == 0
            and legacy["lag"] == 0
            and legacy_outbox["available"]
            and legacy_outbox["undelivered"] == 0
        ),
        "task_streams": observations,
        "stale_running": stale_running,
    }


async def _collect_control_stream_kind_metrics(
    *,
    kind: str,
    bindings,
    legacy_group_name: str,
    legacy_fanout_consumer_name: str,
    worker_heartbeats_task: asyncio.Task,
    consumer_group_budget: _ConsumerGroupObservationBudget,
) -> dict:
    """Observe one control-queue topology with per-platform failure domains."""

    original_bindings = tuple(bindings)
    legacy_binding = next(
        binding for binding in original_bindings if binding.legacy
    )
    observed_bindings = tuple(
        (
            replace(binding, group_name=legacy_group_name)
            if binding.legacy
            else binding
        )
        for binding in original_bindings
    )
    (
        observed,
        platform_outbox,
        legacy_outbox,
        worker_heartbeats,
    ) = await asyncio.gather(
        asyncio.gather(
            *(
                _task_stream_observation(
                    binding,
                    excluded_consumer_names=frozenset(
                        {"recovery-daemon"}
                    ),
                    consumer_group_budget=consumer_group_budget,
                )
                for binding in observed_bindings
            )
        ),
        _platform_outbox_observation(original_bindings),
        _legacy_outbox_observation(legacy_binding.stream_key),
        worker_heartbeats_task,
    )
    observations = [item[0] for item in observed]
    active_names_by_stream = {
        item[0]["stream"]: set(item[1]) for item in observed
    }
    heartbeats_available = bool(worker_heartbeats.get("available"))
    heartbeat_names = set(worker_heartbeats.get("names") or ())
    outbox_by_stream = platform_outbox["by_stream"]
    by_platform = {}
    for observation in observations:
        platform = observation["platform"]
        if platform is None:
            continue
        outbox = outbox_by_stream.get(observation["stream"], {})
        redis_names = active_names_by_stream.get(
            observation["stream"],
            set(),
        )
        verified_names = (
            redis_names & heartbeat_names
            if heartbeats_available
            else set()
        )
        outbox_available = bool(outbox.get("available"))
        blockers = []
        if not observation["available"]:
            blockers.append(f"{kind}_redis_metrics_unavailable")
        if not heartbeats_available:
            blockers.append(f"{kind}_worker_heartbeats_unavailable")
        elif not verified_names:
            blockers.append(f"{kind}_consumer_offline")
        if observation["consumer_group_governance"].get(
            "retention_alert"
        ):
            blockers.append(f"{kind}_consumer_group_retention_blocked")
        if not outbox_available:
            blockers.append(f"{kind}_outbox_metrics_unavailable")
        elif int(outbox.get("stale_undelivered") or 0) > 0:
            blockers.append(f"{kind}_outbox_stalled")
        by_platform[platform] = {
            **observation,
            "redis_consumers_online": len(redis_names),
            "workers_online": len(verified_names),
            "worker_heartbeats_available": heartbeats_available,
            "outbox_available": outbox_available,
            "outbox_undelivered": outbox.get("undelivered"),
            "outbox_stale_undelivered": outbox.get(
                "stale_undelivered"
            ),
            "outbox_oldest_age_seconds": outbox.get(
                "oldest_age_seconds"
            ),
            "ready": not blockers,
            "blocker_codes": blockers,
        }

    legacy = next(
        item for item in observations if item["platform"] is None
    )
    # The fanout consumer is intentionally excluded from normal Worker counts;
    # expose its liveness explicitly on the compatibility lane.
    legacy_consumers = next(
        (
            item[1]
            for item in observed
            if item[0]["platform"] is None
        ),
        set(),
    )
    legacy_fanout_online = any(
        legacy_fanout_consumer_name in name
        for name in legacy_consumers
    )
    legacy_summary = {
        **legacy,
        "fanout_group": legacy_group_name,
        "fanout_consumer_online": legacy_fanout_online,
        "outbox_undelivered": legacy_outbox["undelivered"],
        "outbox_available": legacy_outbox["available"],
        "drain_complete": bool(
            legacy["available"]
            and legacy["pending"] == 0
            and legacy["lag"] == 0
            and legacy_outbox["available"]
            and legacy_outbox["undelivered"] == 0
        ),
    }
    return {
        "kind": kind,
        "available": all(
            item["available"] for item in by_platform.values()
        ),
        "available_by_platform": {
            platform: bool(item["available"])
            for platform, item in by_platform.items()
        },
        "ready_by_platform": {
            platform: bool(item["ready"])
            for platform, item in by_platform.items()
        },
        "by_platform": by_platform,
        "legacy": legacy_summary,
        "legacy_drain_complete": legacy_summary["drain_complete"],
        "consumer_group_retention_alerts": [
            {
                "stream": item["stream"],
                "platform": item["platform"],
                "retention_alert": bool(
                    item["consumer_group_governance"].get(
                        "retention_alert"
                    )
                ),
                "consumer_inventory_alert": bool(
                    item["consumer_group_governance"].get(
                        "consumer_inventory_alert"
                    )
                ),
                "warning_codes": list(
                    item["consumer_group_governance"].get(
                        "warning_codes"
                    )
                    or ()
                ),
                "stale_groups": list(
                    item["consumer_group_governance"].get(
                        "stale_groups"
                    )
                    or ()
                ),
                "retention_blocked_groups": list(
                    item["consumer_group_governance"].get(
                        "retention_blocked_groups"
                    )
                    or ()
                ),
                "stale_consumer_entries": (
                    item["consumer_group_governance"].get(
                        "stale_consumer_entries"
                    )
                ),
            }
            for item in observations
            if (
                item["consumer_group_governance"].get("retention_alert")
                or item["consumer_group_governance"].get(
                    "consumer_inventory_alert"
                )
            )
        ],
        "streams": observations,
    }


async def _collect_discovery_scan_stream_metrics(
    *,
    consumer_group_budget: _ConsumerGroupObservationBudget,
) -> dict:
    """Observe the four Core-owned discovery request groups.

    These consumers do not have Worker heartbeat rows, so liveness is derived
    from their exact Redis consumer identity.  Governance and retention
    inspection still use the same request-wide budget as every other stream.
    """

    observed = await asyncio.gather(
        *(
            _task_stream_observation(
                binding,
                excluded_consumer_names=frozenset(
                    {"recovery-daemon"}
                ),
                consumer_group_budget=consumer_group_budget,
            )
            for binding in DISCOVERY_SCAN_STREAM_BINDINGS
        )
    )
    observations = []
    by_platform = {}
    for observation, active_names in observed:
        # This private task-lane helper contains Worker lane-health material
        # that has no meaning for a Core-owned discovery consumer.
        observation.pop("_consumer_idle_milliseconds_by_name", None)
        governance = observation["consumer_group_governance"]
        blockers = []
        if not observation["available"]:
            blockers.append("discovery_scan_redis_metrics_unavailable")
        if not active_names:
            blockers.append("discovery_scan_consumer_offline")
        if governance.get("retention_alert"):
            blockers.append(
                "discovery_scan_consumer_group_retention_blocked"
            )
        item = {
            **observation,
            "consumers_online": len(active_names),
            "ready": not blockers,
            "blocker_codes": blockers,
        }
        observations.append(item)
        by_platform[observation["platform"]] = item

    return {
        "kind": "discovery_scan",
        "available": all(
            item["available"] for item in by_platform.values()
        ),
        "available_by_platform": {
            platform: bool(item["available"])
            for platform, item in by_platform.items()
        },
        "ready_by_platform": {
            platform: bool(item["ready"])
            for platform, item in by_platform.items()
        },
        "by_platform": by_platform,
        "consumer_group_retention_alerts": [
            {
                "stream": item["stream"],
                "platform": item["platform"],
                "retention_alert": bool(
                    item["consumer_group_governance"].get(
                        "retention_alert"
                    )
                ),
                "consumer_inventory_alert": bool(
                    item["consumer_group_governance"].get(
                        "consumer_inventory_alert"
                    )
                ),
                "warning_codes": list(
                    item["consumer_group_governance"].get(
                        "warning_codes"
                    )
                    or ()
                ),
                "stale_groups": list(
                    item["consumer_group_governance"].get(
                        "stale_groups"
                    )
                    or ()
                ),
                "retention_blocked_groups": list(
                    item["consumer_group_governance"].get(
                        "retention_blocked_groups"
                    )
                    or ()
                ),
                "stale_consumer_entries": (
                    item["consumer_group_governance"].get(
                        "stale_consumer_entries"
                    )
                ),
            }
            for item in observations
            if (
                item["consumer_group_governance"].get("retention_alert")
                or item["consumer_group_governance"].get(
                    "consumer_inventory_alert"
                )
            )
        ],
        "streams": observations,
    }


async def collect_control_stream_metrics(
    *,
    consumer_group_budget: (
        _ConsumerGroupObservationBudget | None
    ) = None,
) -> dict:
    """Collect Probe and calibration lanes without coupling their failures."""

    if consumer_group_budget is None:
        consumer_group_budget = _ConsumerGroupObservationBudget()
    worker_heartbeats_task = asyncio.create_task(
        _worker_heartbeat_observation()
    )
    (
        probe_metrics,
        calibration_metrics,
        discovery_scan_metrics,
    ) = await asyncio.gather(
        _collect_control_stream_kind_metrics(
            kind="adapter_probe",
            bindings=adapter_probe_stream_bindings(include_legacy=True),
            legacy_group_name=LEGACY_ADAPTER_PROBE_FANOUT_GROUP_NAME,
            legacy_fanout_consumer_name=(
                LEGACY_ADAPTER_PROBE_FANOUT_CONSUMER_NAME
            ),
            worker_heartbeats_task=worker_heartbeats_task,
            consumer_group_budget=consumer_group_budget,
        ),
        _collect_control_stream_kind_metrics(
            kind="account_calibration",
            bindings=account_calibration_stream_bindings(
                include_legacy=True
            ),
            legacy_group_name=(
                LEGACY_ACCOUNT_CALIBRATION_FANOUT_GROUP_NAME
            ),
            legacy_fanout_consumer_name=(
                LEGACY_ACCOUNT_CALIBRATION_FANOUT_CONSUMER_NAME
            ),
            worker_heartbeats_task=worker_heartbeats_task,
            consumer_group_budget=consumer_group_budget,
        ),
        _collect_discovery_scan_stream_metrics(
            consumer_group_budget=consumer_group_budget,
        ),
    )
    platforms = tuple(get_platforms())
    by_platform = {
        platform: {
            "adapter_probe": probe_metrics["by_platform"].get(platform),
            "account_calibration": calibration_metrics[
                "by_platform"
            ].get(platform),
            "discovery_scan": discovery_scan_metrics[
                "by_platform"
            ].get(platform),
            "available": bool(
                probe_metrics["available_by_platform"].get(platform)
                and calibration_metrics["available_by_platform"].get(
                    platform
                )
                and discovery_scan_metrics["available_by_platform"].get(
                    platform
                )
            ),
            "ready": bool(
                probe_metrics["ready_by_platform"].get(platform)
                and calibration_metrics["ready_by_platform"].get(platform)
                and discovery_scan_metrics["ready_by_platform"].get(
                    platform
                )
            ),
        }
        for platform in platforms
    }
    return {
        "adapter_probe": probe_metrics,
        "account_calibration": calibration_metrics,
        "discovery_scan": discovery_scan_metrics,
        "by_platform": by_platform,
        "consumer_group_retention_alerts": [
            *probe_metrics["consumer_group_retention_alerts"],
            *calibration_metrics["consumer_group_retention_alerts"],
            *discovery_scan_metrics["consumer_group_retention_alerts"],
        ],
        "legacy_control_stream_drain_complete": bool(
            probe_metrics["legacy_drain_complete"]
            and calibration_metrics["legacy_drain_complete"]
        ),
    }


async def _stale_running_task_observation() -> dict:
    """Count DB-authoritative expired owners independently of Redis PEL."""

    platforms = tuple(
        dict.fromkeys(
            binding.platform
            for binding in task_stream_bindings(include_legacy=False)
            if binding.platform is not None
        )
    )
    by_platform = {
        platform: {
            "dry_run": 0,
            "shadow_run": 0,
            "real_run": 0,
            "total": 0,
        }
        for platform in platforms
    }
    try:
        rows = await asyncio.wait_for(
            database.fetch_all(
                """SELECT l.platform,
                          COALESCE(
                            NULLIF(TRIM(tr.task_mode), ''),
                            IF(tr.dry_run = 1, 'dry_run', 'real_run')
                          ) AS task_mode,
                          COUNT(*) AS cnt
                   FROM task_runs AS tr
                     FORCE INDEX (idx_task_run_stale_running)
                   JOIN lotteries AS l ON l.id = tr.lottery_id
                   WHERE tr.status = 'running'
                     AND (
                       tr.lease_expires_at IS NULL
                       OR tr.lease_expires_at <= NOW()
                     )
                   GROUP BY l.platform, task_mode"""
            ),
            timeout=TASK_METRICS_OPERATION_TIMEOUT_SECONDS,
        )
        total = 0
        for row in rows or []:
            platform = str(row["platform"] or "").strip().casefold()
            task_mode = str(row["task_mode"] or "").strip().casefold()
            count = int(row["cnt"] or 0)
            if platform not in by_platform:
                by_platform[platform] = {
                    "dry_run": 0,
                    "shadow_run": 0,
                    "real_run": 0,
                    "total": 0,
                }
            # Unknown/corrupt modes are still observable in the total and will
            # be quarantined by the same fail-closed path as a real run.
            bucket = (
                task_mode
                if task_mode in {"dry_run", "shadow_run", "real_run"}
                else "real_run"
            )
            by_platform[platform][bucket] += count
            by_platform[platform]["total"] += count
            total += count
        return {
            "available": True,
            "total": total,
            "by_platform": by_platform,
        }
    except Exception as exc:
        structured_log(
            "warning",
            "stale_running_task_metrics_unavailable",
            exception=exc,
        )
        return {
            "available": False,
            "total": None,
            "by_platform": {
                platform: {
                    "dry_run": None,
                    "shadow_run": None,
                    "real_run": None,
                    "total": None,
                }
                for platform in platforms
            },
        }


async def _legacy_outbox_observation(
    stream_key: str = LEGACY_TASK_STREAM_KEY,
) -> dict:
    """Count old rows which can still append work after Redis looks empty."""

    try:
        row = await asyncio.wait_for(
            database.fetch_one(
                """SELECT COUNT(*) AS cnt
                   FROM outbox_events
                   WHERE stream_key = :stream_key
                     AND status IN ('pending', 'sending')""",
                {"stream_key": stream_key},
            ),
            timeout=TASK_METRICS_OPERATION_TIMEOUT_SECONDS,
        )
        if row is None:
            raise RuntimeError("legacy_outbox_count_missing")
        return {
            "undelivered": int(row["cnt"] or 0),
            "available": True,
        }
    except Exception as exc:
        structured_log(
            "warning",
            "legacy_outbox_metrics_unavailable",
            stream=stream_key,
            exception=exc,
        )
        return {
            "undelivered": None,
            "available": False,
        }


async def count_worker_heartbeats(stale_seconds: int = 45) -> int:
    try:
        row = await asyncio.wait_for(
            database.fetch_one(
                """SELECT COUNT(*) AS cnt
                   FROM worker_heartbeats
                   WHERE status = 'ok'
                     AND TIMESTAMPDIFF(
                           SECOND,
                           last_seen_at,
                           NOW()
                         ) <= :stale_seconds""",
                {"stale_seconds": stale_seconds},
            ),
            timeout=TASK_METRICS_OPERATION_TIMEOUT_SECONDS,
        )
        return int(row["cnt"] if row else 0)
    except Exception:
        return 0


def platform_selectors_complete(selector_config: dict, platform: str) -> bool:
    configured = selector_config.get(platform, {})
    return selector_config_complete(platform, configured)


_NEXT_ACTION_CONTRACTS = {
    "restore_worker_capacity": {
        "operation": "recover_service",
        "requires_user_action": False,
        "example": "Recover the platform Worker service and wait for a fresh database heartbeat plus its Redis consumer membership.",
        "required_evidence": ["worker_heartbeat_fresh", "consumer_online"],
    },
    "restore_platform_task_transport": {
        "operation": "repair_transport",
        "requires_user_action": False,
        "example": "Recover the named platform standard/repair consumer and drain its stale Outbox rows before refreshing readiness.",
        "required_evidence": ["standard_consumer_online", "repair_consumer_online", "outbox_not_stale"],
    },
    "review_global_circuit_breaker": {
        "operation": "review",
        "requires_user_action": True,
        "example": "Open Safety, compare the breaker reason with recent failures, and record recovery evidence; do not close it yet.",
        "required_evidence": ["breaker_reason_reviewed", "recovery_validation_passed"],
    },
    "restore_autopilot_heartbeat": {
        "operation": "recover_service",
        "requires_user_action": False,
        "example": "Recover core-autopilot, then wait for one authenticated heartbeat newer than its stale threshold.",
        "required_evidence": ["fresh_autopilot_heartbeat"],
    },
    "configure_autopilot_dispatch": {
        "operation": "configure",
        "requires_user_action": True,
        "example": "Enable Autopilot with an explicit list such as bilibili,douyin,xiaohongshu; keep real-run authorization disabled.",
        "required_evidence": ["validated_non_empty_platform_allowlist"],
    },
    "authorize_autopilot_real_run": {
        "operation": "authorize",
        "requires_user_action": True,
        "example": "After every preceding P0 gate is green, an Owner supplies the deployment acknowledgement through the controlled runtime configuration.",
        "required_evidence": ["all_prerequisite_p0_checks_passed", "owner_approval"],
    },
    "approve_real_run_deployment": {
        "operation": "authorize",
        "requires_user_action": True,
        "example": "After every technical P0 gate is green, an Owner enables the deployment capability if needed, restarts affected services, then enables the audited runtime switch. Breaker recovery remains separate.",
        "required_evidence": [
            "all_non_authorization_p0_checks_passed",
            "deployment_capability_observed",
            "owner_approval",
        ],
    },
    "restore_target_readiness_observation": {
        "operation": "recover_observation",
        "requires_user_action": False,
        "example": "Restore the read-only target/evidence query, then refresh readiness; no target is dispatched during this check.",
        "required_evidence": ["target_readiness_observation_available"],
    },
    "add_autopilot_target": {
        "operation": "create_target",
        "requires_user_action": True,
        "example": "Accept one unexpired candidate from the pursuit queue into a platform that is present in the Autopilot allowlist.",
        "required_evidence": ["accepted_unexpired_target"],
    },
    "complete_target_action_plan": {
        "operation": "review_target",
        "requires_user_action": True,
        "example": "For example, verify the full rule text and pinned comment, save Action Plan v2 with the exact required actions and comment text, then attest its immutable rule snapshot.",
        "required_evidence": ["action_plan_v2_valid", "authoritative_rule_snapshot_attested"],
    },
    "complete_exact_real_candidate": {
        "operation": "complete_exact_readiness",
        "requires_user_action": True,
        "example": "Choose one accepted target and one lease-free calibrated account, then complete the target-bound dry/shadow and exact execution-evidence checks for that same pair.",
        "required_evidence": [
            "same_lottery_and_account",
            "account_lease_available",
            "exact_execution_evidence_bound",
            "target_valid",
        ],
    },
    "configure_notification": {
        "operation": "configure",
        "requires_user_action": True,
        "example": "Configure one supported channel and send a redacted test notification; never paste its secret into logs or screenshots.",
        "required_evidence": ["notification_test_delivered"],
    },
    "restore_notification_delivery_observation": {
        "operation": "recover_observation",
        "requires_user_action": False,
        "example": "Restore read access to notify_logs, refresh readiness, and keep real-run disabled until recent delivery proof is observable.",
        "required_evidence": ["notification_delivery_observation_available"],
    },
    "verify_notification_delivery": {
        "operation": "send_test",
        "requires_user_action": True,
        "example": "From Operations & Notify, send one redacted test through a configured channel and confirm its notify_logs row is Sent within 24 hours.",
        "required_evidence": ["recent_supported_channel_delivery_succeeded"],
    },
    "add_calibrated_account": {
        "operation": "account_login_and_calibration",
        "requires_user_action": True,
        "example": "Complete platform login, run calibration, and confirm the account reaches ready without an active risk hold.",
        "required_evidence": ["account_ready", "latest_calibration_succeeded"],
    },
    "configure_weibo_oauth": {
        "operation": "oauth_authorization",
        "requires_user_action": True,
        "example": "Authorize only approved OAuth scopes and refresh the account-bound capability attestation.",
        "required_evidence": ["oauth_capability_attestation"],
    },
    "complete_adapter_probe": {
        "operation": "probe",
        "requires_user_action": False,
        "example": "Run one no-side-effect adapter probe for the target platform and review its phase/capability evidence before real-run.",
        "required_evidence": ["adapter_probe_succeeded", "real_action_capability_observed"],
    },
    "enable_real_adapter": {
        "operation": "configure_adapter",
        "requires_user_action": True,
        "example": "Enable only a reviewed platform adapter and bind it to successful probe evidence.",
        "required_evidence": ["adapter_config_reviewed", "adapter_probe_succeeded"],
    },
    "resolve_redis_consumer_group_retention": {
        "operation": "repair_transport",
        "requires_user_action": True,
        "example": "Confirm the group is outside the active topology, inspect pending/lag, then use the controlled retirement command.",
        "required_evidence": ["consumer_group_inactive", "pending_zero", "retirement_audited"],
    },
    "retire_stale_redis_consumer_metadata": {
        "operation": "maintenance",
        "requires_user_action": False,
        "example": "Allow the bounded control-Worker pass to remove only zero-pending consumers with no live heartbeat.",
        "required_evidence": ["pending_zero", "consumer_heartbeat_absent"],
    },
    "review_risk": {
        "operation": "review",
        "requires_user_action": True,
        "example": "Inspect the latest risk event, keep the account cooling, and recalibrate it before reuse.",
        "required_evidence": ["risk_reviewed", "account_recalibrated"],
    },
    "add_proxy_exit": {
        "operation": "optional_hardening",
        "requires_user_action": True,
        "example": "Optionally assign an isolated, healthy exit to a production account; this P1 recommendation does not block readiness.",
        "required_evidence": ["proxy_health_verified"],
    },
    "keep_dry_run": {
        "operation": "validate",
        "requires_user_action": False,
        "example": "Continue dry/shadow validation without external side effects until the real-run evidence gate is complete.",
        "required_evidence": ["dry_or_shadow_run_succeeded"],
    },
}


# Dependency order for the operator-facing remediation queue.  Priority is
# still authoritative (all P0s precede P1/P2), while this rank keeps final
# authorization/breaker work behind the evidence it depends on.
_NEXT_ACTION_SEQUENCE = {
    "restore_worker_capacity": 10,
    "restore_platform_task_transport": 11,
    "restore_autopilot_heartbeat": 12,
    "configure_autopilot_dispatch": 13,
    "resolve_redis_consumer_group_retention": 14,
    "restore_notification_delivery_observation": 20,
    "configure_notification": 21,
    "verify_notification_delivery": 22,
    "restore_target_readiness_observation": 30,
    "add_autopilot_target": 31,
    "complete_target_action_plan": 32,
    "add_calibrated_account": 40,
    "configure_weibo_oauth": 41,
    "enable_real_adapter": 42,
    "complete_adapter_probe": 43,
    "complete_exact_real_candidate": 50,
    "review_global_circuit_breaker": 80,
    "approve_real_run_deployment": 90,
    "authorize_autopilot_real_run": 100,
    "review_risk": 110,
    "add_proxy_exit": 120,
    "retire_stale_redis_consumer_metadata": 121,
    "keep_dry_run": 200,
}


def _next_action_sort_key(action: dict) -> tuple[int, int, str, str]:
    priority_rank = {"P0": 0, "P1": 1, "P2": 2}.get(
        str(action.get("priority") or "").upper(),
        3,
    )
    code = str(action.get("code") or "")
    return (
        priority_rank,
        _NEXT_ACTION_SEQUENCE.get(code, 150),
        code,
        str(action.get("target") or ""),
    )


def _notification_gate_ready(summary: dict) -> bool:
    delivery = summary.get("notification_delivery")
    return bool(
        summary.get("notification_channels_configured", 0) > 0
        and isinstance(delivery, dict)
        and delivery.get("available") is True
        and delivery.get("ready") is True
        and int(delivery.get("sent_count_24h") or 0) > 0
        and delivery.get("last_success_at") is not None
    )


def _target_eligible_platforms(summary: dict) -> set[str]:
    target_status = summary.get("autopilot_targets")
    eligible_by_platform = (
        target_status.get("eligible_by_platform")
        if isinstance(target_status, dict)
        else None
    )
    if not isinstance(eligible_by_platform, dict):
        return set()
    return {
        str(platform).strip().casefold()
        for platform, count in eligible_by_platform.items()
        if (
            str(platform).strip().casefold() in PLATFORM_IDS
            and int(count or 0) > 0
        )
    }


def _real_capability_platforms(scoped_platforms) -> set[str]:
    return {
        str(item.get("platform") or "").strip().casefold()
        for item in scoped_platforms
        if (
            str(item.get("platform") or "").strip().casefold()
            in PLATFORM_IDS
            and int(item.get("safe_accounts") or 0) > 0
            and item.get("real_actions_ready") is True
            and item.get("task_transport_ready") is True
        )
    }


def _deployment_real_run_capability(summary: dict) -> bool:
    """Read the explicit deployment ceiling, with legacy summary fallback."""

    return bool(
        summary.get(
            "deployment_real_run_enabled",
            summary.get("real_run_enabled"),
        )
    )


def _safe_latest_probe_observation(platform: dict) -> dict | None:
    latest = platform.get("latest_probe")
    if not isinstance(latest, dict):
        return None
    try:
        ready_phase_count = max(int(latest.get("ready_phase_count") or 0), 0)
    except (TypeError, ValueError):
        ready_phase_count = 0
    return {
        "status": str(latest.get("status") or "unknown")[:32],
        "created_at": latest.get("created_at"),
        "ready_phase_count": ready_phase_count,
        "ready_for_real_actions": bool(
            latest.get("ready_for_real_actions")
        ),
    }


def _structured_next_action(action: dict) -> dict:
    contract = _NEXT_ACTION_CONTRACTS.get(action["code"], {})
    observed = action.pop("_observed", {})
    return {
        **action,
        "next_step": {
            "operation": contract.get("operation", "review"),
            "target": action.get("target"),
            "description": action.get("detail"),
            "requires_user_action": bool(
                contract.get("requires_user_action", True)
            ),
        },
        "example": {
            "description": contract.get("example", action.get("detail")),
        },
        "evidence": {
            "observed": observed,
            "required": list(contract.get("required_evidence", ())),
        },
    }


def build_next_actions(platforms, summary):

    actions = []
    scoped_platforms = _autopilot_scope_items(platforms, summary)
    scope_resolution = _autopilot_scope_resolution(platforms, summary)
    heartbeat_fresh = False
    dispatch_configured = False

    global_breaker = summary.get("global_circuit_breaker")
    if (
        isinstance(global_breaker, dict)
        and global_breaker.get("allows_real_run") is not True
    ):
        breaker_status = str(
            global_breaker.get("status") or "unknown"
        )
        actions.append({
            "code": "review_global_circuit_breaker",
            "priority": "P0",
            "target": "runtime",
            "title": "Review the global circuit breaker",
            "detail": (
                f"The global circuit breaker is {breaker_status}. Review "
                "the recorded cause and use the controlled recovery workflow "
                "before closing it; readiness will not reset it automatically."
                if global_breaker.get("available") is True
                else (
                    "The global circuit breaker cannot be observed. Restore "
                    "its database state before permitting any real action."
                )
            ),
            "_observed": {
                "available": global_breaker.get("available"),
                "status": breaker_status,
                "reason": global_breaker.get("reason"),
            },
        })

    autopilot = summary.get("autopilot")
    if isinstance(autopilot, dict):
        heartbeat_fresh = (
            autopilot.get("available") is True
            and autopilot.get("reported") is True
            and autopilot.get("fresh") is True
        )
        dispatch_configured = (
            autopilot.get("enabled") is True
            and autopilot.get("dispatch_configured") is True
            and autopilot.get("platform_allowlist_valid") is True
            and scope_resolution["complete"] is True
        )
        if not heartbeat_fresh:
            actions.append({
                "code": "restore_autopilot_heartbeat",
                "priority": "P0",
                "target": "autopilot",
                "title": "Restore the Autopilot heartbeat",
                "detail": (
                    "Start or recover the Autopilot service and confirm a "
                    "fresh authenticated heartbeat before automatic dispatch."
                ),
                "_observed": {
                    "available": autopilot.get("available"),
                    "reported": autopilot.get("reported"),
                    "fresh": autopilot.get("fresh"),
                    "heartbeat_age_seconds": autopilot.get(
                        "heartbeat_age_seconds"
                    ),
                },
            })
        elif not dispatch_configured:
            actions.append({
                "code": "configure_autopilot_dispatch",
                "priority": "P0",
                "target": "autopilot",
                "title": "Enable and configure Autopilot dispatch",
                "detail": (
                    "Enable Autopilot and configure a non-empty validated "
                    "platform allowlist before automatic dispatch."
                ),
                "_observed": {
                    "enabled": autopilot.get("enabled"),
                    "platform_allowlist": autopilot.get(
                        "platform_allowlist"
                    ),
                    "platform_allowlist_valid": autopilot.get(
                        "platform_allowlist_valid"
                    ),
                    "scope_resolution": scope_resolution,
                },
            })

    retention_alerts = [
        item
        for item in (
            summary.get("redis_consumer_group_retention_alerts") or ()
        )
        if isinstance(item, dict)
    ]
    blocking_retention_alerts = [
        item for item in retention_alerts if item.get("retention_alert")
    ]
    stale_consumer_entries = sum(
        int(item.get("stale_consumer_entries") or 0)
        for item in retention_alerts
        if item.get("consumer_inventory_alert")
    )
    if "workers_online" in summary and summary.get("workers_online", 0) <= 0:
        actions.append({
            "code": "restore_worker_capacity",
            "priority": "P0",
            "target": "workers",
            "title": "Restore at least one observable Worker",
            "detail": (
                "No live Redis consumer or fresh Worker heartbeat is visible; "
                "automatic dispatch remains fail-closed."
            ),
            "_observed": {"workers_online": summary.get("workers_online", 0)},
        })
    for item in scoped_platforms:
        if item.get("task_transport_ready"):
            continue
        actions.append({
            "code": "restore_platform_task_transport",
            "priority": "P0",
            "target": item.get("platform"),
            "title": f"Restore task transport for {item.get('label')}",
            "detail": (
                "The platform's standard/repair task lanes or Outbox delivery "
                "are not fully observable and ready."
            ),
            "_observed": {
                "blocker_codes": item.get(
                    "task_transport_blocker_codes", []
                ),
                "task_transport": item.get("task_transport"),
            },
        })
    if blocking_retention_alerts:
        actions.append({
            "code": "resolve_redis_consumer_group_retention",
            "priority": "P0",
            "target": "redis",
            "title": "Resolve Redis consumer-group retention blockers",
            "detail": (
                f"{len(blocking_retention_alerts)} stream observation(s) "
                "contain a missing, unexpected, backlogged, or "
                "incompletely observed group. Inspect pending and lag, "
                "then use the explicit group-retirement workflow only "
                "after the group leaves the active topology."
            ),
        })
    elif stale_consumer_entries:
        actions.append({
            "code": "retire_stale_redis_consumer_metadata",
            "priority": "P1",
            "target": "redis",
            "title": "Retire stale Redis consumer metadata",
            "detail": (
                f"{stale_consumer_entries} zero-pending stale consumer "
                "entries are awaiting the bounded control-Worker pass. "
                "The pass requires no live heartbeat and atomically "
                "rechecks pending and idle without destroying the group."
            ),
        })

    if summary.get("notification_channels_configured", 0) == 0:

        actions.append({

            "code": "configure_notification",

            "priority": "P0",

            "target": "notifications",

            "title": "Configure at least one notification channel",

            "detail": "Set SERVERCHAN_KEY, FEISHU_WEBHOOK, GENERIC_WEBHOOK_URL, or TELEGRAM_BOT_TOKEN plus TELEGRAM_CHAT_ID, then send a notification test.",

            "_observed": {"configured_channels": 0},

        })
    elif "notification_delivery" in summary:
        notification_delivery = summary.get("notification_delivery")
        if (
            not isinstance(notification_delivery, dict)
            or notification_delivery.get("available") is not True
        ):
            actions.append({
                "code": "restore_notification_delivery_observation",
                "priority": "P0",
                "target": "notifications",
                "title": "Restore notification delivery observation",
                "detail": (
                    "Configured channels cannot prove a recent delivery "
                    "because the bounded notify_logs observation is unavailable."
                ),
                "_observed": {
                    "available": (
                        notification_delivery.get("available")
                        if isinstance(notification_delivery, dict)
                        else False
                    ),
                    "blocker_code": (
                        notification_delivery.get("blocker_code")
                        if isinstance(notification_delivery, dict)
                        else "notification_delivery_status_unavailable"
                    ),
                },
            })
        elif not _notification_gate_ready(summary):
            actions.append({
                "code": "verify_notification_delivery",
                "priority": "P0",
                "target": "notifications",
                "title": "Verify one recent notification delivery",
                "detail": (
                    "A supported channel is configured, but no successful "
                    "delivery through a currently configured channel was "
                    f"observed in the last {NOTIFICATION_DELIVERY_WINDOW_HOURS} hours."
                ),
                "_observed": {
                    "configured_channels": notification_delivery.get(
                        "configured_channels", []
                    ),
                    "sent_count_24h": notification_delivery.get(
                        "sent_count_24h", 0
                    ),
                    "last_success_at": notification_delivery.get(
                        "last_success_at"
                    ),
                    "blocker_code": notification_delivery.get("blocker_code"),
                },
            })

    target_readiness = summary.get("autopilot_targets")
    if isinstance(target_readiness, dict):
        if target_readiness.get("available") is not True:
            actions.append({
                "code": "restore_target_readiness_observation",
                "priority": "P0",
                "target": "lottery-targets",
                "title": "Restore automatic-target readiness observation",
                "detail": (
                    "The bounded target and Action Plan evidence read is "
                    "unavailable, so automatic production remains fail-closed."
                ),
                "_observed": {
                    "blocker_code": target_readiness.get("blocker_code"),
                },
            })
        elif int(target_readiness.get("pending_targets") or 0) == 0:
            actions.append({
                "code": "add_autopilot_target",
                "priority": "P0",
                "target": "lottery-targets",
                "title": "Accept an unexpired target in the Autopilot scope",
                "detail": (
                    "No pending or claimed target exists in the validated "
                    "Autopilot platform allowlist."
                ),
                "_observed": {
                    "platform_allowlist": list(
                        _autopilot_allowlist(summary)
                    ),
                    "pending_targets": 0,
                },
            })
        elif int(target_readiness.get("eligible_targets") or 0) == 0:
            actions.append({
                "code": "complete_target_action_plan",
                "priority": "P0",
                "target": "lottery-targets",
                "title": "Complete Action Plan v2 and the rule snapshot",
                "detail": (
                    f"{target_readiness.get('missing_plan_targets', 0)} "
                    "observed target(s) require an exact reviewed Action Plan "
                    "v2 and attested authoritative rule snapshot before "
                    "Autopilot can select them."
                ),
                "_observed": {
                    "missing_plan_target_ids": target_readiness.get(
                        "missing_plan_target_ids", []
                    ),
                    "plan_blocker_counts": target_readiness.get(
                        "plan_blocker_counts", {}
                    ),
                    "observation_truncated": target_readiness.get(
                        "observation_truncated"
                    ),
                },
            })
        elif int(target_readiness.get("missing_plan_targets") or 0) > 0:
            actions.append({
                "code": "complete_target_action_plan",
                "priority": "P1",
                "target": "lottery-targets",
                "title": "Complete plans for the remaining automatic targets",
                "detail": (
                    "At least one automatic target is eligible, but other "
                    "observed targets still lack exact plan/rule evidence."
                ),
                "_observed": {
                    "eligible_targets": target_readiness.get(
                        "eligible_targets", 0
                    ),
                    "missing_plan_target_ids": target_readiness.get(
                        "missing_plan_target_ids", []
                    ),
                },
            })
        exact_candidate = target_readiness.get("exact_real_candidate")
        exact_candidate = (
            exact_candidate if isinstance(exact_candidate, dict) else {}
        )
        if (
            int(target_readiness.get("eligible_targets") or 0) > 0
            and exact_candidate.get("ready") is not True
        ):
            exact_observation_available = (
                exact_candidate.get("available") is True
            )
            actions.append({
                "code": (
                    "complete_exact_real_candidate"
                    if exact_observation_available
                    else "restore_target_readiness_observation"
                ),
                "priority": "P0",
                "target": "lottery-targets",
                "title": (
                    "Complete one exact target/account real-run candidate"
                    if exact_observation_available
                    else "Restore exact-candidate readiness observation"
                ),
                "detail": (
                    "Bind one eligible target to the same lease-free account "
                    "through successful dry/shadow validation, exact execution "
                    "evidence, and target validation."
                    if exact_observation_available
                    else (
                        "The bounded account-scoped candidate observation is "
                        "unavailable; restore it before production approval."
                    )
                ),
                "_observed": {
                    "available": exact_observation_available,
                    "candidate_count": int(
                        exact_candidate.get("candidate_count") or 0
                    ),
                    "blocker_code": exact_candidate.get("blocker_code"),
                    "blocker_counts": exact_candidate.get(
                        "blocker_counts", {}
                    ),
                    "observation_truncated": bool(
                        exact_candidate.get("observation_truncated")
                    ),
                },
            })

    if summary.get("recent_risk_events_24h", 0) > 0:

        actions.append({

            "code": "review_risk",

            "priority": "P1",

            "target": "risk-center",

            "title": "Review recent account risk signals",

            "detail": "Open Risk Center, inspect the latest risk events, and recalibrate any affected account before returning it to the ready pool.",

        })

    if summary.get("proxy_exits_total", 0) == 0:

        actions.append({

            "code": "add_proxy_exit",

            "priority": "P1",

            "target": "proxy-pool",

            "title": "Add isolated proxy exits for production accounts",

            "detail": "Open Safety, add one proxy exit per account, and keep risky exits in cooldown before enabling higher-volume multi-platform automation.",

        })

    real_capability_platforms = _real_capability_platforms(
        scoped_platforms
    )
    no_scoped_real_capability = not real_capability_platforms
    for platform in scoped_platforms:

        if int(platform.get("safe_accounts") or 0) == 0:

            actions.append({

                "code": "add_calibrated_account",

                "priority": "P0",

                "target": platform["platform"],

                "title": f"Add a calibrated safe account for {platform['label']}",

                "detail": "Use QR login or cookie import, then run account calibration so dry-run dispatch can safely auto-pick the account.",

                "_observed": {"safe_accounts": 0},

            })

        if platform.get("adapter_kind") == "oauth" and not platform.get("real_actions_ready"):

            actions.append({

                "code": "configure_weibo_oauth",

                "priority": "P0" if no_scoped_real_capability else "P1",

                "target": platform["platform"],

                "title": f"Authorize official OAuth actions for {platform['label']}",

                "detail": "Configure an approved OAuth application and refresh account-bound capability evidence. Advanced like/follow permissions remain denied until explicitly granted.",

                "_observed": {
                    "adapter_kind": "oauth",
                    "real_actions_ready": False,
                },

            })

        if (
            platform.get("action_adapter")
            and platform.get("adapter_kind") != "oauth"
            and not platform.get("real_actions_ready")
        ):

            actions.append({

                "code": "complete_adapter_probe",

                "priority": "P0" if no_scoped_real_capability else "P1",

                "target": platform["platform"],

                "title": f"Complete adapter probe for {platform['label']}",

                "detail": (
                    "Run a no-side-effect probe for the configured adapter "
                    "kind and review its required-action capability evidence."
                ),

                "_observed": {
                    "adapter_kind": platform.get("adapter_kind"),
                    "latest_probe": _safe_latest_probe_observation(platform),
                    "real_actions_ready": False,
                },

            })

        if not platform.get("action_adapter") and platform.get("adapter_kind") != "manual_assisted":

            actions.append({

                "code": "enable_real_adapter",

                "priority": "P0" if no_scoped_real_capability else "P1",

                "target": platform["platform"],

                "title": f"Enable real action adapter for {platform['label']}",

                "detail": "After a safe account is calibrated, configure DPMS_ADAPTER_SELECTORS_B64 with selectors verified by adapter probe evidence.",

                "_observed": {
                    "adapter_kind": platform.get("adapter_kind"),
                    "action_adapter": False,
                },

            })

    dry_scope = [
        item for item in scoped_platforms if item.get("dry_run_supported")
    ]
    target_eligible_platforms = _target_eligible_platforms(summary)
    target_gate_ready = bool(
        isinstance(target_readiness, dict)
        and target_readiness.get("available") is True
        and int(target_readiness.get("eligible_targets") or 0) > 0
        and target_eligible_platforms
    )
    exact_candidate_status = (
        target_readiness.get("exact_real_candidate")
        if isinstance(target_readiness, dict)
        else None
    )
    exact_candidate_projection = (
        exact_candidate_status.get("candidate")
        if isinstance(exact_candidate_status, dict)
        else None
    )
    exact_candidate_platform = (
        str(exact_candidate_projection.get("platform") or "")
        .strip()
        .casefold()
        if isinstance(exact_candidate_projection, dict)
        else ""
    )
    exact_candidate_ready = bool(
        isinstance(exact_candidate_status, dict)
        and exact_candidate_status.get("available") is True
        and exact_candidate_status.get("ready") is True
        and int(exact_candidate_status.get("candidate_count") or 0) > 0
        and exact_candidate_platform in real_capability_platforms
    )
    predeployment_ready = bool(
        heartbeat_fresh
        and dispatch_configured
        and scoped_platforms
        and summary.get("workers_online", 0) > 0
        and all(item.get("task_transport_ready") for item in scoped_platforms)
        and dry_scope
        and all(item.get("ready_for_dry_run") for item in dry_scope)
        and exact_candidate_ready
        and _notification_gate_ready(summary)
        and target_gate_ready
        and not blocking_retention_alerts
    )
    breaker_closed = bool(
        isinstance(global_breaker, dict)
        and global_breaker.get("available") is True
        and global_breaker.get("allows_real_run") is True
    )
    deployment_real_run_enabled = _deployment_real_run_capability(summary)
    if predeployment_ready and not summary.get("real_run_enabled"):
        if deployment_real_run_enabled:
            approval_detail = (
                "All observed technical prerequisites are ready and the "
                "deployment capability is available. An Owner may now enable "
                "the audited runtime switch; breaker recovery remains a "
                "separate controlled action."
            )
        else:
            approval_detail = (
                "All observed technical prerequisites are ready. An Owner may "
                "now enable the deployment capability, restart the affected "
                "services, and then enable the audited runtime switch; breaker "
                "recovery remains separate."
            )
        actions.append({
            "code": "approve_real_run_deployment",
            "priority": "P0",
            "target": "runtime",
            "title": "Complete the controlled real-run approval",
            "detail": approval_detail,
            "_observed": {
                "deployment_real_run_enabled": deployment_real_run_enabled,
                "real_run_enabled": bool(summary.get("real_run_enabled")),
                "global_circuit_breaker_closed": breaker_closed,
            },
        })
    elif (
        predeployment_ready
        and breaker_closed
        and summary.get("real_run_enabled")
        and isinstance(autopilot, dict)
        and autopilot.get("real_run_authorized") is not True
    ):
        actions.append({
            "code": "authorize_autopilot_real_run",
            "priority": "P0",
            "target": "autopilot",
            "title": "Authorize Autopilot real-run explicitly",
            "detail": (
                "Every preceding automatic-production prerequisite is ready; "
                "an Owner may now provide the explicit deployment acknowledgement."
            ),
            "_observed": {
                "deployment_real_run_enabled": autopilot.get(
                    "deployment_real_run_enabled"
                ),
                "real_run_ack_valid": autopilot.get("real_run_ack_valid"),
            },
        })

    scoped_dry_ready = sum(
        1 for item in scoped_platforms if item.get("ready_for_dry_run")
    )
    scoped_real_ready = sum(
        1 for item in scoped_platforms if item.get("ready_for_real_run")
    )
    if scoped_dry_ready > 0 and scoped_real_ready == 0:

        actions.append({

            "code": "keep_dry_run",

            "priority": "P2",

            "target": "workflow",

            "title": "Keep production dispatch in dry-run mode",

            "detail": "Dry-run validation can continue, but real execution should remain gated until at least one platform has a complete probe and real adapter readiness.",

            "_observed": {
                "autopilot_scope_dry_ready": scoped_dry_ready,
                "autopilot_scope_real_ready": scoped_real_ready,
            },

        })

    return [
        _structured_next_action(action)
        for action in sorted(actions, key=_next_action_sort_key)
    ]


async def build_strategy_advice(summary):
    window_days = 7
    mode_rows = await database.fetch_all(
        """SELECT COALESCE(task_mode, IF(dry_run = 1, 'dry_run', 'real_run')) AS mode,
                  status,
                  COUNT(*) AS cnt
           FROM task_runs
           WHERE created_at >= DATE_SUB(NOW(), INTERVAL 7 DAY)
           GROUP BY mode, status"""
    )
    risk_rows = await database.fetch_all(
        """SELECT account_id, COUNT(*) AS cnt
           FROM risk_events
           WHERE created_at >= DATE_SUB(NOW(), INTERVAL 7 DAY)
           GROUP BY account_id
           ORDER BY cnt DESC
           LIMIT 3"""
    )
    high_value_pending = await database.fetch_one(
        "SELECT COUNT(*) AS cnt FROM lotteries WHERE status = 'pending' AND value_score >= 70"
    )
    stale_active_tasks = await database.fetch_one(
        """SELECT COUNT(*) AS cnt
           FROM task_runs
           WHERE status IN ('queued','running')
             AND created_at < DATE_SUB(NOW(), INTERVAL 30 MINUTE)"""
    )

    mode_counts = [dict(row) for row in mode_rows]
    mode_success = {
        row["mode"]: int(row["cnt"])
        for row in mode_counts
        if row["status"] == "succeeded"
    }
    mode_failed = {
        row["mode"]: int(row["cnt"])
        for row in mode_counts
        if row["status"] == "failed"
    }
    shadow_success = mode_success.get("shadow_run", 0)
    dry_success = mode_success.get("dry_run", 0)
    real_success = mode_success.get("real_run", 0)
    failed_total = sum(mode_failed.values())
    advice = []

    if summary.get("notification_channels_configured", 0) == 0:
        advice.append(strategy_item(
            "configure_notifications",
            "P0",
            "notify",
            "Configure notification before autonomous runs",
            "No external notification channel is configured, so wins, failures, captcha, or bans may only be visible in logs.",
            {"configured_channels": 0},
        ))

    if summary.get("pending_targets", 0) > 0 and shadow_success == 0:
        advice.append(strategy_item(
            "run_shadow_before_real",
            "P0",
            "workflow",
            "Run shadow-run before real-run",
            "Pending targets exist, but no successful shadow-run was recorded in the last 7 days.",
            {"pending_targets": summary.get("pending_targets", 0), "shadow_success_7d": shadow_success},
        ))

    if dry_success > 0 and shadow_success == 0:
        advice.append(strategy_item(
            "promote_dry_to_shadow",
            "P1",
            "workflow",
            "Promote dry-run validation into shadow-run",
            "Dry-run is succeeding, so the next safe step is opening real pages with no side effects to validate login state and risk signals.",
            {"dry_success_7d": dry_success},
        ))

    if shadow_success > 0 and summary.get("real_run_ready", 0) == 0:
        advice.append(strategy_item(
            "complete_real_gate",
            "P1",
            "strategy",
            "Complete the real-run gate after shadow evidence",
            "Shadow-run evidence exists, but no platform currently satisfies real-run readiness.",
            {"shadow_success_7d": shadow_success, "real_run_ready": summary.get("real_run_ready", 0)},
        ))

    if failed_total > 0:
        advice.append(strategy_item(
            "review_failed_runs",
            "P1",
            "review",
            "Review failed runs before increasing volume",
            "Recent task failures should be inspected through evidence screenshots and event timeline before scaling the scheduler.",
            {"failed_runs_7d": failed_total},
        ))

    if summary.get("recent_risk_events_24h", 0) > 0 or risk_rows:
        advice.append(strategy_item(
            "cooldown_risky_accounts",
            "P1",
            "risk",
            "Cooldown and recalibrate risky accounts",
            "Risk events were recorded recently; affected accounts should be cooled, recalibrated, or assigned safer proxy exits.",
            {
                "risk_24h": summary.get("recent_risk_events_24h", 0),
                "hot_accounts": [dict(row) for row in risk_rows],
            },
        ))

    if int(high_value_pending["cnt"] if high_value_pending else 0) > 0 and summary.get("safe_accounts_total", 0) > 0:
        advice.append(strategy_item(
            "prioritize_high_value_targets",
            "P2",
            "strategy",
            "Prioritize high-value pending targets",
            "High-score pending targets exist and safe accounts are available; dispatch order should favor expected value after shadow-run clears.",
            {
                "high_value_pending": int(high_value_pending["cnt"]),
                "safe_accounts": summary.get("safe_accounts_total", 0),
            },
        ))

    if int(stale_active_tasks["cnt"] if stale_active_tasks else 0) > 0:
        advice.append(strategy_item(
            "recover_stale_tasks",
            "P1",
            "worker",
            "Recover stale active tasks",
            "Queued or running tasks older than 30 minutes were found; inspect Worker health and recovery daemon behavior.",
            {"stale_active_tasks": int(stale_active_tasks["cnt"])},
        ))

    if not advice and real_success > 0:
        advice.append(strategy_item(
            "continue_controlled_real_run",
            "P2",
            "strategy",
            "Continue controlled real-run cadence",
            "Recent real-run tasks succeeded and no urgent blocker was detected by the strategy review.",
            {"real_success_7d": real_success},
        ))

    return {
        "review_window_days": window_days,
        "mode_counts": mode_counts,
        "high_value_pending": int(high_value_pending["cnt"] if high_value_pending else 0),
        "stale_active_tasks": int(stale_active_tasks["cnt"] if stale_active_tasks else 0),
        "risk_hot_accounts": [dict(row) for row in risk_rows],
        "advice": advice[:8],
    }


def strategy_item(code, priority, target, title, detail, evidence):
    return {
        "code": code,
        "priority": priority,
        "target": target,
        "title": title,
        "detail": detail,
        "evidence": evidence,
    }


_CHECK_ACTION_CODES = {
    "worker_online": "restore_worker_capacity",
    "all_platform_task_transports_ready": "restore_platform_task_transport",
    "global_circuit_breaker_closed": "review_global_circuit_breaker",
    "autopilot_heartbeat_fresh": "restore_autopilot_heartbeat",
    "autopilot_dispatch_configured": "configure_autopilot_dispatch",
    "autopilot_real_run_authorized": "authorize_autopilot_real_run",
    "notification_ready": "configure_notification",
    "target_pool_ready": "add_autopilot_target",
    "autopilot_target_plan_ready": "complete_target_action_plan",
    "autopilot_exact_real_candidate_ready": "complete_exact_real_candidate",
    "all_platforms_dry_ready": "add_calibrated_account",
    "real_run_available": "complete_adapter_probe",
    "active_proxy_exit": "add_proxy_exit",
    "safe_accounts_proxied": "add_proxy_exit",
    "recent_risk_clear": "review_risk",
}


def _structured_production_check(check: dict, scope: list[str]) -> dict:
    observed = check.pop("_observed", {})
    required = check.pop("_required", {})
    next_action_override = check.pop("_next_action_code", None)
    return {
        **check,
        "blocking": check.get("priority") == "P0",
        "scope": list(scope),
        "next_action_code": (
            None
            if check.get("passed")
            else next_action_override or _CHECK_ACTION_CODES.get(check["code"])
        ),
        "evidence": {
            "observed": observed,
            "required": required,
        },
    }


def build_production_checks(platforms, summary):
    scoped_platforms = _autopilot_scope_items(platforms, summary)
    scope_resolution = _autopilot_scope_resolution(platforms, summary)
    live_autopilot_scope = "autopilot" in summary
    scope_names = [
        str(item.get("platform") or "") for item in scoped_platforms
    ]
    if live_autopilot_scope:
        platform_count = len(scoped_platforms)
        dry_supported = sum(
            1 for item in scoped_platforms if item.get("dry_run_supported")
        )
        dry_ready = sum(
            1 for item in scoped_platforms if item.get("ready_for_dry_run")
        )
        real_ready = sum(
            1
            for item in scoped_platforms
            if (
                int(item.get("safe_accounts") or 0) > 0
                and item.get("real_actions_ready") is True
                and item.get("task_transport_ready") is True
            )
        )
        transport_ready = sum(
            1 for item in scoped_platforms if item.get("task_transport_ready")
        )
    else:
        platform_count = summary.get("platforms_total", 0)
        dry_ready = summary.get("dry_run_ready", 0)
        dry_supported = summary.get("dry_run_supported", platform_count)
        real_ready = summary.get("real_run_ready", 0)
        transport_ready = summary.get("task_transport_ready", 0)
        scope_names = sorted(
            (summary.get("task_transport_by_platform") or {}).keys()
        )
    safe_accounts = summary.get("safe_accounts_total", 0)
    blocked_transport_platforms = sorted(
        str(item.get("platform") or "")
        for item in scoped_platforms
        if not item.get("task_transport_ready")
    ) if live_autopilot_scope else sorted(
        platform
        for platform, observation in (
            summary.get("task_transport_by_platform") or {}
        ).items()
        if not observation.get("ready")
    )

    runtime_checks = []
    global_breaker = summary.get("global_circuit_breaker")
    if isinstance(global_breaker, dict):
        breaker_status = str(global_breaker.get("status") or "unknown")
        breaker_observed = global_breaker.get("available") is True
        breaker_closed = bool(
            breaker_observed
            and breaker_status == "closed"
            and global_breaker.get("allows_real_run") is True
        )
        runtime_checks.append({
            "code": "global_circuit_breaker_closed",
            "priority": "P0",
            "passed": breaker_closed,
            "title": "Global circuit breaker is observed and closed",
            "detail": (
                "Global circuit breaker is closed and permits real-run."
                if breaker_closed
                else (
                    f"Global circuit breaker is {breaker_status}; review the "
                    "cause and close it only through controlled recovery."
                    if breaker_observed
                    else "Global circuit breaker state is unavailable."
                )
            ),
            "_observed": {
                "available": breaker_observed,
                "status": breaker_status,
                "reason": global_breaker.get("reason"),
            },
            "_required": {"available": True, "status": "closed"},
        })

    autopilot = summary.get("autopilot")
    if isinstance(autopilot, dict):
        heartbeat_fresh = bool(
            autopilot.get("available") is True
            and autopilot.get("reported") is True
            and autopilot.get("fresh") is True
        )
        heartbeat_detail = (
            "Autopilot heartbeat is fresh "
            f"({autopilot.get('heartbeat_age_seconds')} second(s) old; stale "
            f"after {autopilot.get('stale_after_seconds')} second(s))."
            if heartbeat_fresh
            else (
                "Autopilot heartbeat state is unavailable."
                if autopilot.get("available") is not True
                else (
                    "Autopilot has not reported a heartbeat."
                    if autopilot.get("reported") is not True
                    else "Autopilot heartbeat is stale."
                )
            )
        )
        runtime_checks.append({
            "code": "autopilot_heartbeat_fresh",
            "priority": "P0",
            "passed": heartbeat_fresh,
            "title": "Autopilot heartbeat is present and fresh",
            "detail": heartbeat_detail,
            "_observed": {
                "available": autopilot.get("available"),
                "reported": autopilot.get("reported"),
                "fresh": autopilot.get("fresh"),
                "heartbeat_age_seconds": autopilot.get(
                    "heartbeat_age_seconds"
                ),
            },
            "_required": {"available": True, "reported": True, "fresh": True},
        })
        dispatch_configured = bool(
            autopilot.get("enabled") is True
            and autopilot.get("dispatch_configured") is True
            and autopilot.get("platform_allowlist_valid") is True
            and scope_resolution["complete"] is True
        )
        runtime_checks.append({
            "code": "autopilot_dispatch_configured",
            "priority": "P0",
            "passed": dispatch_configured,
            "title": "Autopilot dispatch scope is configured",
            "detail": (
                f"Autopilot dispatch is enabled for {len(scope_names)} "
                f"validated platform(s): {', '.join(scope_names) or 'none'}."
            ),
            "_observed": {
                "enabled": autopilot.get("enabled"),
                "platform_allowlist": autopilot.get("platform_allowlist"),
                "resolved_scope": scope_names,
                "scope_resolution": scope_resolution,
            },
            "_required": {
                "enabled": True,
                "declared_scope_equals_resolved_scope": True,
                "resolved_scope_minimum": 1,
            },
        })
        real_run_authorized = autopilot.get("real_run_authorized") is True
        runtime_checks.append({
            "code": "autopilot_real_run_authorized",
            "priority": "P0",
            "passed": real_run_authorized,
            "title": "Autopilot has explicit deployment real-run authorization",
            "detail": (
                "Deployment capability and explicit acknowledgement are valid."
                if real_run_authorized
                else "Autopilot remains dry-run only pending final Owner approval."
            ),
            "_observed": {
                "deployment_real_run_enabled": autopilot.get(
                    "deployment_real_run_enabled"
                ),
                "real_run_ack_valid": autopilot.get("real_run_ack_valid"),
                "real_run_authorized": real_run_authorized,
            },
            "_required": {"real_run_authorized": True},
        })

    target_status = summary.get("autopilot_targets")
    if isinstance(target_status, dict):
        target_observed = target_status.get("available") is True
        scoped_pending = int(target_status.get("pending_targets") or 0)
        eligible_targets = int(target_status.get("eligible_targets") or 0)
        exact_candidate_status = target_status.get("exact_real_candidate")
        if not isinstance(exact_candidate_status, dict):
            exact_candidate_status = {}
    else:
        target_observed = True
        scoped_pending = int(summary.get("pending_targets") or 0)
        eligible_targets = scoped_pending
        exact_candidate_status = {}
    target_eligible_platforms = _target_eligible_platforms(summary)
    real_capability_platforms = _real_capability_platforms(
        scoped_platforms
    ) if live_autopilot_scope else set()

    notification_delivery = summary.get("notification_delivery")
    notification_ready = _notification_gate_ready(summary)
    if summary.get("notification_channels_configured", 0) <= 0:
        notification_next_action = "configure_notification"
    elif (
        not isinstance(notification_delivery, dict)
        or notification_delivery.get("available") is not True
    ):
        notification_next_action = "restore_notification_delivery_observation"
    else:
        notification_next_action = "verify_notification_delivery"

    real_capability_next_action = "complete_adapter_probe"
    if live_autopilot_scope and real_ready <= 0:
        if not scoped_platforms:
            real_capability_next_action = "configure_autopilot_dispatch"
        else:
            for item in scoped_platforms:
                if int(item.get("safe_accounts") or 0) <= 0:
                    real_capability_next_action = "add_calibrated_account"
                    break
                if item.get("task_transport_ready") is not True:
                    real_capability_next_action = (
                        "restore_platform_task_transport"
                    )
                    break
                if item.get("action_adapter") is not True:
                    real_capability_next_action = "enable_real_adapter"
                    break
                if item.get("real_actions_ready") is not True:
                    real_capability_next_action = "complete_adapter_probe"
                    break

    checks = [
        {
            "code": "worker_online",
            "priority": "P0",
            "passed": summary.get("workers_online", 0) > 0,
            "title": "At least one worker is online",
            "detail": f"{summary.get('workers_online', 0)} Worker(s) are observable.",
            "_observed": {"workers_online": summary.get("workers_online", 0)},
            "_required": {"minimum": 1},
        },
        {
            "code": "all_platform_task_transports_ready",
            "priority": "P0",
            "passed": platform_count > 0 and transport_ready == platform_count,
            "title": "Every Autopilot-scope task transport is ready",
            "detail": (
                f"{transport_ready}/{platform_count} scoped transport(s) are "
                "ready."
                + (
                    " Blocked: " + ", ".join(blocked_transport_platforms)
                    if blocked_transport_platforms else ""
                )
            ),
            "_observed": {
                "ready": transport_ready,
                "total": platform_count,
                "blocked_platforms": blocked_transport_platforms,
            },
            "_required": {"ready_equals_total": True, "minimum_total": 1},
        },
        {
            "code": "real_run_deployment_capability",
            "priority": "P0",
            "passed": _deployment_real_run_capability(summary),
            "title": "Deployment real-run capability is available",
            "detail": (
                "The deployment-level REAL_RUN_ENABLED capability is available."
                if _deployment_real_run_capability(summary)
                else (
                    "The deployment-level REAL_RUN_ENABLED capability remains "
                    "disabled; no runtime switch can override this ceiling."
                )
            ),
            "_observed": {
                "deployment_real_run_enabled": (
                    _deployment_real_run_capability(summary)
                ),
                "source": "process_env.REAL_RUN_ENABLED",
            },
            "_required": {"deployment_real_run_enabled": True},
        },
        {
            "code": "real_run_global_switch",
            "priority": "P0",
            "passed": bool(summary.get("real_run_enabled")),
            "title": "Global real-run switch has production approval",
            "detail": (
                "Global real-run is enabled."
                if summary.get("real_run_enabled")
                else "Global real-run remains disabled by default."
            ),
            "_observed": {"real_run_enabled": bool(summary.get("real_run_enabled"))},
            "_required": {"real_run_enabled": True},
        },
        *runtime_checks,
        {
            "code": "notification_ready",
            "priority": "P0",
            "passed": notification_ready,
            "title": "A configured channel has a recent successful delivery",
            "detail": (
                f"{summary.get('notification_channels_configured', 0)} "
                "channel(s) are configured; "
                f"{(notification_delivery or {}).get('sent_count_24h', 0)} "
                f"successful delivery record(s) were observed in the last "
                f"{NOTIFICATION_DELIVERY_WINDOW_HOURS} hours."
            ),
            "_observed": {
                "configured_channels": summary.get(
                    "notification_channels_configured", 0
                ),
                "delivery_observation_available": (
                    notification_delivery.get("available")
                    if isinstance(notification_delivery, dict)
                    else False
                ),
                "sent_count_24h": (
                    notification_delivery.get("sent_count_24h", 0)
                    if isinstance(notification_delivery, dict)
                    else 0
                ),
                "last_success_at": (
                    notification_delivery.get("last_success_at")
                    if isinstance(notification_delivery, dict)
                    else None
                ),
                "last_success_channel": (
                    notification_delivery.get("last_success_channel")
                    if isinstance(notification_delivery, dict)
                    else None
                ),
                "blocker_code": (
                    notification_delivery.get("blocker_code")
                    if isinstance(notification_delivery, dict)
                    else "notification_delivery_status_unavailable"
                ),
            },
            "_required": {
                "configured_channels_minimum": 1,
                "delivery_observation_available": True,
                "sent_count_24h_minimum": 1,
                "last_success_at_required": True,
            },
            "_next_action_code": notification_next_action,
        },
        {
            "code": "target_pool_ready",
            "priority": "P0",
            "passed": target_observed and scoped_pending > 0,
            "title": "Autopilot scope contains an unexpired pending target",
            "detail": f"{scoped_pending} scoped pending/claimed target(s) were observed.",
            "_observed": {"available": target_observed, "pending_targets": scoped_pending},
            "_required": {"available": True, "minimum": 1},
        },
    ]
    if isinstance(target_status, dict):
        checks.append({
            "code": "autopilot_target_plan_ready",
            "priority": "P0",
            "passed": bool(
                target_observed
                and eligible_targets > 0
                and target_eligible_platforms
            ),
            "title": "At least one automatic target satisfies plan prerequisites",
            "detail": (
                f"{eligible_targets}/{target_status.get('observed_targets', 0)} "
                "observed target(s) satisfy Autopilot's Action Plan/rule gate."
            ),
            "_observed": {
                "available": target_observed,
                "eligible_targets": eligible_targets,
                "plan_ready_targets": target_status.get("plan_ready_targets", 0),
                "missing_plan_targets": target_status.get("missing_plan_targets", 0),
                "plan_blocker_counts": target_status.get("plan_blocker_counts", {}),
                "eligible_by_platform": target_status.get(
                    "eligible_by_platform", {}
                ),
                "observation_truncated": target_status.get("observation_truncated"),
            },
            "_required": {"available": True, "eligible_targets_minimum": 1},
        })
        exact_candidate_ready = bool(
            exact_candidate_status.get("available") is True
            and exact_candidate_status.get("ready") is True
            and int(exact_candidate_status.get("candidate_count") or 0) > 0
            and isinstance(exact_candidate_status.get("candidate"), dict)
            and str(
                exact_candidate_status["candidate"].get("platform") or ""
            ).strip().casefold() in real_capability_platforms
        )
        checks.append({
            "code": "autopilot_exact_real_candidate_ready",
            "priority": "P0",
            "passed": exact_candidate_ready,
            "title": "A target and lease-free account form one exact real-run candidate",
            "detail": (
                f"{int(exact_candidate_status.get('candidate_count') or 0)} "
                "exact target/account candidate(s) passed the bounded "
                "account-scoped evidence and target-validity snapshot."
            ),
            "_observed": {
                "available": exact_candidate_status.get("available") is True,
                "candidate_count": int(
                    exact_candidate_status.get("candidate_count") or 0
                ),
                "candidate": exact_candidate_status.get("candidate"),
                "candidate_platform_has_real_capability": bool(
                    isinstance(
                        exact_candidate_status.get("candidate"), dict
                    )
                    and str(
                        exact_candidate_status["candidate"].get(
                            "platform"
                        ) or ""
                    ).strip().casefold() in real_capability_platforms
                ),
                "blocker_code": exact_candidate_status.get("blocker_code"),
                "blocker_counts": exact_candidate_status.get(
                    "blocker_counts", {}
                ),
                "observed_targets": int(
                    exact_candidate_status.get("observed_targets") or 0
                ),
                "observation_limit": int(
                    exact_candidate_status.get("observation_limit") or 0
                ),
                "observation_truncated": bool(
                    exact_candidate_status.get("observation_truncated")
                ),
                "account_candidate_truncated_platforms": (
                    exact_candidate_status.get(
                        "account_candidate_truncated_platforms", []
                    )
                ),
            },
            "_required": {
                "available": True,
                "candidate_count_minimum": 1,
                "same_lottery_and_account": True,
                "account_lease_available": True,
                "execution_readiness_allowed": True,
                "target_valid": True,
                "candidate_platform_has_real_capability": True,
            },
        })
    else:
        checks.append({
            "code": "autopilot_exact_real_candidate_ready",
            "priority": "P0",
            "passed": False,
            "title": "A target and lease-free account form one exact real-run candidate",
            "detail": (
                "The exact target/account candidate observation is missing; "
                "production remains fail-closed."
            ),
            "_observed": {
                "available": False,
                "candidate_count": 0,
                "candidate": None,
                "blocker_code": (
                    "autopilot_exact_candidate_observation_unavailable"
                ),
                "blocker_counts": {},
                "observed_targets": 0,
                "observation_limit": 0,
                "observation_truncated": False,
                "account_candidate_truncated_platforms": [],
            },
            "_required": {
                "available": True,
                "candidate_count_minimum": 1,
                "same_lottery_and_account": True,
                "account_lease_available": True,
                "execution_readiness_allowed": True,
                "target_valid": True,
            },
        })
    checks.extend([
        {
            "code": "all_platforms_dry_ready",
            "priority": "P0",
            "passed": dry_supported > 0 and dry_ready == dry_supported,
            "title": "All dry-run-capable Autopilot platforms are ready",
            "detail": f"{dry_ready}/{dry_supported} scoped dry-run platform(s) are ready.",
            "_observed": {"ready": dry_ready, "supported": dry_supported},
            "_required": {"ready_equals_supported": True, "minimum_supported": 1},
        },
        {
            "code": "real_run_available",
            "priority": "P0",
            "passed": real_ready > 0,
            "title": "At least one scoped platform has real-action preflight capability",
            "detail": (
                f"{real_ready}/{platform_count} scoped platform(s) have a "
                "safe account, ready action adapter/probe evidence, and ready "
                "task transport. This check is independent of deployment "
                "real-run authorization and the global breaker."
            ),
            "_observed": {
                "capability_ready": real_ready,
                "total": platform_count,
                "platforms": [
                    {
                        "platform": item.get("platform"),
                        "safe_accounts": int(item.get("safe_accounts") or 0),
                        "real_actions_ready": bool(
                            item.get("real_actions_ready")
                        ),
                        "task_transport_ready": bool(
                            item.get("task_transport_ready")
                        ),
                    }
                    for item in scoped_platforms
                ] if live_autopilot_scope else [],
            },
            "_required": {
                "minimum_capability_ready": 1,
                "per_platform": [
                    "safe_accounts > 0",
                    "real_actions_ready = true",
                    "task_transport_ready = true",
                ],
                "independent_of": [
                    "real_run_global_switch",
                    "global_circuit_breaker_closed",
                ],
            },
            "_next_action_code": real_capability_next_action,
        },
        {
            "code": "active_proxy_exit",
            "priority": "P1",
            "passed": summary.get("active_proxy_exits", 0) > 0,
            "title": "Optional proxy hardening is available",
            "detail": f"{summary.get('active_proxy_exits', 0)} active proxy exit(s) are available; this advisory does not block production readiness.",
            "_observed": {"active_proxy_exits": summary.get("active_proxy_exits", 0)},
            "_required": {"recommended_minimum": 1},
        },
        {
            "code": "safe_accounts_proxied",
            "priority": "P1",
            "passed": safe_accounts > 0 and summary.get("proxied_safe_accounts", 0) >= safe_accounts,
            "title": "Optional ready-account proxy isolation is complete",
            "detail": f"{summary.get('proxied_safe_accounts', 0)}/{safe_accounts} ready account(s) have active exits; this advisory does not block production readiness.",
            "_observed": {"proxied": summary.get("proxied_safe_accounts", 0), "ready_accounts": safe_accounts},
            "_required": {"recommended_proxied_equals_ready": True},
        },
        {
            "code": "recent_risk_clear",
            "priority": "P1",
            "passed": summary.get("recent_risk_events_24h", 0) == 0,
            "title": "Recent risk review is clear",
            "detail": f"{summary.get('recent_risk_events_24h', 0)} recent risk event(s) were recorded; this advisory does not independently block production readiness.",
            "_observed": {"recent_risk_events_24h": summary.get("recent_risk_events_24h", 0)},
            "_required": {"recommended_maximum": 0},
        },
    ])
    return [
        _structured_production_check(check, scope_names)
        for check in checks
    ]


@router.get("/external-action-intents")
async def external_action_intents(
    limit: int = Query(default=50, ge=1, le=200),
    status: str | None = Query(default=None),
    task_id: str | None = Query(default=None, min_length=1, max_length=64),
):
    """Read-only, payload-free view of durable external action attempts."""

    normalized_status = str(status or "").strip().lower()
    if normalized_status and normalized_status not in EXTERNAL_ACTION_INTENT_STATUSES:
        raise HTTPException(status_code=400, detail="Invalid external action intent status")
    normalized_task_id = str(task_id or "").strip()
    clauses = []
    values = {"limit": limit}
    if normalized_status:
        clauses.append("eai.status = :status")
        values["status"] = normalized_status
    if normalized_task_id:
        clauses.append("eai.task_id = :task_id")
        values["task_id"] = normalized_task_id
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = await database.fetch_all(
        f"""SELECT eai.intent_id, eai.task_id, eai.account_id, eai.lottery_id,
                   eai.lease_id, eai.lease_generation, eai.action,
                   eai.payload_hash, eai.status, eai.effect_certainty,
                   eai.attempt_no,
                   eai.started_at, eai.completed_at, eai.outcome,
                   CASE WHEN eai.remote_ref IS NULL THEN 0 ELSE 1 END AS has_remote_ref,
                   CASE WHEN eai.error_message IS NULL THEN 0 ELSE 1 END AS has_error,
                   eai.created_at, eai.updated_at,
                   tr.status AS task_status, tr.task_mode,
                   tr.reconciliation_required,
                   l.platform
            FROM external_action_intents eai
            JOIN task_runs tr ON tr.task_id = eai.task_id
            JOIN lotteries l ON l.id = eai.lottery_id
            {where}
            ORDER BY eai.updated_at DESC, eai.intent_id DESC
            LIMIT :limit""",
        values,
    )
    return {
        "items": [_intent_observation(row) for row in rows],
        "count": len(rows),
        "payload_exposed": False,
    }


@router.get("/reconciliation")
async def reconciliation_queue(
    limit: int = Query(default=50, ge=1, le=200),
):
    """Tasks quarantined by structured reconciliation state, newest first."""

    rows = await database.fetch_all(
        """SELECT tr.task_id, tr.account_id, tr.lottery_id, tr.task_mode,
                  tr.status AS task_status, tr.reconciliation_required,
                  tr.created_at, tr.started_at, tr.finished_at,
                  l.platform,
                  COUNT(eai.intent_id) AS intent_count,
                  COALESCE(SUM(eai.status = 'started'), 0) AS started_intent_count,
                  COALESCE(SUM(eai.status = 'unknown'), 0) AS unknown_intent_count,
                  COALESCE(SUM(eai.effect_certainty = 'unknown'), 0) AS unknown_effect_count,
                  COALESCE(SUM(eai.status = 'succeeded'), 0) AS succeeded_intent_count,
                  MAX(eai.updated_at) AS latest_intent_at
           FROM task_runs tr
           JOIN lotteries l ON l.id = tr.lottery_id
           LEFT JOIN external_action_intents eai ON eai.task_id = tr.task_id
           WHERE tr.reconciliation_required = 1
              OR EXISTS (
                   SELECT 1 FROM external_action_intents unsettled
                   WHERE unsettled.task_id = tr.task_id
                     AND unsettled.status IN ('started', 'unknown')
              )
           GROUP BY tr.task_id, tr.account_id, tr.lottery_id, tr.task_mode,
                    tr.status, tr.reconciliation_required, tr.created_at,
                    tr.started_at, tr.finished_at, l.platform
           ORDER BY COALESCE(MAX(eai.updated_at), tr.finished_at, tr.started_at, tr.created_at) DESC
           LIMIT :limit""",
        {"limit": limit},
    )
    items = []
    for row in rows:
        item = dict(row)
        item["reconciliation_required"] = bool(item.get("reconciliation_required"))
        for field in (
            "intent_count",
            "started_intent_count",
            "unknown_intent_count",
            "unknown_effect_count",
            "succeeded_intent_count",
        ):
            item[field] = int(item.get(field) or 0)
        items.append(item)
    return {"items": items, "count": len(items), "payload_exposed": False}



@router.post("/worker/restart")

async def restart_worker(request: Request):
    require_min_role(request, "admin")
    require_confirmation(request)

    await redis.publish("worker:reload", "restart_requested")
    await audit_event(
        request,
        action="worker.restart",
        resource_type="worker",
        resource_id="all",
        result="signaled",
        risk_level="high",
    )

    return {"status": "restart_requested"}



@router.post("/worker/reload")

async def reload_worker(request: Request):
    require_min_role(request, "admin")
    require_confirmation(request)

    await redis.publish("worker:reload", "1")
    await audit_event(
        request,
        action="worker.reload",
        resource_type="worker",
        resource_id="all",
        result="signaled",
        risk_level="high",
    )

    return {"status": "reload_signaled"}


def _autopilot_heartbeat_detail(
    payload: AutopilotHeartbeatReport,
) -> str:
    detail = {
        "contract_version": AUTOPILOT_HEARTBEAT_DETAIL_VERSION,
        "enabled": payload.enabled,
        "deployment_real_run_enabled": (
            payload.deployment_real_run_enabled
        ),
        "real_run_ack_valid": payload.real_run_ack_valid,
        "platform_allowlist": sorted(set(payload.platform_allowlist)),
        "platform_allowlist_valid": payload.platform_allowlist_valid,
        "poll_interval_seconds": payload.poll_interval_seconds,
        "last_round": {
            "status": payload.round_status,
            "selected": payload.selected,
            "dispatched": payload.dispatched,
            "failures": payload.failures,
            "probes_requested": payload.probes_requested,
            "deferred": payload.deferred,
        },
    }
    return json.dumps(
        detail,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _autopilot_database_status(payload: AutopilotHeartbeatReport) -> str:
    if not payload.enabled:
        return "disabled"
    if (
        payload.round_status == "error"
        or not payload.platform_allowlist_valid
        or not payload.platform_allowlist
    ):
        return "degraded"
    return "ok"


@router.post("/autopilot/heartbeat")
async def record_autopilot_heartbeat(
    payload: AutopilotHeartbeatReport,
    request: Request,
):
    """Persist bounded Autopilot telemetry through the authenticated API."""

    require_min_role(request, "admin")
    await database.execute(
        """INSERT INTO worker_heartbeats
             (worker_id, service_name, status, pid, detail, last_seen_at)
           VALUES (:worker_id, :service_name, :status, NULL, :detail, NOW())
           ON DUPLICATE KEY UPDATE
             service_name = :service_name,
             status = :status,
             pid = NULL,
             detail = :detail,
             last_seen_at = NOW(),
             updated_at = NOW()""",
        {
            "worker_id": AUTOPILOT_HEARTBEAT_WORKER_ID,
            "service_name": AUTOPILOT_HEARTBEAT_SERVICE_NAME,
            "status": _autopilot_database_status(payload),
            "detail": _autopilot_heartbeat_detail(payload),
        },
    )
    return {"status": "recorded"}


def _bounded_autopilot_number(
    value,
    *,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(parsed):
        return default
    return min(max(parsed, minimum), maximum)


def _bounded_autopilot_count(value) -> int:
    return int(_bounded_autopilot_number(
        value,
        default=0,
        minimum=0,
        maximum=100,
    ))


def _empty_autopilot_runtime_status(*, available: bool) -> dict:
    return {
        "available": available,
        "reported": False,
        "fresh": False,
        "status": "unknown",
        "last_seen_at": None,
        "heartbeat_age_seconds": None,
        "stale_after_seconds": None,
        "enabled": False,
        "dispatch_configured": False,
        "deployment_real_run_enabled": False,
        "real_run_ack_valid": False,
        "real_run_authorized": False,
        "platform_allowlist": [],
        "platform_allowlist_valid": False,
        "poll_interval_seconds": None,
        "last_round": None,
    }


async def _autopilot_runtime_status() -> dict:
    try:
        row = await asyncio.wait_for(
            database.fetch_one(
                """SELECT status, detail, last_seen_at,
                          TIMESTAMPDIFF(
                            SECOND, last_seen_at, NOW()
                          ) AS heartbeat_age_seconds
                     FROM worker_heartbeats
                    WHERE worker_id = :worker_id
                      AND service_name = :service_name""",
                {
                    "worker_id": AUTOPILOT_HEARTBEAT_WORKER_ID,
                    "service_name": AUTOPILOT_HEARTBEAT_SERVICE_NAME,
                },
            ),
            timeout=TASK_METRICS_OPERATION_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        structured_log(
            "warning",
            "autopilot_heartbeat_status_unavailable",
            cause_type=type(exc).__name__,
        )
        return _empty_autopilot_runtime_status(available=False)
    if not row:
        return _empty_autopilot_runtime_status(available=True)

    parsed_detail = parse_json_field(row["detail"])
    detail = parsed_detail if isinstance(parsed_detail, dict) else {}
    raw_platforms = detail.get("platform_allowlist")
    platforms = sorted({
        platform
        for platform in (
            raw_platforms if isinstance(raw_platforms, list) else []
        )
        if platform in PLATFORM_IDS
    })
    poll_interval = _bounded_autopilot_number(
        detail.get("poll_interval_seconds"),
        default=60,
        minimum=1,
        maximum=3600,
    )
    heartbeat_age = _bounded_autopilot_number(
        row["heartbeat_age_seconds"],
        default=-1,
        minimum=0,
        maximum=86_400 * 365,
    )
    heartbeat_age_seconds = (
        int(heartbeat_age) if heartbeat_age >= 0 else None
    )
    stale_after_seconds = max(
        AUTOPILOT_HEARTBEAT_MIN_STALE_SECONDS,
        int(poll_interval * 2 + 30),
    )
    enabled = detail.get("enabled") is True
    deployment_real_run_enabled = (
        detail.get("deployment_real_run_enabled") is True
    )
    real_run_ack_valid = detail.get("real_run_ack_valid") is True
    allowlist_valid = (
        detail.get("platform_allowlist_valid") is True
        and isinstance(raw_platforms, list)
        and len(platforms) == len(raw_platforms)
    )
    raw_round = detail.get("last_round")
    round_detail = raw_round if isinstance(raw_round, dict) else {}
    round_status = str(round_detail.get("status") or "")
    last_round = None
    if round_status in {"ok", "error", "disabled"}:
        last_round = {
            "status": round_status,
            "selected": _bounded_autopilot_count(
                round_detail.get("selected")
            ),
            "dispatched": _bounded_autopilot_count(
                round_detail.get("dispatched")
            ),
            "failures": _bounded_autopilot_count(
                round_detail.get("failures")
            ),
            "probes_requested": _bounded_autopilot_count(
                round_detail.get("probes_requested")
            ),
            "deferred": _bounded_autopilot_count(
                round_detail.get("deferred")
            ),
        }
    stored_status = str(row["status"] or "").strip().casefold()
    if stored_status not in {"ok", "degraded", "disabled"}:
        stored_status = "unknown"
    return {
        "available": True,
        "reported": True,
        "fresh": (
            heartbeat_age_seconds is not None
            and heartbeat_age_seconds <= stale_after_seconds
        ),
        "status": stored_status,
        "last_seen_at": row["last_seen_at"],
        "heartbeat_age_seconds": heartbeat_age_seconds,
        "stale_after_seconds": stale_after_seconds,
        "enabled": enabled,
        "dispatch_configured": (
            enabled and allowlist_valid and bool(platforms)
        ),
        "deployment_real_run_enabled": deployment_real_run_enabled,
        "real_run_ack_valid": real_run_ack_valid,
        "real_run_authorized": (
            enabled
            and deployment_real_run_enabled
            and real_run_ack_valid
        ),
        "platform_allowlist": platforms,
        "platform_allowlist_valid": allowlist_valid,
        "poll_interval_seconds": poll_interval,
        "last_round": last_round,
    }


@router.get("/runtime/settings")
async def runtime_settings():
    breaker = await database.fetch_one(
        "SELECT status, reason, opened_at, updated_at FROM circuit_breakers WHERE scope = 'global'"
    )
    setting = await database.fetch_one(
        """SELECT setting_value, updated_at FROM runtime_settings
           WHERE setting_key = 'real_run_enabled'"""
    )
    setting_projection = dict(setting) if setting else {}
    deployment_real_run_enabled = bool(settings.real_run_enabled)
    runtime_real_run_enabled = parse_bool(
        setting_projection.get("setting_value")
    )
    effective_real_run_enabled = bool(
        deployment_real_run_enabled and runtime_real_run_enabled
    )
    return {
        "deployment_real_run_enabled": deployment_real_run_enabled,
        "runtime_real_run_enabled": runtime_real_run_enabled,
        "real_run_enabled": effective_real_run_enabled,
        "real_run_setting_updated_at": setting_projection.get("updated_at"),
        "real_run_control": {
            "deployment_capability": {
                "enabled": deployment_real_run_enabled,
                "source": "process_env.REAL_RUN_ENABLED",
                "change_requires_service_restart": True,
            },
            "runtime_switch": {
                "enabled": runtime_real_run_enabled,
                "setting_key": "real_run_enabled",
                "updated_at": setting_projection.get("updated_at"),
            },
            "effective_enabled": effective_real_run_enabled,
            "technical_prerequisites_validated_on_enable": True,
            "global_circuit_breaker_independent": True,
            "final_authorization_gate_codes": sorted(
                REAL_RUN_FINAL_AUTHORIZATION_GATE_CODES
            ),
        },
        "inflight_real_runs": await _real_run_inflight_counts(),
        "worker_gate_contract": _worker_gate_contract(),
        "global_circuit_breaker": dict(breaker) if breaker else None,
        "autopilot": await _autopilot_runtime_status(),
    }


@router.put("/runtime/settings/real-run")
async def update_real_run_setting(payload: RealRunSettingUpdate, request: Request):
    actor = require_min_role(request, "owner")
    require_confirmation(request)
    if payload.enabled:
        if not settings.real_run_enabled:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "real_run_deployment_capability_disabled",
                },
            )
        try:
            readiness_snapshot = await _readiness_snapshot()
        except Exception as exc:
            structured_log(
                "warning",
                "real_run_prerequisite_observation_unavailable",
                cause_type=type(exc).__name__,
            )
            blocker_codes = [
                "production_readiness_observation_unavailable"
            ]
        else:
            blocker_codes = _real_run_technical_prerequisite_blocker_codes(
                readiness_snapshot
            )
        if blocker_codes:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "real_run_prerequisites_not_ready",
                    "blocker_codes": blocker_codes,
                },
            )
    await set_runtime_setting("real_run_enabled", "true" if payload.enabled else "false")
    inflight = await _real_run_inflight_counts()
    contract = _worker_gate_contract()
    deployment_real_run_enabled = bool(settings.real_run_enabled)
    runtime_real_run_enabled = bool(payload.enabled)
    result = {
        "status": "updated",
        "deployment_real_run_enabled": deployment_real_run_enabled,
        "runtime_real_run_enabled": runtime_real_run_enabled,
        "real_run_enabled": bool(
            deployment_real_run_enabled and runtime_real_run_enabled
        ),
        "inflight_real_runs": inflight,
        "worker_gate_contract": contract,
        "enforcement": (
            "enabled_for_fresh_worker_gate_checks"
            if payload.enabled
            else "disabled_at_next_worker_gate_check"
        ),
    }
    await audit_event(
        request,
        action="runtime.real_run.update",
        resource_type="runtime_setting",
        resource_id="real_run_enabled",
        result="enabled" if payload.enabled else "disabled",
        risk_level="critical" if payload.enabled else "high",
        detail={
            "enabled": payload.enabled,
            "deployment_real_run_enabled": deployment_real_run_enabled,
            "runtime_real_run_enabled": runtime_real_run_enabled,
            "global_circuit_breaker": "unchanged",
            "inflight_real_runs": inflight,
            "worker_gate_contract": contract,
        },
    )
    await record_event(
        aggregate="runtime",
        aggregate_id="real_run_enabled",
        event_type="RealRunRuntimeSettingChanged",
        payload=result,
        actor_type="operator",
        actor_id=actor["actor_id"],
    )
    return result


@router.post("/runtime/rollback")
async def runtime_rollback(payload: RuntimeRollbackRequest, request: Request):
    actor = require_min_role(request, "owner")
    require_confirmation(request)
    reason = (payload.reason or "manual runtime rollback").strip()[:255]

    queued_rows = await database.fetch_all(
        """SELECT task_id, lottery_id
           FROM task_runs
           WHERE task_mode = 'real_run'
             AND status = 'queued'"""
    )
    running_count = await database.fetch_one(
        """SELECT COUNT(*) AS cnt
           FROM task_runs
           WHERE task_mode = 'real_run'
             AND status = 'running'"""
    )

    await set_runtime_setting("real_run_enabled", "false")
    await database.execute(
        """INSERT INTO circuit_breakers (scope, status, reason, opened_at)
           VALUES ('global', 'open', :reason, NOW())
           ON DUPLICATE KEY UPDATE
             status = 'open',
             reason = :reason,
             opened_at = NOW(),
             updated_at = NOW()""",
        {"reason": reason},
    )
    breaker = await database.fetch_one(
        "SELECT status FROM circuit_breakers WHERE scope = 'global'"
    )
    if not breaker or str(breaker["status"] or "").strip().lower() != "open":
        raise HTTPException(500, detail="Global circuit breaker write was not persisted")
    await database.execute(
        """UPDATE lotteries l
           JOIN task_runs tr ON tr.lottery_id = l.id
           SET l.status = 'pending',
               l.execution_lock = NULL,
               l.locked_at = NULL
           WHERE tr.task_mode = 'real_run'
             AND tr.status = 'queued'"""
    )
    await database.execute(
        """UPDATE task_runs
           SET status = 'failed',
               error_message = :error_message,
               finished_at = NOW()
           WHERE task_mode = 'real_run'
             AND status = 'queued'""",
        {"error_message": f"Runtime rollback: {reason}"},
    )
    await redis.publish("worker:reload", "runtime_rollback")
    await redis.xadd(
        "notify_events",
        {
            "title": "DPMS runtime rollback applied",
            "content": f"Real-run disabled and global circuit breaker opened. Reason: {reason}",
            "status": "rollback",
            "channels": "all",
        },
    )

    queued_task_ids = [row["task_id"] for row in queued_rows]
    result = {
        "status": "rollback_applied",
        "real_run_enabled": False,
        "circuit_breaker": "global=open",
        "queued_real_runs_cancelled": len(queued_task_ids),
        "running_real_runs_observed": int(running_count["cnt"] if running_count else 0),
        "queued_task_ids": queued_task_ids,
        "reason": reason,
    }
    await audit_event(
        request,
        action="runtime.rollback",
        resource_type="runtime",
        resource_id="global",
        result="applied",
        risk_level="critical",
        detail=result,
    )
    await record_event(
        aggregate="runtime",
        aggregate_id="global",
        event_type="RuntimeRollbackApplied",
        payload=result,
        actor_type="operator",
        actor_id=actor["actor_id"],
    )
    return result
