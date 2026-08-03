"""Finite Redis consumer-group topology and retention health contracts.

Business modules own queue behavior.  This module only declares which groups
are expected to exist on each durable DPMS stream and evaluates bounded Redis
observations.  It deliberately contains no destructive operation: retiring a
group is an explicit operator workflow with separate credentials.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import re
from types import MappingProxyType

from shared.account_calibration_streams import (
    LEGACY_ACCOUNT_CALIBRATION_FANOUT_GROUP_NAME,
    LEGACY_ACCOUNT_CALIBRATION_STREAM_KEY,
    account_calibration_stream_bindings,
)
from shared.adapter_probe_streams import (
    LEGACY_ADAPTER_PROBE_FANOUT_GROUP_NAME,
    LEGACY_ADAPTER_PROBE_STREAM_KEY,
    adapter_probe_stream_bindings,
)
from shared.discovery_scan_streams import DISCOVERY_SCAN_STREAM_BINDINGS
from shared.login_streams import (
    LOGIN_REQUEST_GROUP_NAME,
    LOGIN_REQUEST_STREAM_KEY,
)
from shared.platform_ids import PLATFORM_IDS
from shared.task_streams import (
    MAX_SAFE_TERMINAL_CONSUMER_GROUPS,
    task_stream_bindings,
)
from shared.xiaohongshu_target_pursuit_streams import (
    XIAOHONGSHU_TARGET_PURSUIT_GROUP_NAME,
    XIAOHONGSHU_TARGET_PURSUIT_STREAM_KEY,
)


CONSUMER_GROUP_NAME_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9:._-]{0,127}$"
)
MAX_OBSERVED_CONSUMER_GROUPS = MAX_SAFE_TERMINAL_CONSUMER_GROUPS
MAX_OBSERVED_CONSUMERS_PER_GROUP = 256
NOTIFY_EVENT_STREAM_KEY = "notify_events"
NOTIFY_EVENT_GROUP_NAME = "notify-dispatchers"
CONSUMER_RETIRE_MAX_INVENTORY = 256
CONSUMER_RETIRE_MAX_PER_PASS = 8
CONSUMER_RETIRE_TIMEOUT_SECONDS = 5.0
_SAFE_STALE_CONSUMER_DELETE_LUA = """
local consumers = redis.call('XINFO', 'CONSUMERS', KEYS[1], ARGV[1])
for _, consumer in ipairs(consumers) do
  local name = nil
  local pending = nil
  local idle = nil
  for index = 1, #consumer, 2 do
    if consumer[index] == 'name' then
      name = consumer[index + 1]
    elseif consumer[index] == 'pending' then
      pending = tonumber(consumer[index + 1])
    elseif consumer[index] == 'idle' then
      idle = tonumber(consumer[index + 1])
    end
  end
  if name == ARGV[2] then
    if pending == nil or pending ~= 0 then
      return {'blocked_pending', tostring(pending or 'unknown')}
    end
    if idle == nil or idle < tonumber(ARGV[3]) then
      return {'blocked_active', tostring(idle or 'unknown')}
    end
    local deleted = redis.call(
      'XGROUP', 'DELCONSUMER', KEYS[1], ARGV[1], ARGV[2]
    )
    return {'deleted', tostring(deleted)}
  end
end
return {'absent', '0'}
"""
# Append-only governance catalog.  It is deliberately independent of the
# currently deployed group specs so the last group removed from a stream can
# still be retired under the explicit operator workflow.
GOVERNED_CONSUMER_GROUP_STREAM_KEYS = frozenset(
    {
        "lottery_tasks",
        "adapter_probe_requests",
        "account_calibration_requests",
        NOTIFY_EVENT_STREAM_KEY,
        LOGIN_REQUEST_STREAM_KEY,
        *(
            f"lottery_tasks:{platform}"
            for platform in PLATFORM_IDS
        ),
        *(
            f"lottery_repair_tasks:v1:{platform}"
            for platform in PLATFORM_IDS
        ),
        *(
            f"adapter_probe_requests:{platform}"
            for platform in PLATFORM_IDS
        ),
        *(
            f"account_calibration_requests:{platform}"
            for platform in PLATFORM_IDS
        ),
        *(
            f"discovery_scan_requests:v1:{platform}"
            for platform in PLATFORM_IDS
        ),
        XIAOHONGSHU_TARGET_PURSUIT_STREAM_KEY,
    }
)


@dataclass(frozen=True)
class RedisConsumerGroupSpec:
    stream_key: str
    group_name: str
    subsystem: str
    platform: str | None
    legacy: bool
    role: str = "primary"


_TASK_GROUP_SPECS = tuple(
    RedisConsumerGroupSpec(
        stream_key=binding.stream_key,
        group_name=binding.group_name,
        subsystem="lottery_task",
        platform=binding.platform,
        legacy=bool(binding.legacy),
    )
    for binding in task_stream_bindings(include_legacy=True)
)
_PROBE_GROUP_SPECS = tuple(
    RedisConsumerGroupSpec(
        stream_key=binding.stream_key,
        group_name=binding.group_name,
        subsystem="adapter_probe",
        platform=binding.platform,
        legacy=bool(binding.legacy),
    )
    for binding in adapter_probe_stream_bindings(include_legacy=True)
)
_CALIBRATION_GROUP_SPECS = tuple(
    RedisConsumerGroupSpec(
        stream_key=binding.stream_key,
        group_name=binding.group_name,
        subsystem="account_calibration",
        platform=binding.platform,
        legacy=bool(binding.legacy),
    )
    for binding in account_calibration_stream_bindings(include_legacy=True)
)
_DISCOVERY_SCAN_GROUP_SPECS = tuple(
    RedisConsumerGroupSpec(
        stream_key=binding.stream_key,
        group_name=binding.group_name,
        subsystem="discovery_scan",
        platform=binding.platform,
        legacy=False,
    )
    for binding in DISCOVERY_SCAN_STREAM_BINDINGS
)
_XIAOHONGSHU_TARGET_PURSUIT_GROUP_SPEC = RedisConsumerGroupSpec(
    stream_key=XIAOHONGSHU_TARGET_PURSUIT_STREAM_KEY,
    group_name=XIAOHONGSHU_TARGET_PURSUIT_GROUP_NAME,
    subsystem="xiaohongshu_target_pursuit",
    platform="xiaohongshu",
    legacy=False,
)
_CONTROL_GROUP_SPECS = (
    RedisConsumerGroupSpec(
        stream_key=NOTIFY_EVENT_STREAM_KEY,
        group_name=NOTIFY_EVENT_GROUP_NAME,
        subsystem="notification",
        platform=None,
        legacy=False,
    ),
    RedisConsumerGroupSpec(
        stream_key=LOGIN_REQUEST_STREAM_KEY,
        group_name=LOGIN_REQUEST_GROUP_NAME,
        subsystem="login",
        platform=None,
        legacy=False,
    ),
)
REDIS_CONSUMER_GROUP_SPECS = (
    *_TASK_GROUP_SPECS,
    *_PROBE_GROUP_SPECS,
    RedisConsumerGroupSpec(
        stream_key=LEGACY_ADAPTER_PROBE_STREAM_KEY,
        group_name=LEGACY_ADAPTER_PROBE_FANOUT_GROUP_NAME,
        subsystem="adapter_probe",
        platform=None,
        legacy=True,
        role="legacy_fanout",
    ),
    *_CALIBRATION_GROUP_SPECS,
    RedisConsumerGroupSpec(
        stream_key=LEGACY_ACCOUNT_CALIBRATION_STREAM_KEY,
        group_name=LEGACY_ACCOUNT_CALIBRATION_FANOUT_GROUP_NAME,
        subsystem="account_calibration",
        platform=None,
        legacy=True,
        role="legacy_fanout",
    ),
    *_DISCOVERY_SCAN_GROUP_SPECS,
    _XIAOHONGSHU_TARGET_PURSUIT_GROUP_SPEC,
    *_CONTROL_GROUP_SPECS,
)
_SPECS_BY_STREAM = MappingProxyType(
    {
        stream_key: tuple(
            spec
            for spec in REDIS_CONSUMER_GROUP_SPECS
            if spec.stream_key == stream_key
        )
        for stream_key in {
            spec.stream_key for spec in REDIS_CONSUMER_GROUP_SPECS
        }
    }
)


def consumer_group_specs_for_stream(
    stream_key: str,
) -> tuple[RedisConsumerGroupSpec, ...]:
    return _SPECS_BY_STREAM.get(str(stream_key or "").strip(), ())


def expected_consumer_group_names(stream_key: str) -> frozenset[str]:
    return frozenset(
        spec.group_name for spec in consumer_group_specs_for_stream(stream_key)
    )


def is_governed_consumer_group_stream(stream_key: str) -> bool:
    return (
        str(stream_key or "").strip()
        in GOVERNED_CONSUMER_GROUP_STREAM_KEYS
    )


_RUNTIME_PLATFORM_SUBSYSTEMS = MappingProxyType(
    {
        "core": frozenset({"lottery_task", "discovery_scan"}),
        "worker": frozenset(
            {
                "lottery_task",
                "adapter_probe",
                "account_calibration",
                "xiaohongshu_target_pursuit",
            }
        ),
    }
)
_RUNTIME_SHARED_SUBSYSTEMS = MappingProxyType(
    {
        "core": frozenset({"lottery_task", "notification"}),
        "worker": frozenset(
            {"adapter_probe", "account_calibration", "login"}
        ),
    }
)


class RedisConsumerGroupTopologyError(RuntimeError):
    """A fixed runtime group is absent or its inventory cannot be trusted."""

    def __init__(self, code: str):
        self.code = str(code)
        super().__init__(self.code)


class RedisConsumerRetentionError(RuntimeError):
    """A bounded stale-consumer observation or atomic recheck failed."""

    def __init__(self, code: str):
        self.code = str(code)
        super().__init__(self.code)


def runtime_consumer_group_specs(
    role: str,
    *,
    platforms,
    include_shared: bool,
) -> tuple[RedisConsumerGroupSpec, ...]:
    """Return only the fixed groups owned by one Core/Worker runtime scope."""

    normalized_role = str(role or "").strip().casefold()
    platform_subsystems = _RUNTIME_PLATFORM_SUBSYSTEMS.get(normalized_role)
    shared_subsystems = _RUNTIME_SHARED_SUBSYSTEMS.get(normalized_role)
    if platform_subsystems is None or shared_subsystems is None:
        raise RedisConsumerGroupTopologyError(
            "redis_consumer_group_role_unsupported"
        )
    if isinstance(platforms, str):
        selected_platforms = {
            item.strip().casefold()
            for item in platforms.split(",")
            if item.strip()
        }
    else:
        selected_platforms = {
            str(item or "").strip().casefold()
            for item in (platforms or ())
            if str(item or "").strip()
        }
    unknown = selected_platforms - {
        spec.platform
        for spec in REDIS_CONSUMER_GROUP_SPECS
        if spec.platform is not None
    }
    if unknown:
        raise RedisConsumerGroupTopologyError(
            "redis_consumer_group_platform_scope_unsupported"
        )
    return tuple(
        spec
        for spec in REDIS_CONSUMER_GROUP_SPECS
        if (
            spec.platform in selected_platforms
            and spec.subsystem in platform_subsystems
        )
        or (
            include_shared
            and spec.platform is None
            and spec.subsystem in shared_subsystems
        )
    )


async def verify_redis_consumer_groups(
    redis_client,
    specs,
) -> None:
    """Boundedly prove that every requested fixed stream/group pair exists."""

    expected_by_stream: dict[str, set[str]] = {}
    for spec in tuple(specs or ()):
        if not isinstance(spec, RedisConsumerGroupSpec):
            raise RedisConsumerGroupTopologyError(
                "redis_consumer_group_spec_invalid"
            )
        expected_by_stream.setdefault(spec.stream_key, set()).add(
            spec.group_name
        )

    for stream_key, expected_names in expected_by_stream.items():
        try:
            rows = list(
                await redis_client.xinfo_groups(stream_key) or ()
            )
        except Exception as exc:
            raise RedisConsumerGroupTopologyError(
                "redis_consumer_group_topology_unavailable"
            ) from exc
        if len(rows) > MAX_OBSERVED_CONSUMER_GROUPS:
            raise RedisConsumerGroupTopologyError(
                "redis_consumer_group_inventory_too_large"
            )
        observed_names = [
            normalized_consumer_group_name(_record_field(row, "name"))
            for row in rows
        ]
        if (
            any(name is None for name in observed_names)
            or len(set(observed_names)) != len(observed_names)
        ):
            raise RedisConsumerGroupTopologyError(
                "redis_consumer_group_inventory_invalid"
            )
        if not expected_names.issubset(set(observed_names)):
            raise RedisConsumerGroupTopologyError(
                "redis_consumer_group_expected_missing"
            )


async def verify_redis_consumer_group(
    redis_client,
    *,
    stream_key: str,
    group_name: str,
) -> None:
    """Verify one governed pair without granting runtime creation authority."""

    matches = tuple(
        spec
        for spec in consumer_group_specs_for_stream(stream_key)
        if spec.group_name == str(group_name or "").strip()
    )
    if not matches:
        raise RedisConsumerGroupTopologyError(
            "redis_consumer_group_not_governed"
        )
    await verify_redis_consumer_groups(redis_client, matches)


async def verify_redis_consumer_group_topology(
    redis_client,
    *,
    role: str,
    platforms,
    include_shared: bool,
) -> None:
    """Fail startup unless the bootstrap-owned topology is already present."""

    specs = runtime_consumer_group_specs(
        role,
        platforms=platforms,
        include_shared=include_shared,
    )
    if not specs:
        raise RedisConsumerGroupTopologyError(
            "redis_consumer_group_scope_empty"
        )
    await verify_redis_consumer_groups(redis_client, specs)


async def retire_stale_consumer_metadata(
    redis_client,
    *,
    stream_key: str,
    group_name: str,
    current_consumer_name: str,
    managed_consumer_prefix: str,
    minimum_idle_milliseconds: int,
    timeout_seconds: float = CONSUMER_RETIRE_TIMEOUT_SECONDS,
    max_inventory: int = CONSUMER_RETIRE_MAX_INVENTORY,
    max_retired: int = CONSUMER_RETIRE_MAX_PER_PASS,
) -> dict[str, int]:
    """Boundedly delete only zero-pending, still-idle managed consumers.

    The initial inventory is bounded. Every candidate is then rechecked inside
    the same Lua execution as ``XGROUP DELCONSUMER`` so a consumer that becomes
    active or acquires pending work after observation is never deleted.
    """

    await verify_redis_consumer_group(
        redis_client,
        stream_key=stream_key,
        group_name=group_name,
    )
    current_name = normalized_consumer_group_name(current_consumer_name)
    prefix = str(managed_consumer_prefix or "").strip()
    if current_name is None or not prefix or len(prefix) > 96:
        raise RedisConsumerRetentionError(
            "redis_consumer_retention_scope_invalid"
        )
    try:
        minimum_idle = int(minimum_idle_milliseconds)
        inventory_limit = int(max_inventory)
        retirement_limit = int(max_retired)
        operation_timeout = float(timeout_seconds)
    except (TypeError, ValueError) as exc:
        raise RedisConsumerRetentionError(
            "redis_consumer_retention_bounds_invalid"
        ) from exc
    if (
        isinstance(minimum_idle_milliseconds, bool)
        or minimum_idle <= 0
        or not 1 <= inventory_limit <= 4096
        or not 1 <= retirement_limit <= 32
        or operation_timeout <= 0
    ):
        raise RedisConsumerRetentionError(
            "redis_consumer_retention_bounds_invalid"
        )

    loop = asyncio.get_running_loop()
    deadline = loop.time() + operation_timeout

    def remaining_timeout() -> float:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise RedisConsumerRetentionError(
                "redis_consumer_retention_timeout"
            )
        return remaining

    try:
        inventory_timeout = remaining_timeout()
        rows = list(
            await asyncio.wait_for(
                redis_client.xinfo_consumers(stream_key, group_name),
                timeout=inventory_timeout,
            )
            or ()
        )
    except RedisConsumerRetentionError:
        raise
    except asyncio.TimeoutError as exc:
        raise RedisConsumerRetentionError(
            "redis_consumer_retention_timeout"
        ) from exc
    except Exception as exc:
        raise RedisConsumerRetentionError(
            "redis_consumer_retention_unavailable"
        ) from exc
    if len(rows) > inventory_limit:
        raise RedisConsumerRetentionError(
            "redis_consumer_retention_inventory_too_large"
        )

    observed_names: set[str] = set()
    candidates: list[str] = []
    for row in rows:
        name = normalized_consumer_group_name(_record_field(row, "name"))
        pending = _nonnegative_int(_record_field(row, "pending"))
        idle = _nonnegative_int(_record_field(row, "idle"))
        if (
            name is None
            or name in observed_names
            or pending is None
            or idle is None
        ):
            raise RedisConsumerRetentionError(
                "redis_consumer_retention_inventory_invalid"
            )
        observed_names.add(name)
        if (
            name != current_name
            and name.startswith(prefix)
            and pending == 0
            and idle >= minimum_idle
        ):
            candidates.append(name)

    summary = {
        "inventory": len(rows),
        "candidates": len(candidates),
        "retired": 0,
        "blocked": 0,
        "absent": 0,
    }
    for candidate in candidates[:retirement_limit]:
        try:
            candidate_timeout = remaining_timeout()
            result = list(
                await asyncio.wait_for(
                    redis_client.eval(
                        _SAFE_STALE_CONSUMER_DELETE_LUA,
                        1,
                        stream_key,
                        group_name,
                        candidate,
                        str(minimum_idle),
                    ),
                    timeout=candidate_timeout,
                )
                or ()
            )
        except RedisConsumerRetentionError:
            raise
        except asyncio.TimeoutError as exc:
            raise RedisConsumerRetentionError(
                "redis_consumer_retention_timeout"
            ) from exc
        except Exception as exc:
            raise RedisConsumerRetentionError(
                "redis_consumer_retention_unavailable"
            ) from exc
        if len(result) != 2:
            raise RedisConsumerRetentionError(
                "redis_consumer_retention_result_invalid"
            )
        status = _decoded_text(result[0])
        value = _decoded_text(result[1])
        if status == "deleted" and value == "0":
            summary["retired"] += 1
        elif status in {"blocked_pending", "blocked_active"}:
            summary["blocked"] += 1
        elif status == "absent" and value == "0":
            summary["absent"] += 1
        else:
            raise RedisConsumerRetentionError(
                "redis_consumer_retention_result_invalid"
            )
    return summary


def normalized_consumer_group_name(value) -> str | None:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    name = str(value or "").strip()
    if not CONSUMER_GROUP_NAME_PATTERN.fullmatch(name):
        return None
    return name


def _record_field(record, field: str):
    if isinstance(record, dict):
        return record.get(field)
    try:
        return record[field]
    except (KeyError, TypeError):
        return getattr(record, field, None)


def _decoded_text(value) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return str(value or "").strip()


def _nonnegative_int(value) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def evaluate_consumer_group_governance(
    *,
    stream_key: str,
    groups,
    consumers_by_group: dict[str, list | tuple | None],
    stale_after_milliseconds: int,
    stream_length: int | None,
) -> dict:
    """Summarize stale/unregistered groups without unbounded Redis work."""

    if stale_after_milliseconds <= 0:
        raise ValueError("consumer_group_stale_window_invalid")
    expected_names = expected_consumer_group_names(stream_key)
    group_rows = list(groups or ())
    inventory_too_large = (
        len(group_rows) > MAX_OBSERVED_CONSUMER_GROUPS
    )
    inspected_rows = group_rows[:MAX_OBSERVED_CONSUMER_GROUPS]
    observed_names: set[str] = set()
    unexpected_names: set[str] = set()
    stale_names: set[str] = set()
    xdel_blocked_names: set[str] = set()
    retention_blocked_names: set[str] = set()
    unavailable_consumer_names: set[str] = set()
    oversized_consumer_names: set[str] = set()
    stale_consumer_entries = 0
    invalid_group_names = 0
    details = []

    for group in inspected_rows:
        name = normalized_consumer_group_name(
            _record_field(group, "name")
        )
        if name is None or name in observed_names:
            invalid_group_names += 1
            continue
        observed_names.add(name)
        if name not in expected_names:
            unexpected_names.add(name)

        pending = _nonnegative_int(_record_field(group, "pending"))
        lag = _nonnegative_int(_record_field(group, "lag"))
        reported_consumers = _nonnegative_int(
            _record_field(group, "consumers")
        )
        consumers = consumers_by_group.get(name)
        consumers_available = name in consumers_by_group and consumers is not None
        consumer_rows = list(consumers or ())
        if (
            reported_consumers is not None
            and reported_consumers > MAX_OBSERVED_CONSUMERS_PER_GROUP
        ) or len(consumer_rows) > MAX_OBSERVED_CONSUMERS_PER_GROUP:
            consumers_available = False
            oversized_consumer_names.add(name)
            consumer_rows = []
        active_consumers = 0
        stale_consumers = 0
        if consumers_available:
            for consumer in consumer_rows:
                idle = _nonnegative_int(_record_field(consumer, "idle"))
                if (
                    idle is not None
                    and idle <= stale_after_milliseconds
                ):
                    active_consumers += 1
                elif idle is not None:
                    stale_consumers += 1
            stale_consumer_entries += stale_consumers
        else:
            unavailable_consumer_names.add(name)

        pending_blocks = pending is None or pending > 0
        lag_blocks = lag is None or lag > 0
        # If Redis reports an empty stream and an otherwise clean group, an
        # unavailable lag value cannot block deletion of a non-existent entry.
        if lag is None and stream_length == 0 and pending == 0:
            lag_blocks = False
        xdel_blocked = pending_blocks or lag_blocks
        inactive = consumers_available and active_consumers == 0
        stale = inactive
        if stale:
            stale_names.add(name)
        if xdel_blocked:
            xdel_blocked_names.add(name)
        if xdel_blocked and (
            stale
            or name in unexpected_names
            or not consumers_available
        ):
            retention_blocked_names.add(name)

        details.append(
            {
                "group": name,
                "expected": name in expected_names,
                "pending": pending,
                "lag": lag,
                "consumers_observed": (
                    len(consumer_rows) if consumers_available else None
                ),
                "consumers_reported": reported_consumers,
                "active_consumers": (
                    active_consumers if consumers_available else None
                ),
                "stale_consumers": (
                    stale_consumers if consumers_available else None
                ),
                "stale": stale,
                "xdel_blocked": xdel_blocked,
                "retention_blocked": name in retention_blocked_names,
            }
        )

    missing_expected_names = expected_names - observed_names
    warning_codes = []
    if inventory_too_large:
        warning_codes.append("consumer_group_inventory_too_large")
    if invalid_group_names:
        warning_codes.append("consumer_group_name_invalid")
    if missing_expected_names:
        warning_codes.append("consumer_group_expected_missing")
    if unexpected_names:
        warning_codes.append("consumer_group_unexpected")
    if stale_names:
        warning_codes.append("consumer_group_stale")
    if xdel_blocked_names:
        warning_codes.append("consumer_group_xdel_blocked")
    if retention_blocked_names:
        warning_codes.append("consumer_group_retention_blocked")
    if unavailable_consumer_names:
        warning_codes.append("consumer_group_consumer_metrics_unavailable")
    if oversized_consumer_names:
        warning_codes.append(
            "consumer_group_consumer_inventory_too_large"
        )
    if stale_consumer_entries:
        warning_codes.append("consumer_group_stale_consumer_entries")

    observation_available = not (
        inventory_too_large
        or invalid_group_names
        or unavailable_consumer_names
    )
    retention_alert = bool(
        inventory_too_large
        or invalid_group_names
        or unexpected_names
        or retention_blocked_names
    )
    return {
        "available": observation_available,
        "groups_total": len(group_rows),
        "groups_inspected": len(inspected_rows),
        "expected_groups": sorted(expected_names),
        "missing_expected_groups": sorted(missing_expected_names),
        "unexpected_groups": sorted(unexpected_names),
        "stale_groups": sorted(stale_names),
        "xdel_blocked_groups": sorted(xdel_blocked_names),
        "retention_blocked_groups": sorted(retention_blocked_names),
        "retention_alert": retention_alert,
        "consumer_inventory_alert": bool(
            stale_consumer_entries or oversized_consumer_names
        ),
        "stale_consumer_entries": stale_consumer_entries,
        "warning_codes": warning_codes,
        "groups": details,
    }


__all__ = (
    "CONSUMER_GROUP_NAME_PATTERN",
    "CONSUMER_RETIRE_MAX_INVENTORY",
    "CONSUMER_RETIRE_MAX_PER_PASS",
    "CONSUMER_RETIRE_TIMEOUT_SECONDS",
    "GOVERNED_CONSUMER_GROUP_STREAM_KEYS",
    "LOGIN_REQUEST_GROUP_NAME",
    "LOGIN_REQUEST_STREAM_KEY",
    "MAX_OBSERVED_CONSUMER_GROUPS",
    "MAX_OBSERVED_CONSUMERS_PER_GROUP",
    "NOTIFY_EVENT_GROUP_NAME",
    "NOTIFY_EVENT_STREAM_KEY",
    "REDIS_CONSUMER_GROUP_SPECS",
    "RedisConsumerGroupTopologyError",
    "RedisConsumerRetentionError",
    "RedisConsumerGroupSpec",
    "consumer_group_specs_for_stream",
    "evaluate_consumer_group_governance",
    "expected_consumer_group_names",
    "is_governed_consumer_group_stream",
    "normalized_consumer_group_name",
    "retire_stale_consumer_metadata",
    "runtime_consumer_group_specs",
    "verify_redis_consumer_group",
    "verify_redis_consumer_group_topology",
    "verify_redis_consumer_groups",
)
