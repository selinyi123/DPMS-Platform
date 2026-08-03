"""Independent, fail-closed dispatcher for the DPMS strategy queue.

The service deliberately talks to Core through its public HTTP API instead of
importing database or dispatch internals.  The strategy engine remains the
authority for the next safe mode (dry_run -> shadow_run -> real_run), while
Core's normal dispatch endpoint remains the final policy and audit authority.
"""

from __future__ import annotations

import asyncio
import hmac
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

import httpx

from app.utils.log import structured_log
from shared.platform_ids import PLATFORM_IDS


SUPPORTED_MODES = frozenset({"dry_run", "shadow_run", "real_run"})
PLAN_REQUIRED_VALIDATION_PLATFORMS = frozenset(
    {"xiaohongshu", "douyin", "weibo"}
)
REAL_RUN_ACK_VALUE = "I_ACKNOWLEDGE_DPMS_AUTOPILOT_REAL_RUN"
AUTOPILOT_HEALTH_PATH = Path("/tmp/autopilot-health")
ACTIVE_PROBE_STATUSES = frozenset({"queued", "running"})
# Re-probe before Core's 24-hour execution-evidence window can expire.  The
# lower bound tolerates a database/container timezone offset without accepting
# an arbitrarily future timestamp as fresh.
PROBE_REUSE_WINDOW = timedelta(hours=15)
PROBE_FUTURE_TOLERANCE = timedelta(hours=12)
AUTOPILOT_HEARTBEAT_PATH = "/api/metrics/autopilot/heartbeat"
AUTOPILOT_HEARTBEAT_TIMEOUT_SECONDS = 5.0
EMPTY_ROUND_SUMMARY = {
    "selected": 0,
    "dispatched": 0,
    "failures": 0,
    "probes_requested": 0,
    "deferred": 0,
}


def _parse_bool(value: Any) -> bool:
    return str(value or "").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
        "enabled",
    }


def _bounded_int(
    value: Any,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return min(max(parsed, minimum), maximum)


def _bounded_float(
    value: Any,
    *,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        return default
    return min(max(parsed, minimum), maximum)


def _platform_allowlist(value: Any) -> frozenset[str]:
    return frozenset(
        platform
        for platform in (
            item.strip().casefold() for item in str(value or "").split(",")
        )
        if platform
    )


def _core_api_url(value: Any) -> str:
    url = str(value or "http://core-api:8000").strip().rstrip("/")
    parsed = urlparse(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("DPMS_AUTOPILOT_CORE_API_URL must be a plain HTTP(S) origin")
    return url


@dataclass(frozen=True)
class AutopilotConfig:
    core_api_url: str
    admin_token: str
    enabled: bool
    real_run_enabled: bool
    real_run_ack: str
    platform_allowlist: frozenset[str]
    round_limit: int = 10
    scan_limit: int = 100
    failure_limit: int = 3
    poll_interval_seconds: float = 60.0
    request_timeout_seconds: float = 20.0

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> "AutopilotConfig":
        env = os.environ if environ is None else environ
        return cls(
            core_api_url=_core_api_url(env.get("DPMS_AUTOPILOT_CORE_API_URL")),
            admin_token=str(env.get("ADMIN_TOKEN") or ""),
            enabled=_parse_bool(env.get("DPMS_AUTOPILOT_ENABLED")),
            real_run_enabled=_parse_bool(env.get("REAL_RUN_ENABLED")),
            real_run_ack=str(env.get("DPMS_AUTOPILOT_REAL_RUN_ACK") or ""),
            platform_allowlist=_platform_allowlist(
                env.get("DPMS_AUTOPILOT_PLATFORMS")
            ),
            round_limit=_bounded_int(
                env.get("DPMS_AUTOPILOT_ROUND_LIMIT"),
                default=10,
                minimum=1,
                maximum=100,
            ),
            scan_limit=_bounded_int(
                env.get("DPMS_AUTOPILOT_SCAN_LIMIT"),
                default=100,
                minimum=1,
                maximum=100,
            ),
            failure_limit=_bounded_int(
                env.get("DPMS_AUTOPILOT_FAILURE_LIMIT"),
                default=3,
                minimum=1,
                maximum=100,
            ),
            poll_interval_seconds=_bounded_float(
                env.get("DPMS_AUTOPILOT_POLL_SECONDS"),
                default=60.0,
                minimum=1.0,
                maximum=3600.0,
            ),
            request_timeout_seconds=_bounded_float(
                env.get("DPMS_AUTOPILOT_REQUEST_TIMEOUT_SECONDS"),
                default=20.0,
                minimum=1.0,
                maximum=120.0,
            ),
        )


@dataclass(frozen=True)
class DispatchCandidate:
    lottery_id: int
    platform: str
    mode: str
    account_id: int | None = None
    probe_required: bool = False
    dry_success: int = 0
    shadow_success: int = 0
    failed_runs: int = 0


def real_run_authorized(config: AutopilotConfig) -> bool:
    """Require all deployment-level acknowledgements before real dispatch."""

    return bool(
        config.enabled
        and config.real_run_enabled
        and hmac.compare_digest(config.real_run_ack, REAL_RUN_ACK_VALUE)
    )


def select_candidates(
    payload: Any,
    config: AutopilotConfig,
) -> list[DispatchCandidate]:
    """Select only explicitly allowed, currently dispatchable recommendations."""

    if not config.enabled or not config.platform_allowlist:
        return []
    items = payload.get("items") if isinstance(payload, Mapping) else None
    if not isinstance(items, list):
        return []

    selected: list[DispatchCandidate] = []
    for raw in items:
        if not isinstance(raw, Mapping):
            continue
        platform = str(raw.get("platform") or "").strip().casefold()
        mode = str(raw.get("recommended_mode") or "").strip().casefold()
        if platform not in config.platform_allowlist or mode not in SUPPORTED_MODES:
            continue
        try:
            lottery_id = int(raw.get("lottery_id"))
            active_runs = int(raw.get("active_runs") or 0)
            dry_success = int(raw.get("dry_success") or 0)
            shadow_success = int(raw.get("shadow_success") or 0)
            failed_runs = int(raw.get("failed_runs") or 0)
        except (TypeError, ValueError):
            continue
        if (
            lottery_id <= 0
            or active_runs > 0
            or failed_runs >= config.failure_limit
        ):
            continue
        readiness = raw.get("execution_readiness")
        if (
            platform in PLAN_REQUIRED_VALIDATION_PLATFORMS
            and (
                not isinstance(readiness, Mapping)
                or readiness.get("action_plan_ready") is not True
                or readiness.get("rule_snapshot_ready") is not True
            )
        ):
            # These platform paths bind even their no-side-effect validation
            # to the reviewed source rule and Action Plan v2. Repeatedly
            # calling dispatch cannot create that operator-owned evidence, so
            # leave the target visible as a gate instead of retrying forever.
            continue
        # The strategy endpoint can keep recommending the last non-mutating
        # stage while real-run is disabled or another evidence gate is still
        # missing. Re-dispatching that already-successful stage every poll
        # would create an infinite task loop, so Autopilot advances each
        # validation rung at most once for the current target history.
        if mode == "dry_run" and dry_success > 0:
            continue
        readiness_blockers = raw.get("execution_readiness_blockers")
        probe_required = bool(
            mode == "shadow_run"
            and shadow_success > 0
            and raw.get("execution_readiness_ready") is not True
            and isinstance(readiness_blockers, list)
            and "exact_execution_evidence_required" in readiness_blockers
        )
        if mode == "shadow_run" and shadow_success > 0 and not probe_required:
            continue

        if mode == "real_run":
            if not real_run_authorized(config):
                continue
            if (
                raw.get("real_run_enabled") is not True
                or raw.get("target_valid") is not True
                or raw.get("breaker_allowed") is not True
                or raw.get("execution_readiness_ready") is not True
            ):
                continue

        account_id = None
        recommended_account = raw.get("recommended_account")
        if isinstance(recommended_account, Mapping):
            try:
                parsed_account_id = int(recommended_account.get("account_id"))
            except (TypeError, ValueError):
                parsed_account_id = 0
            if parsed_account_id > 0:
                account_id = parsed_account_id

        selected.append(
            DispatchCandidate(
                lottery_id=lottery_id,
                platform=platform,
                mode=mode,
                account_id=account_id,
                probe_required=probe_required,
                dry_success=dry_success,
                shadow_success=shadow_success,
                failed_runs=failed_runs,
            )
        )
        if len(selected) >= config.round_limit:
            break
    return selected


async def fetch_strategy_queue(
    client: httpx.AsyncClient,
    config: AutopilotConfig,
) -> dict[str, Any]:
    response = await client.get(
        "/api/lotteries/strategy/queue",
        # Scan more than one dispatch round so a high-ranked platform outside
        # this process' allowlist cannot starve permitted candidates.
        params={"limit": config.scan_limit},
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("strategy queue response must be an object")
    return payload


async def dispatch_candidate(
    client: httpx.AsyncClient,
    config: AutopilotConfig,
    candidate: DispatchCandidate,
) -> dict[str, Any]:
    if candidate.mode == "real_run" and not real_run_authorized(config):
        raise RuntimeError("autopilot_real_run_not_authorized")

    body: dict[str, Any] = {
        "mode": candidate.mode,
        "dry_run": candidate.mode != "real_run",
        "confirm": candidate.mode == "real_run",
    }
    if candidate.account_id is not None:
        body["account_id"] = candidate.account_id
    headers = (
        {"x-confirm-action": "true"}
        if candidate.mode == "real_run"
        else None
    )
    response = await client.post(
        f"/api/lotteries/{candidate.lottery_id}/dispatch",
        json=body,
        headers=headers,
    )
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, dict) else {"status": "accepted"}


async def _fetch_list(
    client: httpx.AsyncClient,
    path: str,
    config: AutopilotConfig,
) -> list[dict[str, Any]]:
    response = await client.get(path, params={"limit": config.scan_limit})
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise ValueError(f"{path} response must be a list")
    return [dict(item) for item in payload if isinstance(item, Mapping)]


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def latest_successful_shadow_account(
    task_runs: list[dict[str, Any]],
    lottery_id: int,
    platform: str = "bilibili",
) -> int | None:
    """Return the account bound to the newest successful platform shadow."""

    for run in task_runs:
        if (
            _positive_int(run.get("lottery_id")) == lottery_id
            and str(run.get("platform") or "").strip().casefold() == platform
            and str(run.get("task_mode") or "").strip().casefold()
            == "shadow_run"
            and str(run.get("status") or "").strip().casefold() == "succeeded"
        ):
            return _positive_int(run.get("account_id"))
    return None


def _probe_finished_recently(value: Any) -> bool:
    raw = str(value or "").strip()
    if not raw:
        return False
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)
    return -PROBE_FUTURE_TOLERANCE <= age <= PROBE_REUSE_WINDOW


def platform_probe_decision(
    probes: list[dict[str, Any]],
    *,
    lottery_id: int,
    account_id: int,
    platform: str = "bilibili",
    failure_limit: int,
) -> str:
    """Return request/wait/succeeded/failure_limit for an exact probe."""

    matching = [
        probe
        for probe in probes
        if _positive_int(probe.get("lottery_id")) == lottery_id
        and _positive_int(probe.get("account_id")) == account_id
        and str(probe.get("platform") or "").strip().casefold() == platform
    ]
    if not matching:
        return "request"
    latest_status = str(matching[0].get("status") or "").strip().casefold()
    if latest_status in ACTIVE_PROBE_STATUSES:
        return "wait"
    if latest_status == "succeeded" and _probe_finished_recently(
        matching[0].get("finished_at")
    ):
        return "succeeded"
    failed = sum(
        str(probe.get("status") or "").strip().casefold() == "failed"
        for probe in matching
    )
    return "failure_limit" if failed >= failure_limit else "request"


def bilibili_probe_decision(
    probes: list[dict[str, Any]],
    *,
    lottery_id: int,
    account_id: int,
    failure_limit: int,
) -> str:
    """Backward-compatible wrapper for the original Bilibili-only helper."""

    return platform_probe_decision(
        probes,
        lottery_id=lottery_id,
        account_id=account_id,
        platform="bilibili",
        failure_limit=failure_limit,
    )


async def request_platform_probe(
    client: httpx.AsyncClient,
    *,
    lottery_id: int,
    account_id: int,
) -> None:
    response = await client.post(
        f"/api/lotteries/{lottery_id}/probe",
        json={"account_id": account_id},
    )
    response.raise_for_status()


def write_health_heartbeat(path: Path = AUTOPILOT_HEALTH_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


def build_autopilot_heartbeat_payload(
    config: AutopilotConfig,
    *,
    round_status: str,
    summary: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Project runtime state without copying tokens or acknowledgement text."""

    known_platforms = sorted(
        platform
        for platform in config.platform_allowlist
        if platform in PLATFORM_IDS
    )
    counts = dict(EMPTY_ROUND_SUMMARY)
    if summary is not None:
        for key in counts:
            counts[key] = _bounded_int(
                summary.get(key),
                default=0,
                minimum=0,
                maximum=100,
            )
    return {
        "enabled": config.enabled,
        "deployment_real_run_enabled": config.real_run_enabled,
        "real_run_ack_valid": hmac.compare_digest(
            config.real_run_ack,
            REAL_RUN_ACK_VALUE,
        ),
        "platform_allowlist": known_platforms,
        "platform_allowlist_valid": (
            len(known_platforms) == len(config.platform_allowlist)
        ),
        "poll_interval_seconds": config.poll_interval_seconds,
        "round_status": round_status,
        **counts,
    }


async def report_autopilot_heartbeat(
    client: httpx.AsyncClient,
    config: AutopilotConfig,
    *,
    round_status: str,
    summary: Mapping[str, Any] | None = None,
) -> bool:
    """Best-effort telemetry: observability can never fail a strategy round."""

    try:
        response = await client.post(
            AUTOPILOT_HEARTBEAT_PATH,
            json=build_autopilot_heartbeat_payload(
                config,
                round_status=round_status,
                summary=summary,
            ),
            timeout=min(
                AUTOPILOT_HEARTBEAT_TIMEOUT_SECONDS,
                config.request_timeout_seconds,
            ),
        )
        response.raise_for_status()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        structured_log(
            "warning",
            "autopilot_heartbeat_report_failed",
            cause_type=type(exc).__name__,
        )
        return False
    return True


async def run_round(
    client: httpx.AsyncClient,
    config: AutopilotConfig,
) -> dict[str, int]:
    queue = await fetch_strategy_queue(client, config)
    candidates = select_candidates(queue, config)
    dispatched = 0
    failures = 0
    probes_requested = 0
    deferred = 0
    task_runs: list[dict[str, Any]] | None = None
    probes: list[dict[str, Any]] | None = None

    for candidate in candidates:
        try:
            if candidate.probe_required:
                if task_runs is None or probes is None:
                    task_runs, probes = await asyncio.gather(
                        _fetch_list(
                            client,
                            "/api/lotteries/tasks/runs",
                            config,
                        ),
                        _fetch_list(client, "/api/lotteries/probes", config),
                    )
                shadow_account_id = latest_successful_shadow_account(
                    task_runs,
                    candidate.lottery_id,
                    candidate.platform,
                )
                if shadow_account_id is None:
                    deferred += 1
                    structured_log(
                        "warning",
                        "autopilot_platform_probe_deferred",
                        lottery_id=candidate.lottery_id,
                        platform=candidate.platform,
                        reason="successful_shadow_account_not_found",
                    )
                    continue
                decision = platform_probe_decision(
                    probes,
                    lottery_id=candidate.lottery_id,
                    account_id=shadow_account_id,
                    platform=candidate.platform,
                    failure_limit=config.failure_limit,
                )
                if decision != "request":
                    deferred += 1
                    structured_log(
                        "info",
                        "autopilot_platform_probe_deferred",
                        lottery_id=candidate.lottery_id,
                        platform=candidate.platform,
                        account_id=shadow_account_id,
                        reason=decision,
                    )
                    continue
                await request_platform_probe(
                    client,
                    lottery_id=candidate.lottery_id,
                    account_id=shadow_account_id,
                )
                probes_requested += 1
                structured_log(
                    "info",
                    "autopilot_platform_probe_accepted",
                    lottery_id=candidate.lottery_id,
                    platform=candidate.platform,
                    account_id=shadow_account_id,
                )
                continue
            await dispatch_candidate(client, config, candidate)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failures += 1
            structured_log(
                "warning",
                "autopilot_dispatch_failed",
                lottery_id=candidate.lottery_id,
                platform=candidate.platform,
                mode=candidate.mode,
                exception=exc,
            )
            if failures >= config.failure_limit:
                break
        else:
            dispatched += 1
            structured_log(
                "info",
                "autopilot_dispatch_accepted",
                lottery_id=candidate.lottery_id,
                platform=candidate.platform,
                mode=candidate.mode,
            )
    return {
        "selected": len(candidates),
        "dispatched": dispatched,
        "failures": failures,
        "probes_requested": probes_requested,
        "deferred": deferred,
    }


async def run_forever(config: AutopilotConfig) -> None:
    if not config.admin_token:
        raise RuntimeError("ADMIN_TOKEN is required")

    headers = {"x-admin-token": config.admin_token}
    async with httpx.AsyncClient(
        base_url=config.core_api_url,
        headers=headers,
        timeout=config.request_timeout_seconds,
        follow_redirects=False,
    ) as client:
        disabled_logged = False
        while True:
            if not config.enabled:
                if not disabled_logged:
                    structured_log("info", "autopilot_disabled")
                    disabled_logged = True
                write_health_heartbeat()
                round_status = "disabled"
                summary = dict(EMPTY_ROUND_SUMMARY)
            else:
                try:
                    summary = await run_round(client, config)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    structured_log(
                        "error",
                        "autopilot_round_failed",
                        exception=exc,
                    )
                    round_status = "error"
                    summary = dict(EMPTY_ROUND_SUMMARY)
                else:
                    structured_log("info", "autopilot_round_completed", **summary)
                    write_health_heartbeat()
                    round_status = "ok"
            await report_autopilot_heartbeat(
                client,
                config,
                round_status=round_status,
                summary=summary,
            )
            await asyncio.sleep(config.poll_interval_seconds)


def main() -> None:
    try:
        asyncio.run(run_forever(AutopilotConfig.from_env()))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
