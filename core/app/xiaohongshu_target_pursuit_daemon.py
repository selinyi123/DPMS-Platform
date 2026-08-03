"""Independent read-only scheduler for Xiaohongshu target-source scans.

The daemon deliberately talks to Core through the authenticated public HTTP
API.  It may list active target sources and request a read-only scan; it never
imports database code, changes candidate review decisions, or dispatches
lottery participation tasks.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

import httpx

from app.utils.log import structured_log


XIAOHONGSHU_PLATFORM = "xiaohongshu"
SCANNABLE_SOURCE_TYPES = frozenset({"keyword", "author_profile"})
SOURCE_LIST_PATH = "/api/xiaohongshu-targets/sources"
SOURCE_SCAN_PATH = "/api/xiaohongshu-targets/scan"
HEALTH_PATH = Path("/tmp/xiaohongshu-target-pursuit-health")


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
        raise ValueError(
            "DPMS_XIAOHONGSHU_TARGET_PURSUIT_CORE_API_URL must be a plain "
            "HTTP(S) origin"
        )
    return url


@dataclass(frozen=True)
class PursuitDaemonConfig:
    core_api_url: str
    admin_token: str
    enabled: bool
    platform_allowlist: frozenset[str]
    cadence_seconds: float = 1800.0
    poll_interval_seconds: float = 60.0
    source_limit: int = 100
    scan_limit: int = 10
    failure_limit: int = 3
    max_candidates: int = 20
    request_timeout_seconds: float = 120.0

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> "PursuitDaemonConfig":
        env = os.environ if environ is None else environ
        prefix = "DPMS_XIAOHONGSHU_TARGET_PURSUIT_"
        return cls(
            core_api_url=_core_api_url(env.get(prefix + "CORE_API_URL")),
            admin_token=str(env.get("ADMIN_TOKEN") or ""),
            enabled=_parse_bool(env.get(prefix + "ENABLED")),
            platform_allowlist=_platform_allowlist(
                env.get(prefix + "PLATFORMS")
            ),
            cadence_seconds=_bounded_float(
                env.get(prefix + "CADENCE_SECONDS"),
                default=1800.0,
                minimum=30.0,
                maximum=86400.0,
            ),
            poll_interval_seconds=_bounded_float(
                env.get(prefix + "POLL_SECONDS"),
                default=60.0,
                minimum=1.0,
                maximum=3600.0,
            ),
            source_limit=_bounded_int(
                env.get(prefix + "SOURCE_LIMIT"),
                default=100,
                minimum=1,
                maximum=200,
            ),
            scan_limit=_bounded_int(
                env.get(prefix + "SCAN_LIMIT"),
                default=10,
                minimum=1,
                maximum=100,
            ),
            failure_limit=_bounded_int(
                env.get(prefix + "FAILURE_LIMIT"),
                default=3,
                minimum=1,
                maximum=100,
            ),
            max_candidates=_bounded_int(
                env.get(prefix + "MAX_CANDIDATES"),
                default=20,
                minimum=1,
                maximum=50,
            ),
            request_timeout_seconds=_bounded_float(
                env.get(prefix + "REQUEST_TIMEOUT_SECONDS"),
                default=120.0,
                minimum=1.0,
                maximum=180.0,
            ),
        )


@dataclass
class PursuitDaemonState:
    """Process-local retry state; durable cadence remains owned by Core."""

    failure_counts: dict[int, int] = field(default_factory=dict)


def daemon_enabled(config: PursuitDaemonConfig) -> bool:
    return bool(
        config.enabled
        and XIAOHONGSHU_PLATFORM in config.platform_allowlist
    )


def _parse_timestamp(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _source_id(source: Mapping[str, Any]) -> int | None:
    try:
        source_id = int(source.get("id"))
    except (TypeError, ValueError):
        return None
    return source_id if source_id > 0 else None


def source_due(
    source: Mapping[str, Any],
    config: PursuitDaemonConfig,
    state: PursuitDaemonState,
    *,
    now: datetime,
) -> tuple[bool, str]:
    source_id = _source_id(source)
    if source_id is None or not bool(source.get("active")):
        return False, "invalid_or_inactive"
    source_type = str(source.get("source_type") or "").strip().casefold()
    if source_type not in SCANNABLE_SOURCE_TYPES:
        return False, "offline_or_unsupported"
    if state.failure_counts.get(source_id, 0) >= config.failure_limit:
        return False, "failure_limit"
    if str(source.get("status") or "").strip().casefold() == "scanning":
        return False, "scan_in_progress"

    raw_last_scan_at = source.get("last_scan_at")
    last_scan_at = _parse_timestamp(raw_last_scan_at)
    if raw_last_scan_at not in (None, "") and last_scan_at is None:
        # An unparseable durable timestamp must never be interpreted as an
        # invitation to rescan continuously.
        return False, "invalid_last_scan_at"
    if last_scan_at is not None:
        due_at = last_scan_at + timedelta(seconds=config.cadence_seconds)
        if now < due_at:
            return False, "cadence"
    return True, "due"


def _source_order(source: Mapping[str, Any]) -> tuple[int, datetime, int]:
    last_scan_at = _parse_timestamp(source.get("last_scan_at"))
    return (
        0 if last_scan_at is None else 1,
        last_scan_at or datetime.min.replace(tzinfo=timezone.utc),
        _source_id(source) or 0,
    )


async def _fetch_active_sources(
    client: httpx.AsyncClient,
    config: PursuitDaemonConfig,
) -> list[dict[str, Any]]:
    response = await client.get(
        SOURCE_LIST_PATH,
        params={"active": "true", "limit": config.source_limit, "offset": 0},
    )
    response.raise_for_status()
    payload = response.json()
    items = payload.get("items") if isinstance(payload, Mapping) else None
    if not isinstance(items, list):
        raise RuntimeError("xiaohongshu_target_source_list_invalid")
    return [dict(item) for item in items if isinstance(item, Mapping)]


async def _request_source_scan(
    client: httpx.AsyncClient,
    config: PursuitDaemonConfig,
    source_id: int,
) -> None:
    response = await client.post(
        SOURCE_SCAN_PATH,
        json={
            "source_id": source_id,
            "max_candidates": config.max_candidates,
        },
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, Mapping) or payload.get("status") != "scanned":
        raise RuntimeError("xiaohongshu_target_scan_response_invalid")


async def run_round(
    client: httpx.AsyncClient,
    config: PursuitDaemonConfig,
    state: PursuitDaemonState,
    *,
    now: datetime | None = None,
) -> dict[str, int]:
    """Run one bounded source scan round without changing review decisions."""

    if not daemon_enabled(config):
        return {
            "fetched": 0,
            "selected": 0,
            "scanned": 0,
            "failures": 0,
            "deferred": 0,
        }

    observed_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    sources = await _fetch_active_sources(client, config)
    selected: list[dict[str, Any]] = []
    deferred = 0
    for source in sorted(sources, key=_source_order):
        due, _reason = source_due(
            source,
            config,
            state,
            now=observed_at,
        )
        if not due:
            deferred += 1
            continue
        selected.append(source)
        if len(selected) >= config.scan_limit:
            break

    scanned = 0
    failures = 0
    for source in selected:
        source_id = _source_id(source)
        if source_id is None:
            continue
        try:
            await _request_source_scan(client, config, source_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failures += 1
            state.failure_counts[source_id] = (
                state.failure_counts.get(source_id, 0) + 1
            )
            structured_log(
                "warning",
                "xiaohongshu_target_pursuit_daemon_scan_failed",
                source_id=source_id,
                source_type=source.get("source_type"),
                failures=state.failure_counts[source_id],
                exception=exc,
            )
        else:
            scanned += 1
            state.failure_counts.pop(source_id, None)
            structured_log(
                "info",
                "xiaohongshu_target_pursuit_daemon_scan_completed",
                source_id=source_id,
                source_type=source.get("source_type"),
            )

    return {
        "fetched": len(sources),
        "selected": len(selected),
        "scanned": scanned,
        "failures": failures,
        "deferred": deferred,
    }


def write_health_heartbeat(path: Path = HEALTH_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


async def run_forever(config: PursuitDaemonConfig) -> None:
    active = daemon_enabled(config)
    if active and not config.admin_token:
        raise RuntimeError("ADMIN_TOKEN is required when target pursuit is enabled")

    state = PursuitDaemonState()
    headers = {"x-admin-token": config.admin_token} if config.admin_token else {}
    async with httpx.AsyncClient(
        base_url=config.core_api_url,
        headers=headers,
        timeout=config.request_timeout_seconds,
        follow_redirects=False,
    ) as client:
        disabled_logged = False
        while True:
            if not active:
                if not disabled_logged:
                    structured_log(
                        "info",
                        "xiaohongshu_target_pursuit_daemon_disabled",
                    )
                    disabled_logged = True
                write_health_heartbeat()
            else:
                try:
                    summary = await run_round(client, config, state)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    structured_log(
                        "error",
                        "xiaohongshu_target_pursuit_daemon_round_failed",
                        exception=exc,
                    )
                else:
                    structured_log(
                        "info",
                        "xiaohongshu_target_pursuit_daemon_round_completed",
                        **summary,
                    )
                    write_health_heartbeat()
            await asyncio.sleep(config.poll_interval_seconds)


def main() -> None:
    try:
        asyncio.run(run_forever(PursuitDaemonConfig.from_env()))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
