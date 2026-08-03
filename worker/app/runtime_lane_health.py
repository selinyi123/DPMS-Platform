"""Process-local liveness evidence for non-task Worker lanes.

Task streams already publish an exact per-stream health contract from
``task_runner``.  Probe, calibration and the Worker-owned task Outbox have the
same failure mode (their loops intentionally retry forever), so process/task
liveness alone cannot prove that they are making progress.  This module keeps
only bounded, non-secret timestamps and error types for those lanes.
"""

from __future__ import annotations

from dataclasses import dataclass
import time

from shared.platform_ids import PLATFORM_IDS


RUNTIME_LANE_HEALTH_CONTRACT_VERSION = 1
RUNTIME_LANE_HEALTH_RECENT_SECONDS = 45
RUNTIME_LANE_PROGRESS_INTERVAL_SECONDS = 10
RUNTIME_LANE_HEALTH_MAX_REPORTED_AGE_SECONDS = 86_400
RUNTIME_LANE_HEALTH_MAX_CONSECUTIVE_FAILURES = 1_000_000
_PLATFORM_LANE_KINDS = frozenset({"probe", "calibration"})
_SHARED_LANE_KINDS = frozenset(
    {
        "outbox",
        "legacy_probe_fanout",
        "legacy_calibration_fanout",
    }
)


@dataclass
class _RuntimeLaneHealthState:
    key: str
    kind: str
    platform: str | None
    last_success_monotonic: float | None = None
    last_progress_monotonic: float | None = None
    saturated: bool = False
    last_error_monotonic: float | None = None
    last_error_type: str | None = None
    consecutive_failures: int = 0


_RUNTIME_LANE_HEALTH: dict[str, _RuntimeLaneHealthState] = {}


def _runtime_lane_identity(
    kind: str,
    platform: str | None = None,
) -> tuple[str, str, str | None]:
    normalized_kind = str(kind or "").strip().casefold()
    if normalized_kind in _PLATFORM_LANE_KINDS:
        normalized_platform = str(platform or "").strip().casefold()
        if normalized_platform not in PLATFORM_IDS:
            raise ValueError("runtime_lane_platform_invalid")
        return (
            f"{normalized_kind}:{normalized_platform}",
            normalized_kind,
            normalized_platform,
        )
    if normalized_kind in _SHARED_LANE_KINDS:
        if platform not in (None, ""):
            raise ValueError("runtime_lane_shared_platform_invalid")
        return f"{normalized_kind}:shared", normalized_kind, None
    raise ValueError("runtime_lane_kind_invalid")


def _runtime_lane_state(
    kind: str,
    platform: str | None = None,
) -> _RuntimeLaneHealthState:
    key, normalized_kind, normalized_platform = _runtime_lane_identity(
        kind,
        platform,
    )
    state = _RUNTIME_LANE_HEALTH.get(key)
    if state is None:
        state = _RuntimeLaneHealthState(
            key=key,
            kind=normalized_kind,
            platform=normalized_platform,
        )
        _RUNTIME_LANE_HEALTH[key] = state
    return state


def record_runtime_lane_success(
    kind: str,
    platform: str | None = None,
) -> None:
    """Record one completed poll/read cycle without retaining its payload."""

    state = _runtime_lane_state(kind, platform)
    state.last_success_monotonic = time.monotonic()
    state.consecutive_failures = 0


def record_runtime_lane_failure(
    kind: str,
    platform: str | None,
    exc: BaseException,
) -> None:
    """Record bounded failure metadata for one exact owned lane."""

    state = _runtime_lane_state(kind, platform)
    state.last_error_monotonic = time.monotonic()
    state.last_error_type = str(
        type(exc).__name__ or "Exception"
    )[:64]
    state.consecutive_failures = min(
        state.consecutive_failures + 1,
        RUNTIME_LANE_HEALTH_MAX_CONSECUTIVE_FAILURES,
    )


def record_runtime_lane_progress(
    kind: str,
    platform: str,
    *,
    saturated: bool,
) -> None:
    """Record live capacity wait without pretending that Redis was read."""

    if not isinstance(saturated, bool):
        raise ValueError("runtime_lane_saturated_invalid")
    state = _runtime_lane_state(kind, platform)
    state.last_progress_monotonic = time.monotonic()
    state.saturated = saturated


def owned_runtime_lane_keys(
    runtime_plan,
    *,
    include_legacy_fanout: bool = False,
) -> tuple[str, ...]:
    """Return the exact non-task lanes required by one Worker role."""

    keys: list[str] = []
    if runtime_plan.owns_platform_lanes:
        for platform in runtime_plan.platforms:
            for kind in ("probe", "calibration"):
                key, _kind, _platform = _runtime_lane_identity(
                    kind,
                    platform,
                )
                keys.append(key)
    if runtime_plan.owns_control_loops:
        key, _kind, _platform = _runtime_lane_identity("outbox")
        keys.append(key)
    if include_legacy_fanout and runtime_plan.owns_legacy_fanout:
        for kind in (
            "legacy_probe_fanout",
            "legacy_calibration_fanout",
        ):
            key, _kind, _platform = _runtime_lane_identity(kind)
            keys.append(key)
    return tuple(keys)


def _reported_age(
    now: float,
    observed_at: float | None,
) -> int | None:
    if observed_at is None:
        return None
    return min(
        max(0, int(now - observed_at)),
        RUNTIME_LANE_HEALTH_MAX_REPORTED_AGE_SECONDS,
    )


def runtime_lane_health_snapshot(
    runtime_plan,
    *,
    include_legacy_fanout: bool = False,
) -> dict:
    """Return a bounded snapshot for only the lanes owned by ``runtime_plan``."""

    now = time.monotonic()
    lanes = []
    for key in owned_runtime_lane_keys(
        runtime_plan,
        include_legacy_fanout=include_legacy_fanout,
    ):
        state = _RUNTIME_LANE_HEALTH.get(key)
        if state is None:
            kind, scope = key.split(":", 1)
            state = _runtime_lane_state(
                kind,
                None if scope == "shared" else scope,
            )
        success_age = _reported_age(
            now,
            state.last_success_monotonic,
        )
        error_age = _reported_age(
            now,
            state.last_error_monotonic,
        )
        progress_age = _reported_age(
            now,
            state.last_progress_monotonic,
        )
        saturated_progress = bool(
            state.last_success_monotonic is not None
            and state.saturated
            and progress_age is not None
            and progress_age <= RUNTIME_LANE_HEALTH_RECENT_SECONDS
        )
        healthy = bool(
            state.consecutive_failures == 0
            and (
                (
                    success_age is not None
                    and success_age
                    <= RUNTIME_LANE_HEALTH_RECENT_SECONDS
                )
                or saturated_progress
            )
        )
        lanes.append(
            {
                "lane": state.key,
                "kind": state.kind,
                "platform": state.platform,
                "status": (
                    "healthy"
                    if healthy
                    else (
                        "degraded"
                        if state.last_success_monotonic is not None
                        or state.last_error_monotonic is not None
                        else "starting"
                    )
                ),
                "last_success_age_seconds": success_age,
                "last_progress_age_seconds": progress_age,
                "saturated": state.saturated,
                "last_error_type": state.last_error_type,
                "last_error_age_seconds": error_age,
                "consecutive_failures": state.consecutive_failures,
            }
        )
    return {
        "contract_version": RUNTIME_LANE_HEALTH_CONTRACT_VERSION,
        "lanes": lanes,
    }


def runtime_lanes_ready(
    runtime_plan,
    *,
    include_legacy_fanout: bool = False,
) -> bool:
    snapshot = runtime_lane_health_snapshot(
        runtime_plan,
        include_legacy_fanout=include_legacy_fanout,
    )
    lanes = snapshot["lanes"]
    expected = owned_runtime_lane_keys(
        runtime_plan,
        include_legacy_fanout=include_legacy_fanout,
    )
    return bool(
        expected
        and len(lanes) == len(expected)
        and all(lane["status"] == "healthy" for lane in lanes)
    )


def _reset_runtime_lane_health_for_tests() -> None:
    """Reset process-local observations; production never calls this helper."""

    _RUNTIME_LANE_HEALTH.clear()


__all__ = (
    "RUNTIME_LANE_HEALTH_CONTRACT_VERSION",
    "RUNTIME_LANE_HEALTH_RECENT_SECONDS",
    "RUNTIME_LANE_PROGRESS_INTERVAL_SECONDS",
    "owned_runtime_lane_keys",
    "record_runtime_lane_failure",
    "record_runtime_lane_progress",
    "record_runtime_lane_success",
    "runtime_lane_health_snapshot",
    "runtime_lanes_ready",
)
