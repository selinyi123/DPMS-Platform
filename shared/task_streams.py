"""Authoritative Redis task-stream topology for Core and Worker.

Platform business modules remain isolated.  This module only owns the shared
transport contract: one standard and one versioned repair stream/group pair
per platform plus the historical single-stream drain binding.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType

from shared.platform_ids import PLATFORM_IDS


# Compatibility alias. New shared transports import the neutral catalog.
TASK_STREAM_PLATFORMS = PLATFORM_IDS
LEGACY_TASK_STREAM_KEY = "lottery_tasks"
LEGACY_TASK_GROUP_NAME = "workers"
LEGACY_TASK_FANOUT_CONSUMER_NAME = "core-legacy-fanout"
REPAIR_TASK_PROTOCOL_VERSION = 1
REPAIR_TASK_STREAM_PREFIX = (
    f"lottery_repair_tasks:v{REPAIR_TASK_PROTOCOL_VERSION}"
)
REPAIR_TASK_GROUP_PREFIX = (
    f"repair-workers:v{REPAIR_TASK_PROTOCOL_VERSION}"
)
MAX_SAFE_TERMINAL_CONSUMER_GROUPS = 32

# Retain a task entry until every consumer group has advanced past the exact
# message ID and no group still owns it in a PEL. This keeps terminal XACK and
# stream retention atomic without assuming that the configured group is the
# only group an operator has attached to a lane.
_ALL_GROUPS_CONFIRMED_LUA = (
    """
local function decimal_compare(left, right)
  left = string.gsub(left or '', '^0+', '')
  right = string.gsub(right or '', '^0+', '')
  if left == '' then left = '0' end
  if right == '' then right = '0' end
  if string.len(left) ~= string.len(right) then
    return string.len(left) > string.len(right) and 1 or -1
  end
  if left == right then
    return 0
  end
  return left > right and 1 or -1
end

local function id_gte(left, right)
  local left_ms, left_seq = string.match(left or '', '^(%d+)%-(%d+)$')
  local right_ms, right_seq = string.match(right or '', '^(%d+)%-(%d+)$')
  if not left_ms or not right_ms then
    return false
  end
  local millisecond_order = decimal_compare(left_ms, right_ms)
  return millisecond_order > 0
    or (
      millisecond_order == 0
      and decimal_compare(left_seq, right_seq) >= 0
    )
end

local function all_groups_confirmed(stream_key, message_id)
  local groups = redis.call('XINFO', 'GROUPS', stream_key)
"""
    + f"""
  if #groups == 0 then
    return false
  end
  if #groups > {MAX_SAFE_TERMINAL_CONSUMER_GROUPS} then
    return false
  end
"""
    + """
  for _, group in ipairs(groups) do
    local group_name = nil
    local last_delivered_id = nil
    for index = 1, #group, 2 do
      if group[index] == 'name' then
        group_name = group[index + 1]
      elseif group[index] == 'last-delivered-id' then
        last_delivered_id = group[index + 1]
      end
    end
    if not group_name or not id_gte(last_delivered_id, message_id) then
      return false
    end
    local pending = redis.call(
      'XPENDING',
      stream_key,
      group_name,
      message_id,
      message_id,
      1
    )
    if #pending > 0 then
      return false
    end
  end
  return true
end
"""
)

SAFE_TERMINAL_STREAM_ACK_DELETE_LUA = (
    _ALL_GROUPS_CONFIRMED_LUA
    + """
local acknowledged = redis.call('XACK', KEYS[1], ARGV[1], ARGV[2])
local deleted = 0
if all_groups_confirmed(KEYS[1], ARGV[2]) then
  deleted = redis.call('XDEL', KEYS[1], ARGV[2])
end
return {acknowledged, deleted}
"""
)
# Backward-compatible name retained for task workers and operational tooling.
SAFE_TERMINAL_TASK_ACK_DELETE_LUA = SAFE_TERMINAL_STREAM_ACK_DELETE_LUA

SAFE_CONFIRMED_STREAM_ENTRY_DELETE_LUA = (
    _ALL_GROUPS_CONFIRMED_LUA
    + """
local confirmed = 0
local deleted = 0
if all_groups_confirmed(KEYS[1], ARGV[1]) then
  confirmed = 1
  deleted = redis.call('XDEL', KEYS[1], ARGV[1])
end
return {confirmed, deleted}
"""
)

SAFE_TERMINAL_FANOUT_ACK_DELETE_LUA = (
    _ALL_GROUPS_CONFIRMED_LUA
    + """
local marker_authorized = redis.call('SISMEMBER', KEYS[2], ARGV[3])
local acknowledged = redis.call('XACK', KEYS[1], ARGV[1], ARGV[2])
local deleted = 0
if marker_authorized == 1
   and all_groups_confirmed(KEYS[1], ARGV[2]) then
  deleted = redis.call('XDEL', KEYS[1], ARGV[2])
  redis.call('SREM', KEYS[2], ARGV[3])
  if redis.call('SCARD', KEYS[2]) == 0 then
    redis.call('DEL', KEYS[2])
  end
end
return {acknowledged, deleted}
"""
)

SAFE_FANOUT_RECOVERY_REENQUEUE_LUA = (
    _ALL_GROUPS_CONFIRMED_LUA
    + """
local source_member = ARGV[3] .. ARGV[2]
if redis.call('SISMEMBER', KEYS[2], source_member) ~= 1 then
  return 'already_reenqueued'
end
local fields = {}
for index = 4, #ARGV do
  fields[#fields + 1] = ARGV[index]
end
local target_id = redis.call('XADD', KEYS[1], '*', unpack(fields))
redis.call('SADD', KEYS[2], ARGV[3] .. target_id)
redis.call('SREM', KEYS[2], source_member)
redis.call('XACK', KEYS[1], ARGV[1], ARGV[2])
if all_groups_confirmed(KEYS[1], ARGV[2]) then
  redis.call('XDEL', KEYS[1], ARGV[2])
end
return target_id
"""
)

TASK_STREAM_KEYS = MappingProxyType(
    {
        platform: f"{LEGACY_TASK_STREAM_KEY}:{platform}"
        for platform in TASK_STREAM_PLATFORMS
    }
)
TASK_GROUP_NAMES = MappingProxyType(
    {
        platform: f"{LEGACY_TASK_GROUP_NAME}:{platform}"
        for platform in TASK_STREAM_PLATFORMS
    }
)
REPAIR_TASK_STREAM_KEYS = MappingProxyType(
    {
        platform: f"{REPAIR_TASK_STREAM_PREFIX}:{platform}"
        for platform in TASK_STREAM_PLATFORMS
    }
)
REPAIR_TASK_GROUP_NAMES = MappingProxyType(
    {
        platform: f"{REPAIR_TASK_GROUP_PREFIX}:{platform}"
        for platform in TASK_STREAM_PLATFORMS
    }
)


@dataclass(frozen=True)
class TaskStreamBinding:
    stream_key: str
    group_name: str
    platform: str | None
    legacy: bool = False
    repair: bool = False
    protocol_version: int | None = None


PLATFORM_TASK_STREAM_BINDINGS = tuple(
    TaskStreamBinding(
        stream_key=TASK_STREAM_KEYS[platform],
        group_name=TASK_GROUP_NAMES[platform],
        platform=platform,
    )
    for platform in TASK_STREAM_PLATFORMS
)
PLATFORM_REPAIR_TASK_STREAM_BINDINGS = tuple(
    TaskStreamBinding(
        stream_key=REPAIR_TASK_STREAM_KEYS[platform],
        group_name=REPAIR_TASK_GROUP_NAMES[platform],
        platform=platform,
        repair=True,
        protocol_version=REPAIR_TASK_PROTOCOL_VERSION,
    )
    for platform in TASK_STREAM_PLATFORMS
)
LEGACY_TASK_STREAM_BINDING = TaskStreamBinding(
    stream_key=LEGACY_TASK_STREAM_KEY,
    group_name=LEGACY_TASK_GROUP_NAME,
    platform=None,
    legacy=True,
)
_BINDINGS_BY_STREAM = MappingProxyType(
    {
        binding.stream_key: binding
        for binding in (
            *PLATFORM_TASK_STREAM_BINDINGS,
            *PLATFORM_REPAIR_TASK_STREAM_BINDINGS,
            LEGACY_TASK_STREAM_BINDING,
        )
    }
)


def task_stream_binding_for_platform(platform: str) -> TaskStreamBinding:
    normalized = str(platform or "").strip().casefold()
    for binding in PLATFORM_TASK_STREAM_BINDINGS:
        if binding.platform == normalized:
            return binding
    raise ValueError(f"task_stream_platform_unsupported:{normalized or 'missing'}")


def task_stream_for_platform(platform: str) -> str:
    return task_stream_binding_for_platform(platform).stream_key


def repair_task_stream_binding_for_platform(
    platform: str,
) -> TaskStreamBinding:
    normalized = str(platform or "").strip().casefold()
    for binding in PLATFORM_REPAIR_TASK_STREAM_BINDINGS:
        if binding.platform == normalized:
            return binding
    raise ValueError(
        f"repair_task_stream_platform_unsupported:{normalized or 'missing'}"
    )


def repair_task_stream_for_platform(platform: str) -> str:
    return repair_task_stream_binding_for_platform(platform).stream_key


def task_stream_binding_for_key(stream_key: str) -> TaskStreamBinding | None:
    return _BINDINGS_BY_STREAM.get(str(stream_key or "").strip())


def is_task_stream(stream_key: str) -> bool:
    return task_stream_binding_for_key(stream_key) is not None


def task_stream_bindings(
    *,
    include_legacy: bool,
    include_repair: bool = True,
) -> tuple[TaskStreamBinding, ...]:
    platform_bindings = PLATFORM_TASK_STREAM_BINDINGS
    if include_repair:
        platform_bindings = (
            *platform_bindings,
            *PLATFORM_REPAIR_TASK_STREAM_BINDINGS,
        )
    if include_legacy:
        return (*platform_bindings, LEGACY_TASK_STREAM_BINDING)
    return tuple(platform_bindings)


def validate_task_stream_message(
    binding: TaskStreamBinding,
    message: dict,
) -> None:
    """Validate the transport-lane contract without interpreting actions."""

    platform = str(message.get("platform") or "").strip().casefold()
    intent_kind = str(
        message.get("execution_intent_kind") or ""
    ).strip().casefold()
    mode = str(message.get("mode") or "").strip().casefold()

    if binding.legacy:
        # Repair did not exist in the historical protocol. Never fan it into a
        # standard lane where a pre-repair Worker could execute the full plan.
        if intent_kind == "repair":
            raise ValueError("legacy_task_stream_repair_forbidden")
        if platform:
            task_stream_binding_for_platform(platform)
        return

    if not platform or platform != binding.platform:
        raise ValueError("task_stream_platform_mismatch")
    if binding.repair:
        if mode != "real_run" or intent_kind != "repair":
            raise ValueError("repair_task_stream_contract_mismatch")
        return
    if intent_kind == "repair":
        raise ValueError("standard_task_stream_repair_forbidden")


__all__ = (
    "LEGACY_TASK_FANOUT_CONSUMER_NAME",
    "LEGACY_TASK_GROUP_NAME",
    "LEGACY_TASK_STREAM_BINDING",
    "LEGACY_TASK_STREAM_KEY",
    "MAX_SAFE_TERMINAL_CONSUMER_GROUPS",
    "PLATFORM_REPAIR_TASK_STREAM_BINDINGS",
    "PLATFORM_TASK_STREAM_BINDINGS",
    "REPAIR_TASK_GROUP_NAMES",
    "REPAIR_TASK_GROUP_PREFIX",
    "REPAIR_TASK_PROTOCOL_VERSION",
    "REPAIR_TASK_STREAM_KEYS",
    "REPAIR_TASK_STREAM_PREFIX",
    "SAFE_FANOUT_RECOVERY_REENQUEUE_LUA",
    "SAFE_CONFIRMED_STREAM_ENTRY_DELETE_LUA",
    "SAFE_TERMINAL_FANOUT_ACK_DELETE_LUA",
    "SAFE_TERMINAL_STREAM_ACK_DELETE_LUA",
    "SAFE_TERMINAL_TASK_ACK_DELETE_LUA",
    "TASK_GROUP_NAMES",
    "TASK_STREAM_KEYS",
    "TASK_STREAM_PLATFORMS",
    "TaskStreamBinding",
    "is_task_stream",
    "repair_task_stream_binding_for_platform",
    "repair_task_stream_for_platform",
    "task_stream_binding_for_key",
    "task_stream_binding_for_platform",
    "task_stream_bindings",
    "task_stream_for_platform",
    "validate_task_stream_message",
)
