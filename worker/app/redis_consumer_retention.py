"""Conservative retirement of stale Worker Redis consumer identities."""

from __future__ import annotations

import asyncio

from app.config import settings
from app.db import database, redis
from app.worker_identity import WORKER_ID, is_worker_instance_id
from shared.account_calibration_streams import (
    LEGACY_ACCOUNT_CALIBRATION_FANOUT_CONSUMER_NAME,
)
from shared.adapter_probe_streams import (
    LEGACY_ADAPTER_PROBE_FANOUT_CONSUMER_NAME,
)
from shared.redis_consumer_groups import REDIS_CONSUMER_GROUP_SPECS
from shared.task_streams import LEGACY_TASK_FANOUT_CONSUMER_NAME


# Core reports a consumer stale after this same interval. Retirement still
# requires a missing live heartbeat, zero pending entries and an atomic idle
# recheck, so aligning the clocks removes only metadata that is already safe.
REDIS_CONSUMER_RETIRE_IDLE_SECONDS = (
    settings.redis_consumer_group_stale_seconds
)
REDIS_CONSUMER_RETIRE_MAX_PER_PASS = 64
REDIS_CONSUMER_INVENTORY_MAX = 512
REDIS_CONSUMER_OPERATION_TIMEOUT_SECONDS = 5
_RESERVED_CONSUMERS = frozenset(
    {
        "recovery-daemon",
        LEGACY_TASK_FANOUT_CONSUMER_NAME,
        LEGACY_ADAPTER_PROBE_FANOUT_CONSUMER_NAME,
        LEGACY_ACCOUNT_CALIBRATION_FANOUT_CONSUMER_NAME,
    }
)

_DELETE_STALE_CONSUMER_LUA = """
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
    if idle == nil or idle <= tonumber(ARGV[3]) then
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


def _field(row, name: str):
    if isinstance(row, dict):
        return row.get(name)
    try:
        return row[name]
    except (KeyError, TypeError):
        return getattr(row, name, None)


def _nonnegative_int(value) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _worker_group_specs():
    return tuple(
        spec
        for spec in REDIS_CONSUMER_GROUP_SPECS
        if spec.role == "primary"
        and spec.subsystem
        in {
            "lottery_task",
            "adapter_probe",
            "account_calibration",
            "login",
            "xiaohongshu_target_pursuit",
        }
    )


async def _live_worker_ids(db) -> frozenset[str]:
    rows = await asyncio.wait_for(
        db.fetch_all(
            """SELECT worker_id
                 FROM worker_heartbeats
                WHERE service_name = 'worker'
                  AND last_seen_at >= DATE_SUB(NOW(), INTERVAL 2 MINUTE)"""
        ),
        timeout=REDIS_CONSUMER_OPERATION_TIMEOUT_SECONDS,
    )
    return frozenset(
        str(_field(row, "worker_id") or "").strip()
        for row in rows or ()
        if str(_field(row, "worker_id") or "").strip()
    )


async def retire_stale_redis_consumers_once(
    *,
    redis_client=redis,
    db=database,
) -> dict[str, int]:
    """Delete only zero-pending, long-idle, non-live Worker identities.

    The Lua recheck closes the XINFO/DELCONSUMER race: a consumer that becomes
    active or gains pending work after observation is never deleted.
    """

    live_ids = set(await _live_worker_ids(db))
    live_ids.add(WORKER_ID)
    summary = {
        "groups_scanned": 0,
        "candidates": 0,
        "retired": 0,
        "skipped_live": 0,
        "oversized_groups": 0,
        "unavailable_groups": 0,
    }
    idle_milliseconds = REDIS_CONSUMER_RETIRE_IDLE_SECONDS * 1000
    for spec in _worker_group_specs():
        if summary["retired"] >= REDIS_CONSUMER_RETIRE_MAX_PER_PASS:
            break
        try:
            consumers = list(
                await asyncio.wait_for(
                    redis_client.xinfo_consumers(
                        spec.stream_key,
                        spec.group_name,
                    ),
                    timeout=REDIS_CONSUMER_OPERATION_TIMEOUT_SECONDS,
                )
                or ()
            )
        except Exception:
            summary["unavailable_groups"] += 1
            continue
        summary["groups_scanned"] += 1
        if len(consumers) > REDIS_CONSUMER_INVENTORY_MAX:
            summary["oversized_groups"] += 1
            continue
        for consumer in consumers:
            name = str(_field(consumer, "name") or "").strip()
            pending = _nonnegative_int(_field(consumer, "pending"))
            idle = _nonnegative_int(_field(consumer, "idle"))
            if (
                not name
                or name in _RESERVED_CONSUMERS
                or not is_worker_instance_id(name)
                or pending != 0
                or idle is None
                or idle <= idle_milliseconds
            ):
                continue
            if name in live_ids:
                summary["skipped_live"] += 1
                continue
            summary["candidates"] += 1
            result = await asyncio.wait_for(
                redis_client.eval(
                    _DELETE_STALE_CONSUMER_LUA,
                    1,
                    spec.stream_key,
                    spec.group_name,
                    name,
                    str(idle_milliseconds),
                ),
                timeout=REDIS_CONSUMER_OPERATION_TIMEOUT_SECONDS,
            )
            values = list(result or ())
            if str(values[0] if values else "") == "deleted":
                summary["retired"] += 1
                if summary["retired"] >= REDIS_CONSUMER_RETIRE_MAX_PER_PASS:
                    break
    return summary


__all__ = (
    "retire_stale_redis_consumers_once",
)
