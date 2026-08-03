#!/usr/bin/env python3
"""Explicit, fail-closed retirement for one DPMS Redis consumer group.

The normal Core/Worker identities intentionally cannot run XGROUP DESTROY.
This tool accepts only a separate ``REDIS_GROUP_ADMIN_URL`` and requires both
a short-lived retirement intent and an exact allowlist approval.  Dry-run is
the default.  Execution atomically rechecks lag, pending and consumer idleness
before destroying the group.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
import sys
import uuid


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from redis.asyncio import from_url  # noqa: E402

from shared.account_calibration_streams import (  # noqa: E402
    account_calibration_stream_binding_for_key,
)
from shared.adapter_probe_streams import (  # noqa: E402
    adapter_probe_stream_binding_for_key,
)
from shared.redis_consumer_groups import (  # noqa: E402
    MAX_OBSERVED_CONSUMER_GROUPS,
    MAX_OBSERVED_CONSUMERS_PER_GROUP,
    consumer_group_specs_for_stream,
    is_governed_consumer_group_stream,
    normalized_consumer_group_name,
)
from shared.login_streams import is_login_request_stream  # noqa: E402
from shared.task_streams import (  # noqa: E402
    SAFE_CONFIRMED_STREAM_ENTRY_DELETE_LUA,
    task_stream_binding_for_key,
)


MAX_POLICY_BYTES = 64 * 1024
MIN_INACTIVE_SECONDS = 3_600
MAX_INACTIVE_SECONDS = 86_400
MAX_INTENT_LIFETIME_SECONDS = 24 * 60 * 60
MAX_BREAK_GLASS_CONSUMER_GROUPS = 256
DEFAULT_RETENTION_SWEEP_LIMIT = 100
MAX_RETENTION_SWEEP_LIMIT = 256
REDIS_STREAM_ID_PATTERN = re.compile(r"^[0-9]+-[0-9]+$")

RETIRE_CONSUMER_GROUP_LUA = """
local target_group = ARGV[1]
local inactive_milliseconds = tonumber(ARGV[2])
local max_consumers = tonumber(ARGV[3])
local allow_oversized_inventory = ARGV[4] == '1'
local normal_max_groups = tonumber(ARGV[5])
local break_glass_max_groups = tonumber(ARGV[6])
local groups = redis.call('XINFO', 'GROUPS', KEYS[1])
local found = nil

if #groups > break_glass_max_groups then
  return {'blocked_group_inventory_hard', tostring(#groups)}
end
if #groups > normal_max_groups and not allow_oversized_inventory then
  return {'blocked_group_inventory', tostring(#groups)}
end

for _, group in ipairs(groups) do
  local name = nil
  local pending = nil
  local lag = nil
  for index = 1, #group, 2 do
    if group[index] == 'name' then
      name = group[index + 1]
    elseif group[index] == 'pending' then
      pending = tonumber(group[index + 1])
    elseif group[index] == 'lag' then
      lag = tonumber(group[index + 1])
    end
  end
  if name == target_group then
    found = {pending, lag}
    break
  end
end

if not found then
  return {'absent', '0'}
end
if found[1] == nil or found[1] ~= 0 then
  return {'blocked_pending', tostring(found[1] or 'unknown')}
end
if found[2] == nil or found[2] ~= 0 then
  return {'blocked_lag', tostring(found[2] or 'unknown')}
end

local consumers = redis.call(
  'XINFO', 'CONSUMERS', KEYS[1], target_group
)
if #consumers > max_consumers then
  return {'blocked_consumer_inventory', tostring(#consumers)}
end
for _, consumer in ipairs(consumers) do
  local idle = nil
  for index = 1, #consumer, 2 do
    if consumer[index] == 'idle' then
      idle = tonumber(consumer[index + 1])
    end
  end
  if idle == nil or idle <= inactive_milliseconds then
    return {'blocked_active_consumer', tostring(idle or 'unknown')}
  end
end

local destroyed = redis.call(
  'XGROUP', 'DESTROY', KEYS[1], target_group
)
return {'destroyed', tostring(destroyed)}
"""


class RetirementRefused(RuntimeError):
    pass


def _safe_operation_error(exc: Exception) -> str:
    if isinstance(exc, RetirementRefused):
        return str(exc)[:512]
    # Redis/client exceptions can embed connection configuration.
    return f"retirement_operation_failed:{type(exc).__name__}"


def _read_json_file(path: Path) -> dict:
    resolved = path.resolve()
    size = resolved.stat().st_size
    if size <= 0 or size > MAX_POLICY_BYTES:
        raise RetirementRefused("retirement_policy_file_size_invalid")
    value = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RetirementRefused("retirement_policy_document_invalid")
    return value


def _parse_utc_timestamp(value, *, field: str) -> datetime:
    raw = str(value or "").strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise RetirementRefused(
            f"retirement_intent_{field}_invalid"
        ) from exc
    if parsed.tzinfo is None:
        raise RetirementRefused(f"retirement_intent_{field}_timezone_required")
    return parsed.astimezone(timezone.utc)


def _approval_matches_intent(
    approval: dict,
    *,
    intent_id: str,
    stream: str,
    group: str,
    actor: str,
    ticket: str,
    reason: str,
    inactive_seconds: int,
    break_glass_oversized_inventory: bool,
    created_at: datetime,
    expires_at: datetime,
) -> bool:
    """Bind approval to every field that changes retirement authority."""

    if not isinstance(approval, dict):
        return False
    try:
        approved_inactive = approval.get("inactive_for_seconds")
        if isinstance(approved_inactive, bool):
            return False
        approved_inactive = int(approved_inactive)
        approved_created = _parse_utc_timestamp(
            approval.get("created_at"),
            field="approval_created_at",
        )
        approved_expires = _parse_utc_timestamp(
            approval.get("expires_at"),
            field="approval_expires_at",
        )
        approved_break_glass = approval.get(
            "break_glass_oversized_inventory",
            False,
        )
        if not isinstance(approved_break_glass, bool):
            return False
    except (RetirementRefused, TypeError, ValueError):
        return False
    return bool(
        str(approval.get("intent_id") or "").strip() == intent_id
        and str(approval.get("stream") or "").strip() == stream
        and normalized_consumer_group_name(approval.get("group")) == group
        and str(approval.get("actor") or "").strip() == actor
        and str(approval.get("ticket") or "").strip() == ticket
        and str(approval.get("reason") or "").strip() == reason
        and approved_inactive == inactive_seconds
        and (
            approved_break_glass
            is break_glass_oversized_inventory
        )
        and approved_created == created_at
        and approved_expires == expires_at
    )


def validate_retirement_intent(
    intent: dict,
    allowlist: dict,
    *,
    now: datetime | None = None,
) -> dict:
    """Validate two independent exact-pair approvals without Redis access."""

    if intent.get("version") != 1 or allowlist.get("version") != 1:
        raise RetirementRefused("retirement_policy_version_unsupported")
    intent_id = str(intent.get("intent_id") or "").strip()
    try:
        parsed_intent_id = uuid.UUID(intent_id)
    except ValueError as exc:
        raise RetirementRefused("retirement_intent_id_invalid") from exc
    if str(parsed_intent_id) != intent_id.casefold():
        raise RetirementRefused("retirement_intent_id_not_canonical")

    stream = str(intent.get("stream") or "").strip()
    group = normalized_consumer_group_name(intent.get("group"))
    if not is_governed_consumer_group_stream(stream):
        raise RetirementRefused("retirement_stream_not_governed")
    if group is None:
        raise RetirementRefused("retirement_group_name_invalid")
    if any(
        spec.group_name == group
        for spec in consumer_group_specs_for_stream(stream)
    ):
        # A current topology group is never a retirement candidate merely
        # because a low-traffic consumer happens to be idle.  Reject before
        # considering operator-provided inactivity or allowlist assertions:
        # the group must first be removed from deployed topology and stopped.
        raise RetirementRefused("retirement_current_topology_group_forbidden")

    actor = str(intent.get("actor") or "").strip()
    ticket = str(intent.get("ticket") or "").strip()
    reason = str(intent.get("reason") or "").strip()
    if not (3 <= len(actor) <= 128):
        raise RetirementRefused("retirement_actor_invalid")
    if not (3 <= len(ticket) <= 128):
        raise RetirementRefused("retirement_ticket_invalid")
    if not (10 <= len(reason) <= 512):
        raise RetirementRefused("retirement_reason_invalid")

    inactive_seconds = intent.get("inactive_for_seconds")
    if isinstance(inactive_seconds, bool):
        raise RetirementRefused("retirement_inactive_window_invalid")
    try:
        inactive_seconds = int(inactive_seconds)
    except (TypeError, ValueError) as exc:
        raise RetirementRefused(
            "retirement_inactive_window_invalid"
        ) from exc
    if not MIN_INACTIVE_SECONDS <= inactive_seconds <= MAX_INACTIVE_SECONDS:
        raise RetirementRefused("retirement_inactive_window_invalid")
    break_glass_oversized_inventory = intent.get(
        "break_glass_oversized_inventory",
        False,
    )
    if not isinstance(break_glass_oversized_inventory, bool):
        raise RetirementRefused(
            "retirement_break_glass_inventory_invalid"
        )

    created_at = _parse_utc_timestamp(
        intent.get("created_at"),
        field="created_at",
    )
    expires_at = _parse_utc_timestamp(
        intent.get("expires_at"),
        field="expires_at",
    )
    evaluation_time = (now or datetime.now(timezone.utc)).astimezone(
        timezone.utc
    )
    lifetime = (expires_at - created_at).total_seconds()
    if lifetime <= 0 or lifetime > MAX_INTENT_LIFETIME_SECONDS:
        raise RetirementRefused("retirement_intent_lifetime_invalid")
    if created_at > evaluation_time or expires_at <= evaluation_time:
        raise RetirementRefused("retirement_intent_not_active")

    allowed = allowlist.get("allowed")
    if not isinstance(allowed, list) or len(allowed) > 256:
        raise RetirementRefused("retirement_allowlist_invalid")
    exact_approvals = [
        item
        for item in allowed
        if _approval_matches_intent(
            item,
            intent_id=intent_id,
            stream=stream,
            group=group,
            actor=actor,
            ticket=ticket,
            reason=reason,
            inactive_seconds=inactive_seconds,
            break_glass_oversized_inventory=(
                break_glass_oversized_inventory
            ),
            created_at=created_at,
            expires_at=expires_at,
        )
    ]
    if len(exact_approvals) != 1:
        raise RetirementRefused("retirement_pair_not_exactly_approved")

    return {
        "version": 1,
        "intent_id": intent_id,
        "stream": stream,
        "group": group,
        "actor": actor,
        "ticket": ticket,
        "reason": reason,
        "created_at": created_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "inactive_for_seconds": inactive_seconds,
        "break_glass_oversized_inventory": (
            break_glass_oversized_inventory
        ),
        "known_group": False,
        "known_group_roles": [],
    }


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


async def inspect_retirement_candidate(client, intent: dict) -> dict:
    groups = list(await client.xinfo_groups(intent["stream"]) or ())
    group_count = len(groups)
    oversized_inventory = (
        group_count > MAX_OBSERVED_CONSUMER_GROUPS
    )
    if group_count > MAX_BREAK_GLASS_CONSUMER_GROUPS:
        raise RetirementRefused(
            "retirement_group_inventory_break_glass_limit_exceeded"
        )
    if (
        oversized_inventory
        and not intent["break_glass_oversized_inventory"]
    ):
        raise RetirementRefused("retirement_group_inventory_too_large")
    matching = [
        group
        for group in groups
        if normalized_consumer_group_name(_record_field(group, "name"))
        == intent["group"]
    ]
    if not matching:
        return {
            "exists": False,
            "groups_total": group_count,
            "inventory_oversized": oversized_inventory,
            "break_glass_used": bool(
                oversized_inventory
                and intent["break_glass_oversized_inventory"]
            ),
            "pending": 0,
            "lag": 0,
            "consumers": 0,
            "active_consumers": 0,
            "safe_to_retire": True,
        }
    if len(matching) != 1:
        raise RetirementRefused("retirement_group_inventory_ambiguous")
    group = matching[0]
    pending = _nonnegative_int(_record_field(group, "pending"))
    lag = _nonnegative_int(_record_field(group, "lag"))
    reported_consumers = _nonnegative_int(
        _record_field(group, "consumers")
    )
    if (
        reported_consumers is not None
        and reported_consumers > MAX_OBSERVED_CONSUMERS_PER_GROUP
    ):
        raise RetirementRefused("retirement_consumer_inventory_too_large")
    consumers = list(
        await client.xinfo_consumers(intent["stream"], intent["group"])
        or ()
    )
    if len(consumers) > MAX_OBSERVED_CONSUMERS_PER_GROUP:
        raise RetirementRefused("retirement_consumer_inventory_too_large")
    inactive_milliseconds = intent["inactive_for_seconds"] * 1000
    active_consumers = 0
    invalid_idle = False
    for consumer in consumers:
        idle = _nonnegative_int(_record_field(consumer, "idle"))
        if idle is None:
            invalid_idle = True
        elif idle <= inactive_milliseconds:
            active_consumers += 1
    safe = bool(
        pending == 0
        and lag == 0
        and not invalid_idle
        and active_consumers == 0
    )
    return {
        "exists": True,
        "groups_total": group_count,
        "inventory_oversized": oversized_inventory,
        "break_glass_used": bool(
            oversized_inventory
            and intent["break_glass_oversized_inventory"]
        ),
        "pending": pending,
        "lag": lag,
        "consumers": len(consumers),
        "active_consumers": active_consumers,
        "consumer_idle_metrics_valid": not invalid_idle,
        "safe_to_retire": safe,
    }


def _absolute_audit_path_without_symlinks(path: Path) -> Path:
    absolute = Path(os.path.abspath(os.fspath(path)))
    # ``Path.resolve`` follows the final symlink before ``is_symlink`` can
    # inspect it. Check every existing component first; O_NOFOLLOW below then
    # closes the final-component race on platforms that provide it.
    candidates = [
        candidate
        for candidate in reversed(absolute.parents)
        if candidate != Path(candidate.anchor)
    ]
    candidates.append(absolute)
    if any(candidate.is_symlink() for candidate in candidates):
        raise RetirementRefused("retirement_audit_symlink_forbidden")
    return absolute


def append_audit_record(path: Path, record: dict) -> None:
    """Append and fsync one secret-free JSON audit record."""

    resolved = _absolute_audit_path_without_symlinks(path).resolve(
        strict=False
    )
    if not resolved.parent.is_dir():
        raise RetirementRefused("retirement_audit_parent_missing")
    flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(resolved, flags, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise RetirementRefused("retirement_audit_not_regular_file")
        payload = (
            json.dumps(
                record,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


async def retire_consumer_group_atomically(client, intent: dict) -> dict:
    result = await client.eval(
        RETIRE_CONSUMER_GROUP_LUA,
        1,
        intent["stream"],
        intent["group"],
        str(intent["inactive_for_seconds"] * 1000),
        str(MAX_OBSERVED_CONSUMERS_PER_GROUP),
        (
            "1"
            if intent["break_glass_oversized_inventory"]
            else "0"
        ),
        str(MAX_OBSERVED_CONSUMER_GROUPS),
        str(MAX_BREAK_GLASS_CONSUMER_GROUPS),
    )
    values = list(result or ())
    status = str(values[0] if values else "")
    detail = str(values[1] if len(values) > 1 else "")
    if status not in {"destroyed", "absent"}:
        raise RetirementRefused(
            f"retirement_atomic_precondition_failed:{status}:{detail}"
        )
    if status == "destroyed" and detail != "1":
        raise RetirementRefused("retirement_destroy_result_invalid")
    return {"status": status, "destroyed": status == "destroyed"}


async def sweep_confirmed_stream_entries(
    client,
    database,
    *,
    stream: str,
    limit: int,
    cursor: str | None = None,
) -> dict:
    """Delete a bounded prefix only after every remaining group confirms it."""

    try:
        parsed_limit = int(limit)
    except (TypeError, ValueError) as exc:
        raise RetirementRefused("retirement_sweep_limit_invalid") from exc
    if (
        isinstance(limit, bool)
        or not 1 <= parsed_limit <= MAX_RETENTION_SWEEP_LIMIT
    ):
        raise RetirementRefused("retirement_sweep_limit_invalid")
    normalized_cursor = str(cursor or "").strip()
    if normalized_cursor and not REDIS_STREAM_ID_PATTERN.fullmatch(
        normalized_cursor
    ):
        raise RetirementRefused("retirement_sweep_cursor_invalid")
    range_start = (
        f"({normalized_cursor}" if normalized_cursor else "-"
    )
    entries = list(
        await client.xrange(
            stream,
            min=range_start,
            max="+",
            count=parsed_limit,
        )
        or ()
    )
    scanned = 0
    nonterminal = 0
    confirmed = 0
    deleted = 0
    invalid = 0
    next_cursor = normalized_cursor or None
    for entry in entries:
        if not isinstance(entry, (tuple, list)) or not entry:
            raise RetirementRefused(
                "retirement_sweep_stream_entry_invalid"
            )
        message_id = str(entry[0] or "").strip()
        if not message_id:
            raise RetirementRefused(
                "retirement_sweep_stream_entry_invalid"
            )
        if not REDIS_STREAM_ID_PATTERN.fullmatch(message_id):
            raise RetirementRefused(
                "retirement_sweep_stream_entry_invalid"
            )
        next_cursor = message_id
        fields = entry[1] if len(entry) > 1 else None
        if not isinstance(fields, dict):
            scanned += 1
            nonterminal += 1
            invalid += 1
            continue
        if not await _terminal_stream_entry_authorized(
            database,
            stream=stream,
            fields=fields,
        ):
            scanned += 1
            nonterminal += 1
            continue
        result = list(
            await client.eval(
                SAFE_CONFIRMED_STREAM_ENTRY_DELETE_LUA,
                1,
                stream,
                message_id,
            )
            or ()
        )
        if len(result) != 2:
            raise RetirementRefused(
                "retirement_sweep_result_invalid"
            )
        try:
            entry_confirmed = int(result[0])
            entry_deleted = int(result[1])
        except (TypeError, ValueError) as exc:
            raise RetirementRefused(
                "retirement_sweep_result_invalid"
            ) from exc
        if entry_confirmed not in {0, 1} or entry_deleted not in {0, 1}:
            raise RetirementRefused(
                "retirement_sweep_result_invalid"
            )
        if entry_deleted > entry_confirmed:
            raise RetirementRefused(
                "retirement_sweep_result_invalid"
            )
        scanned += 1
        confirmed += entry_confirmed
        deleted += entry_deleted
    return {
        "status": "completed",
        "limit": parsed_limit,
        "scanned": scanned,
        "nonterminal": nonterminal,
        "confirmed": confirmed,
        "deleted": deleted,
        "blocked": scanned - confirmed,
        "invalid": invalid,
        "cursor": normalized_cursor or None,
        "next_cursor": next_cursor,
        # Exactly ``limit`` entries may or may not have a successor. Report
        # conservatively so an operator advances the cursor until a short/empty
        # page, then starts a fresh cycle to revisit formerly nonterminal rows.
        "scan_complete": len(entries) < parsed_limit,
    }


def _canonical_uuid_field(fields: dict, name: str) -> str | None:
    raw = fields.get(name)
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
    value = str(raw or "").strip()
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, TypeError, ValueError):
        return None
    return value if str(parsed) == value.casefold() else None


async def _terminal_stream_entry_authorized(
    database,
    *,
    stream: str,
    fields: dict,
) -> bool:
    """Require durable DB terminal authority before deleting stream history."""

    terminal_statuses = {"succeeded", "failed"}
    if task_stream_binding_for_key(stream) is not None:
        identifier = _canonical_uuid_field(fields, "task_id")
        table = "task_runs"
        column = "task_id"
    elif adapter_probe_stream_binding_for_key(stream) is not None:
        identifier = _canonical_uuid_field(fields, "probe_id")
        table = "adapter_calibrations"
        column = "probe_id"
    elif account_calibration_stream_binding_for_key(stream) is not None:
        identifier = _canonical_uuid_field(fields, "calibration_id")
        table = "account_calibrations"
        column = "calibration_id"
    elif is_login_request_stream(stream):
        identifier = _canonical_uuid_field(fields, "session_id")
        table = "login_sessions"
        column = "session_id"
        terminal_statuses = {"confirmed", "failed", "expired"}
    else:
        return False
    if identifier is None:
        return False
    # Table and column are selected only from the fixed branches above.
    row = await database.fetch_one(
        f"""SELECT status
            FROM {table}
            WHERE {column} = :identifier
            LIMIT 1""",
        {"identifier": identifier},
    )
    return bool(
        row
        and str(row["status"] or "").strip().casefold()
        in terminal_statuses
    )


def _audit_record(
    *,
    event: str,
    intent: dict,
    observation: dict,
    result: dict | None = None,
    error: str | None = None,
) -> dict:
    record = {
        "event": event,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "intent": intent,
        "observation": observation,
    }
    if result is not None:
        record["result"] = result
    if error is not None:
        record["error"] = str(error)[:512]
    return record


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Safely inspect or retire one DPMS Redis consumer group."
    )
    parser.add_argument("--intent-file", type=Path, required=True)
    parser.add_argument("--allowlist-file", type=Path, required=True)
    parser.add_argument(
        "--audit-log",
        type=Path,
        help="Required with --execute; append-only JSONL destination.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Perform the atomic retirement. Default is dry-run.",
    )
    parser.add_argument(
        "--retention-sweep-limit",
        type=int,
        default=DEFAULT_RETENTION_SWEEP_LIMIT,
        help=(
            "After retirement, inspect at most this many oldest stream "
            "entries and delete only DB-terminal entries confirmed by every "
            "remaining group."
        ),
    )
    parser.add_argument(
        "--retention-sweep-cursor",
        help=(
            "Exclusive Redis stream ID returned as next_cursor by a prior "
            "bounded sweep. Omit to start a new scan cycle at the oldest row."
        ),
    )
    return parser


async def _run(args: argparse.Namespace) -> dict:
    intent = validate_retirement_intent(
        _read_json_file(args.intent_file),
        _read_json_file(args.allowlist_file),
    )
    redis_url = str(os.getenv("REDIS_GROUP_ADMIN_URL") or "").strip()
    if not redis_url:
        raise RetirementRefused("redis_group_admin_url_required")
    if args.execute and args.audit_log is None:
        raise RetirementRefused("retirement_audit_log_required")
    sweep_limit = args.retention_sweep_limit
    try:
        parsed_sweep_limit = int(sweep_limit)
    except (TypeError, ValueError) as exc:
        raise RetirementRefused(
            "retirement_sweep_limit_invalid"
        ) from exc
    if isinstance(sweep_limit, bool) or not (
        1 <= parsed_sweep_limit <= MAX_RETENTION_SWEEP_LIMIT
    ):
        raise RetirementRefused("retirement_sweep_limit_invalid")
    sweep_cursor = str(
        args.retention_sweep_cursor or ""
    ).strip()
    if sweep_cursor and not REDIS_STREAM_ID_PATTERN.fullmatch(
        sweep_cursor
    ):
        raise RetirementRefused("retirement_sweep_cursor_invalid")
    database = None
    if args.execute:
        database_url = str(os.getenv("DATABASE_URL") or "").strip()
        if not database_url:
            raise RetirementRefused("retirement_database_url_required")
        from databases import Database

        database = Database(
            database_url,
            init_command="SET time_zone = '+00:00'",
        )

    client = from_url(
        redis_url,
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=15,
    )
    try:
        if database is not None:
            # Prove terminal authority is reachable before performing the
            # irreversible XGROUP DESTROY.
            await database.connect()
        observation = await inspect_retirement_candidate(client, intent)
        if not args.execute:
            return {
                "mode": "dry_run",
                "intent": intent,
                "observation": observation,
            }
        if not observation["safe_to_retire"]:
            raise RetirementRefused(
                "retirement_candidate_has_pending_lag_or_active_consumers"
            )
        append_audit_record(
            args.audit_log,
            _audit_record(
                event="redis_consumer_group_retirement_attempted",
                intent=intent,
                observation=observation,
            ),
        )
        try:
            result = await retire_consumer_group_atomically(client, intent)
        except Exception as exc:
            append_audit_record(
                args.audit_log,
                _audit_record(
                    event="redis_consumer_group_retirement_failed",
                    intent=intent,
                    observation=observation,
                    error=_safe_operation_error(exc),
                ),
            )
            raise
        try:
            retention_sweep = await sweep_confirmed_stream_entries(
                client,
                database,
                stream=intent["stream"],
                limit=parsed_sweep_limit,
                cursor=sweep_cursor or None,
            )
        except Exception as exc:
            partial_result = {
                **result,
                "retention_sweep": {
                    "status": "failed",
                    "error": _safe_operation_error(exc),
                },
            }
            append_audit_record(
                args.audit_log,
                _audit_record(
                    event=(
                        "redis_consumer_group_retention_sweep_failed"
                    ),
                    intent=intent,
                    observation=observation,
                    result=partial_result,
                    error=_safe_operation_error(exc),
                ),
            )
            append_audit_record(
                args.audit_log,
                _audit_record(
                    event="redis_consumer_group_retirement_completed",
                    intent=intent,
                    observation=observation,
                    result=partial_result,
                ),
            )
            raise RetirementRefused(
                "retirement_completed_but_retention_sweep_failed"
            ) from exc
        result = {
            **result,
            "retention_sweep": retention_sweep,
        }
        append_audit_record(
            args.audit_log,
            _audit_record(
                event="redis_consumer_group_retirement_completed",
                intent=intent,
                observation=observation,
                result=result,
            ),
        )
        return {
            "mode": "execute",
            "intent_id": intent["intent_id"],
            "stream": intent["stream"],
            "group": intent["group"],
            **result,
        }
    finally:
        if database is not None:
            try:
                await database.disconnect()
            except Exception:
                pass
        await client.aclose()


def main() -> int:
    args = _build_parser().parse_args()
    try:
        result = asyncio.run(_run(args))
    except RetirementRefused as exc:
        print(
            json.dumps(
                {"ok": False, "error": str(exc)[:512]},
                ensure_ascii=True,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    except Exception as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": _safe_operation_error(exc),
                },
                ensure_ascii=True,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(
            {"ok": True, **result},
            ensure_ascii=True,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
