"""Pure health checks shared by task dispatch and transport metrics.

Redis consumer membership and a fresh process heartbeat prove only that an
identity exists.  Repair dispatch additionally requires the same Worker to
report successful progress on the exact platform stream/group lane.  Keeping
that contract here prevents the dispatch gate and operator metrics from
silently drifting apart.
"""

from __future__ import annotations

import json

from app.task_streams import TaskStreamBinding


WORKER_REPAIR_CAPABILITY = "repair_execution_intent_v1"
WORKER_TASK_LANE_HEALTH_COMPATIBLE_VERSIONS = frozenset({1, 2})
REPAIR_WORKER_HEARTBEAT_MAX_AGE_SECONDS = 45
REPAIR_CONSUMER_MAX_IDLE_MILLISECONDS = 45_000
REPAIR_HEARTBEAT_MAX_CONSUMER_ENTRIES = 256
REPAIR_HEARTBEAT_MAX_LANE_ENTRIES = 16
REPAIR_LANE_V2_INFLIGHT_LIMIT = 32
REPAIR_LANE_MAX_REPORTED_AGE_SECONDS = 86_400


def _record_field(record, field: str):
    if isinstance(record, dict):
        return record.get(field)
    try:
        return record[field]
    except (KeyError, TypeError):
        return getattr(record, field, None)


def _nonnegative_int(value) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _parse_json_field(value):
    if isinstance(value, (dict, list)) or value is None:
        return value
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    try:
        return json.loads(value)
    except Exception:
        return value


def repair_consumer_idle_by_name(consumers) -> dict[str, int]:
    """Return a bounded exact-group identity map with no task information."""

    consumer_rows = list(consumers or ())
    if len(consumer_rows) > REPAIR_HEARTBEAT_MAX_CONSUMER_ENTRIES:
        return {}
    idle_by_name: dict[str, int] = {}
    for consumer in consumer_rows:
        raw_name = _record_field(consumer, "name")
        if isinstance(raw_name, bytes):
            raw_name = raw_name.decode("utf-8", errors="replace")
        name = str(raw_name or "").strip()
        idle_milliseconds = _nonnegative_int(
            _record_field(consumer, "idle")
        )
        if (
            not name
            or len(name) > 128
            or idle_milliseconds is None
        ):
            continue
        previous = idle_by_name.get(name)
        if previous is None or idle_milliseconds < previous:
            idle_by_name[name] = idle_milliseconds
    return idle_by_name


def active_repair_consumer_names(consumers) -> frozenset[str]:
    """Return exact-group identities that touched Redis recently."""

    return frozenset(
        name
        for name, idle_milliseconds in (
            repair_consumer_idle_by_name(consumers).items()
        )
        if idle_milliseconds <= REPAIR_CONSUMER_MAX_IDLE_MILLISECONDS
    )


def _worker_row_supports_task_lane(
    row,
    *,
    binding: TaskStreamBinding,
    active_consumer_names: frozenset[str],
    consumer_idle_milliseconds_by_name: dict[str, int] | None = None,
    require_repair_capability: bool = False,
) -> bool:
    worker_id = str(_record_field(row, "worker_id") or "").strip()
    heartbeat_age = _nonnegative_int(
        _record_field(row, "heartbeat_age_seconds")
    )
    known_consumer_names = (
        frozenset(consumer_idle_milliseconds_by_name)
        if consumer_idle_milliseconds_by_name is not None
        else active_consumer_names
    )
    if (
        not worker_id
        or len(worker_id) > 128
        or worker_id not in known_consumer_names
        or heartbeat_age is None
        or heartbeat_age > REPAIR_WORKER_HEARTBEAT_MAX_AGE_SECONDS
    ):
        return False

    detail = _parse_json_field(_record_field(row, "detail"))
    if not isinstance(detail, dict):
        return False
    if (
        detail.get("task_consumer_name") != worker_id
    ):
        return False
    if require_repair_capability:
        capabilities = detail.get("capabilities")
        if (
            not isinstance(capabilities, list)
            or len(capabilities) > 32
            or WORKER_REPAIR_CAPABILITY not in capabilities
            or detail.get("execution_intent_contract_version") != 1
        ):
            return False

    lane_health = detail.get("task_lane_health")
    if not isinstance(lane_health, dict):
        return False
    lane_health_contract_version = _nonnegative_int(
        lane_health.get("contract_version")
    )
    if (
        lane_health_contract_version
        not in WORKER_TASK_LANE_HEALTH_COMPATIBLE_VERSIONS
    ):
        return False
    lanes = lane_health.get("lanes")
    if (
        not isinstance(lanes, list)
        or not lanes
        or len(lanes) > REPAIR_HEARTBEAT_MAX_LANE_ENTRIES
    ):
        return False

    for lane in lanes:
        if not isinstance(lane, dict):
            continue
        success_age = _nonnegative_int(
            lane.get("last_success_age_seconds")
        )
        consecutive_failures = _nonnegative_int(
            lane.get("consecutive_failures")
        )
        recent_read = bool(
            worker_id in active_consumer_names
            and lane.get("last_success_operation") == "xreadgroup"
            and success_age is not None
            and success_age + heartbeat_age
            <= REPAIR_WORKER_HEARTBEAT_MAX_AGE_SECONDS
        )
        recent_saturated_progress = False
        if lane_health_contract_version >= 2:
            progress_age = _nonnegative_int(
                lane.get("last_loop_progress_age_seconds")
            )
            inflight_count = _nonnegative_int(
                lane.get("inflight_count")
            )
            inflight_limit = _nonnegative_int(
                lane.get("inflight_limit")
            )
            recent_saturated_progress = bool(
                worker_id in known_consumer_names
                and lane.get("last_success_operation") == "xreadgroup"
                and success_age is not None
                and success_age
                <= REPAIR_LANE_MAX_REPORTED_AGE_SECONDS
                and lane.get("last_loop_progress_operation")
                == "capacity_wait"
                and progress_age is not None
                and progress_age + heartbeat_age
                <= REPAIR_WORKER_HEARTBEAT_MAX_AGE_SECONDS
                and inflight_limit == REPAIR_LANE_V2_INFLIGHT_LIMIT
                and inflight_count == inflight_limit
                and lane.get("saturated") is True
            )
        if (
            lane.get("stream") != binding.stream_key
            or lane.get("group") != binding.group_name
            or lane.get("platform") != binding.platform
            or lane.get("repair") is not bool(binding.repair)
            or lane.get("protocol_version") != binding.protocol_version
            or lane.get("status") != "healthy"
            or consecutive_failures != 0
            or not (recent_read or recent_saturated_progress)
        ):
            continue
        return True
    return False


def worker_rows_support_task_lane(
    rows,
    *,
    binding: TaskStreamBinding,
    active_consumer_names: frozenset[str],
    consumer_idle_milliseconds_by_name: dict[str, int] | None = None,
    require_repair_capability: bool = False,
) -> bool:
    """Accept any fresh Worker reporting progress on this exact task lane."""

    if (
        binding.legacy
        or not binding.platform
        or not (
            active_consumer_names
            or consumer_idle_milliseconds_by_name
        )
    ):
        return False
    return any(
        _worker_row_supports_task_lane(
            row,
            binding=binding,
            active_consumer_names=active_consumer_names,
            consumer_idle_milliseconds_by_name=(
                consumer_idle_milliseconds_by_name
            ),
            require_repair_capability=require_repair_capability,
        )
        for row in list(rows or ())
    )


def worker_rows_support_repair_dispatch(
    rows,
    *,
    binding: TaskStreamBinding,
    active_consumer_names: frozenset[str],
    consumer_idle_milliseconds_by_name: dict[str, int] | None = None,
) -> bool:
    """Accept any capable, fresh Worker serving this exact repair lane."""

    if (
        not binding.repair
        or not binding.platform
        or not (
            active_consumer_names
            or consumer_idle_milliseconds_by_name
        )
    ):
        return False
    return worker_rows_support_task_lane(
        rows,
        binding=binding,
        active_consumer_names=active_consumer_names,
        consumer_idle_milliseconds_by_name=(
            consumer_idle_milliseconds_by_name
        ),
        require_repair_capability=True,
    )


__all__ = [
    "REPAIR_CONSUMER_MAX_IDLE_MILLISECONDS",
    "active_repair_consumer_names",
    "repair_consumer_idle_by_name",
    "worker_rows_support_repair_dispatch",
    "worker_rows_support_task_lane",
]
