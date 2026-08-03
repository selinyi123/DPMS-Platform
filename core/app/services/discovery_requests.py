"""Durable manual discovery dispatch between shared API and platform runners."""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid

from app.db import redis
from app.services.discovery import run_discovery
from app.utils.log import structured_log
from shared.discovery_scan_streams import (
    DISCOVERY_SCAN_PROTOCOL_VERSION,
    DISCOVERY_SCAN_STREAM_BINDINGS,
    DiscoveryScanStreamBinding,
    discovery_scan_binding_for_platform,
    discovery_scan_result_key,
)
from shared.platform_ids import PLATFORM_IDS
from shared.redis_consumer_groups import (
    normalized_consumer_group_name,
    retire_stale_consumer_metadata,
    verify_redis_consumer_group,
)
from shared.task_streams import SAFE_TERMINAL_STREAM_ACK_DELETE_LUA


DISCOVERY_SCAN_RESULT_TTL_SECONDS = 600
DISCOVERY_SCAN_WAIT_TIMEOUT_SECONDS = 135
DISCOVERY_SCAN_RESULT_POLL_SECONDS = 0.2
DISCOVERY_SCAN_RESULT_MAX_BYTES = 64 * 1024
DISCOVERY_SCAN_REQUEST_MAX_AGE_MILLISECONDS = int(
    min(
        DISCOVERY_SCAN_WAIT_TIMEOUT_SECONDS,
        DISCOVERY_SCAN_RESULT_TTL_SECONDS,
    )
    * 1000
)
DISCOVERY_SCAN_REQUEST_MAX_FUTURE_SKEW_MILLISECONDS = 30_000
# Reclaim only after every request that was valid when first delivered is
# guaranteed to be stale. This prevents a rolling replacement from replaying
# an active discovery scan merely because its old process still owns the PEL.
DISCOVERY_SCAN_RECLAIM_IDLE_MILLISECONDS = (
    DISCOVERY_SCAN_REQUEST_MAX_AGE_MILLISECONDS
    + DISCOVERY_SCAN_REQUEST_MAX_FUTURE_SKEW_MILLISECONDS
)
DISCOVERY_SCAN_RECLAIM_INTERVAL_SECONDS = 15
DISCOVERY_SCAN_RECLAIM_COUNT = 20


_manual_discovery_scan_task: asyncio.Task[dict] | None = None
_CORE_RUNNER_INSTANCE_ID_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
)


def _canonical_request_id(value) -> str:
    normalized = str(value or "").strip().casefold()
    try:
        parsed = uuid.UUID(normalized)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("discovery_scan_request_id_invalid") from exc
    if str(parsed) != normalized:
        raise ValueError("discovery_scan_request_id_invalid")
    return normalized


def _new_discovery_scan_consumer_name(
    platform: str,
    *,
    configured_instance_id: str | None = None,
) -> str:
    """Return a process-instance consumer; stale ownership stays observable."""

    binding = discovery_scan_binding_for_platform(platform)
    explicit = str(
        configured_instance_id
        if configured_instance_id is not None
        else os.getenv("DPMS_CORE_RUNNER_INSTANCE_ID", "")
    ).strip()
    if explicit and not _CORE_RUNNER_INSTANCE_ID_PATTERN.fullmatch(explicit):
        raise ValueError("core_runner_instance_id_invalid")
    instance_id = explicit or uuid.uuid4().hex
    consumer_name = (
        f"core-platform-runner:{binding.platform}:"
        f"{instance_id}"
    )
    if normalized_consumer_group_name(consumer_name) is None:
        raise ValueError("core_runner_instance_id_invalid")
    return consumer_name


async def dispatch_manual_discovery_scan() -> dict:
    """Coalesce concurrent operators onto one shielded durable fan-out."""

    global _manual_discovery_scan_task
    task = _manual_discovery_scan_task
    if task is None or task.done():
        task = asyncio.create_task(
            _dispatch_manual_discovery_scan_once(),
            name="manual-discovery-scan-fanout",
        )
        _manual_discovery_scan_task = task
    return await asyncio.shield(task)


async def _dispatch_manual_discovery_scan_once() -> dict:
    request_id = str(uuid.uuid4())
    requested_at_ms = int(time.time() * 1000)
    dispatch_results = await asyncio.gather(
        *(
            redis.xadd(
                binding.stream_key,
                {
                    "protocol_version": str(
                        DISCOVERY_SCAN_PROTOCOL_VERSION
                    ),
                    "request_id": request_id,
                    "platform": binding.platform,
                    "requested_at_ms": str(requested_at_ms),
                },
            )
            for binding in DISCOVERY_SCAN_STREAM_BINDINGS
        ),
        return_exceptions=True,
    )
    dispatched_platforms = []
    lane_errors: dict[str, str] = {}
    for binding, dispatch_result in zip(
        DISCOVERY_SCAN_STREAM_BINDINGS,
        dispatch_results,
    ):
        if isinstance(dispatch_result, BaseException):
            lane_errors[binding.platform] = (
                "discovery_scan_dispatch_failed"
            )
            structured_log(
                "error",
                "discovery_scan_request_dispatch_failed",
                platform=binding.platform,
                stream=binding.stream_key,
                exception=dispatch_result,
            )
            continue
        dispatched_platforms.append(binding.platform)

    wait_results = await asyncio.gather(
        *(
            _wait_for_discovery_result(request_id, platform)
            for platform in dispatched_platforms
        ),
        return_exceptions=True,
    )
    results: dict[str, dict | None] = {}
    for platform, wait_result in zip(
        dispatched_platforms,
        wait_results,
    ):
        if isinstance(wait_result, BaseException):
            lane_errors[platform] = (
                "discovery_scan_result_unavailable"
            )
            structured_log(
                "error",
                "discovery_scan_result_wait_failed",
                platform=platform,
                exception=wait_result,
            )
            continue
        results[platform] = wait_result
    return aggregate_discovery_results(
        results,
        lane_errors=lane_errors,
    )


async def _wait_for_discovery_result(
    request_id: str,
    platform: str,
) -> dict | None:
    key = discovery_scan_result_key(request_id, platform)
    deadline = (
        asyncio.get_running_loop().time()
        + DISCOVERY_SCAN_WAIT_TIMEOUT_SECONDS
    )
    while asyncio.get_running_loop().time() < deadline:
        raw = await redis.get(key)
        if raw is not None:
            await redis.delete(key)
            try:
                decoded = json.loads(raw)
            except (TypeError, ValueError, json.JSONDecodeError):
                return None
            return decoded if isinstance(decoded, dict) else None
        await asyncio.sleep(DISCOVERY_SCAN_RESULT_POLL_SECONDS)
    return None


def aggregate_discovery_results(
    results: dict[str, dict | None],
    *,
    lane_errors: dict[str, str] | None = None,
) -> dict:
    errors = dict(lane_errors or {})
    totals = {
        "sources": 0,
        "scanned": 0,
        "found": 0,
        "inserted": 0,
        "expanded_sources": 0,
        "expired": 0,
        "failed": 0,
        "by_platform": {},
    }
    for platform in PLATFORM_IDS:
        result = results.get(platform)
        dispatch_error = errors.get(platform)
        if dispatch_error:
            totals["failed"] += 1
            totals["by_platform"][platform] = {
                "scanned": 0,
                "found": 0,
                "inserted": 0,
                "expanded_sources": 0,
                "failed": 1,
                "dispatch_error": dispatch_error,
            }
            continue
        if not isinstance(result, dict):
            totals["failed"] += 1
            totals["by_platform"][platform] = {
                "scanned": 0,
                "found": 0,
                "inserted": 0,
                "expanded_sources": 0,
                "failed": 1,
                "dispatch_error": "discovery_scan_result_timeout",
            }
            continue
        for key in (
            "sources",
            "scanned",
            "found",
            "inserted",
            "expanded_sources",
            "expired",
            "failed",
        ):
            totals[key] += max(0, int(result.get(key) or 0))
        platform_stats = (result.get("by_platform") or {}).get(platform)
        totals["by_platform"][platform] = (
            dict(platform_stats)
            if isinstance(platform_stats, dict)
            else {
                "scanned": 0,
                "found": 0,
                "inserted": 0,
                "expanded_sources": 0,
                "failed": 1,
                "dispatch_error": "discovery_scan_result_invalid",
            }
        )
    return totals


async def _retire_discovery_scan_request(
    binding: DiscoveryScanStreamBinding,
    message_id: str,
) -> None:
    result = list(
        await redis.eval(
            SAFE_TERMINAL_STREAM_ACK_DELETE_LUA,
            1,
            binding.stream_key,
            binding.group_name,
            str(message_id),
        )
        or ()
    )
    if len(result) != 2:
        raise RuntimeError("discovery_scan_terminal_ack_result_invalid")
    try:
        acknowledged, deleted = (int(result[0]), int(result[1]))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            "discovery_scan_terminal_ack_result_invalid"
        ) from exc
    if (
        acknowledged not in {0, 1}
        or deleted not in {0, 1}
        or (acknowledged == 0 and deleted == 0)
    ):
        raise RuntimeError("discovery_scan_terminal_ack_result_invalid")


async def _retire_stale_discovery_scan_consumers(
    binding: DiscoveryScanStreamBinding,
    consumer_name: str,
) -> dict[str, int]:
    return await retire_stale_consumer_metadata(
        redis,
        stream_key=binding.stream_key,
        group_name=binding.group_name,
        current_consumer_name=consumer_name,
        managed_consumer_prefix=(
            f"core-platform-runner:{binding.platform}:"
        ),
        minimum_idle_milliseconds=(
            DISCOVERY_SCAN_RECLAIM_IDLE_MILLISECONDS
        ),
    )


async def _reclaim_stale_discovery_scan_requests(
    binding: DiscoveryScanStreamBinding,
    consumer_name: str,
) -> int:
    """Claim and retire only requests guaranteed stale by protocol age."""

    pending = await redis.xpending_range(
        binding.stream_key,
        binding.group_name,
        min="-",
        max="+",
        count=DISCOVERY_SCAN_RECLAIM_COUNT,
        idle=DISCOVERY_SCAN_RECLAIM_IDLE_MILLISECONDS,
    )
    reclaimed = 0
    for entry in pending or ():
        idle_ms = int(entry.get("time_since_delivered") or 0)
        message_id = str(entry.get("message_id") or "").strip()
        if (
            not message_id
            or idle_ms < DISCOVERY_SCAN_RECLAIM_IDLE_MILLISECONDS
        ):
            continue
        claimed = await redis.xclaim(
            binding.stream_key,
            binding.group_name,
            consumer_name,
            min_idle_time=DISCOVERY_SCAN_RECLAIM_IDLE_MILLISECONDS,
            message_ids=[message_id],
        )
        if not claimed:
            continue
        for claimed_id, fields in claimed:
            await _handle_discovery_scan_request(
                binding,
                str(claimed_id),
                dict(fields or {}),
            )
            reclaimed += 1
    return reclaimed


def _request_timestamp_rejection(
    value,
    *,
    now_ms: int | None = None,
) -> str | None:
    if isinstance(value, bool):
        return "invalid"
    raw = str(value or "").strip()
    try:
        requested_at_ms = int(raw)
    except (TypeError, ValueError):
        return "invalid"
    if str(requested_at_ms) != raw or requested_at_ms <= 0:
        return "invalid"
    observed_at_ms = (
        int(time.time() * 1000)
        if now_ms is None
        else int(now_ms)
    )
    if (
        requested_at_ms - observed_at_ms
        > DISCOVERY_SCAN_REQUEST_MAX_FUTURE_SKEW_MILLISECONDS
    ):
        return "future"
    if (
        observed_at_ms - requested_at_ms
        >= DISCOVERY_SCAN_REQUEST_MAX_AGE_MILLISECONDS
    ):
        return "stale"
    return None


async def discovery_scan_request_loop(platform: str) -> None:
    """Consume one exact platform request stream, including prior pending work."""

    binding = discovery_scan_binding_for_platform(platform)
    await verify_redis_consumer_group(
        redis,
        stream_key=binding.stream_key,
        group_name=binding.group_name,
    )
    consumer_name = _new_discovery_scan_consumer_name(
        binding.platform
    )
    read_id = "0"
    last_reclaim_at = float("-inf")
    while True:
        loop = asyncio.get_running_loop()
        if (
            read_id == ">"
            and loop.time() - last_reclaim_at
            >= DISCOVERY_SCAN_RECLAIM_INTERVAL_SECONDS
        ):
            try:
                await _reclaim_stale_discovery_scan_requests(
                    binding,
                    consumer_name,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                structured_log(
                    "error",
                    "discovery_scan_pending_reclaim_failed",
                    platform=binding.platform,
                    exception=exc,
                )
            try:
                await _retire_stale_discovery_scan_consumers(
                    binding,
                    consumer_name,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                structured_log(
                    "error",
                    "discovery_scan_consumer_retention_failed",
                    platform=binding.platform,
                    exception=exc,
                )
            last_reclaim_at = loop.time()
        messages = await redis.xreadgroup(
            binding.group_name,
            consumer_name,
            {binding.stream_key: read_id},
            count=1,
            block=(None if read_id == "0" else 5000),
        )
        if not messages:
            read_id = ">"
            continue
        delivered_entries = 0
        for stream_name, entries in messages:
            if str(stream_name) != binding.stream_key:
                raise RuntimeError(
                    "discovery_scan_stream_response_mismatch"
                )
            for message_id, fields in entries:
                delivered_entries += 1
                await _handle_discovery_scan_request(
                    binding,
                    str(message_id),
                    dict(fields or {}),
                )
        # redis-py may preserve the stream envelope when the pending-history
        # read has no entries, returning ``[(stream, [])]``. That value is
        # truthy, so checking only ``if not messages`` leaves a new consumer
        # spinning forever on ID ``0`` and it never advances to ``>`` for new
        # requests.
        if delivered_entries == 0:
            read_id = ">"


async def _handle_discovery_scan_request(
    binding: DiscoveryScanStreamBinding,
    message_id: str,
    fields: dict,
) -> None:
    if (
        str(fields.get("protocol_version") or "")
        != str(DISCOVERY_SCAN_PROTOCOL_VERSION)
        or str(fields.get("platform") or "").strip().casefold()
        != binding.platform
    ):
        await _retire_discovery_scan_request(binding, message_id)
        structured_log(
            "error",
            "discovery_scan_request_rejected",
            platform=binding.platform,
            message_id=message_id,
        )
        return
    timestamp_rejection = _request_timestamp_rejection(
        fields.get("requested_at_ms")
    )
    if timestamp_rejection is not None:
        await _retire_discovery_scan_request(binding, message_id)
        structured_log(
            "warning",
            "discovery_scan_request_timestamp_rejected",
            platform=binding.platform,
            message_id=message_id,
            reason=timestamp_rejection,
        )
        return
    try:
        request_id = _canonical_request_id(fields.get("request_id"))
    except ValueError:
        await _retire_discovery_scan_request(binding, message_id)
        return
    result_key = discovery_scan_result_key(
        request_id,
        binding.platform,
    )
    if await redis.get(result_key) is not None:
        await _retire_discovery_scan_request(binding, message_id)
        return

    result = await run_discovery(platforms=(binding.platform,))
    encoded = json.dumps(
        result,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    if len(encoded.encode("utf-8")) > DISCOVERY_SCAN_RESULT_MAX_BYTES:
        raise RuntimeError("discovery_scan_result_exceeds_limit")
    await redis.set(
        result_key,
        encoded,
        ex=DISCOVERY_SCAN_RESULT_TTL_SECONDS,
        nx=True,
    )
    await _retire_discovery_scan_request(binding, message_id)


__all__ = (
    "aggregate_discovery_results",
    "discovery_scan_request_loop",
    "dispatch_manual_discovery_scan",
)
