"""Outbox terminal-settlement contract for adapter-probe deliveries."""

from __future__ import annotations

import json

from app.adapter_probe_streams import (
    ADAPTER_PROBE_STREAM_FIELDS,
    adapter_probe_stream_binding_for_key,
    is_adapter_probe_stream,
    validate_adapter_probe_stream_message,
)
from app.db import database


_PROBE_OUTBOX_FIELDS = ADAPTER_PROBE_STREAM_FIELDS


async def settle_terminal_adapter_probe_delivery_failure(
    current: dict,
    attempts: int,
    error: str,
    *,
    db=None,
) -> bool:
    """Close a queued Probe whose durable Redis delivery exhausted retries.

    The caller owns the ``outbox_events`` row lock and has already moved it to
    ``failed``.  ``True`` means this helper owns the stream type, even when a
    corrupted envelope cannot safely identify a business row.
    """

    del attempts
    target_db = db or database
    stream_key = str(current.get("stream_key") or "").strip()
    binding = adapter_probe_stream_binding_for_key(stream_key)
    if binding is None or not is_adapter_probe_stream(stream_key):
        return False

    payload = current.get("payload")
    try:
        message = (
            json.loads(payload)
            if isinstance(payload, str)
            else dict(payload or {})
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        message = {}
    try:
        validate_adapter_probe_stream_message(binding, message)
    except ValueError:
        return True
    probe_id = str(message.get("probe_id") or "").strip()
    if not probe_id:
        dedup_key = str(current.get("dedup_key") or "").strip()
        prefix = "adapter-probe:"
        if dedup_key.startswith(prefix):
            probe_id = dedup_key[len(prefix) :]
    if not probe_id:
        return True
    if (
        set(message) != _PROBE_OUTBOX_FIELDS
        or str(current.get("dedup_key") or "").strip()
        != f"adapter-probe:{probe_id}"
    ):
        return True

    probe = await target_db.fetch_one(
        """SELECT ac.account_id, ac.platform, ac.lottery_id, ac.target_url,
                  ac.status, ac.execution_path_id, ac.target_hash,
                  ac.rule_snapshot_id, ac.rule_hash, ac.action_plan_hash,
                  ac.config_hash, ac.account_lease_id,
                  ac.account_lease_generation, l.canonical_url,
                  a.execution_revision
           FROM adapter_calibrations ac
           LEFT JOIN lotteries l ON l.id = ac.lottery_id
           LEFT JOIN accounts a ON a.id = ac.account_id
           WHERE ac.probe_id = :probe_id FOR UPDATE""",
        {"probe_id": probe_id},
    )
    if not probe:
        return True
    platform = str(probe["platform"] or "").strip().casefold()
    if not binding.legacy and platform != binding.platform:
        # A lane/payload corruption must not release another platform's lease.
        return True
    persisted = {
        "probe_id": probe_id,
        "platform": platform,
        "account_id": str(probe["account_id"] or ""),
        "lottery_id": str(probe["lottery_id"] or ""),
        "target_url": str(probe["target_url"] or ""),
        "canonical_url": str(probe["canonical_url"] or ""),
        "execution_path_id": str(probe["execution_path_id"] or ""),
        "target_hash": str(probe["target_hash"] or ""),
        "rule_snapshot_id": str(probe["rule_snapshot_id"] or ""),
        "rule_hash": str(probe["rule_hash"] or ""),
        "action_plan_hash": str(probe["action_plan_hash"] or ""),
        "config_hash": str(probe["config_hash"] or ""),
        "execution_revision": str(probe["execution_revision"] or ""),
        "account_lease_id": str(probe["account_lease_id"] or ""),
        "account_lease_generation": str(
            probe["account_lease_generation"] or ""
        ),
    }
    if {
        key: str(value or "")
        for key, value in message.items()
    } != persisted:
        return True
    # XADD may have succeeded even when Core missed the acknowledgement. Once
    # Worker claimed the row, delivery failure must not fail work in flight.
    if str(probe["status"] or "").strip().casefold() != "queued":
        return True
    await target_db.fetch_one(
        "SELECT id FROM accounts WHERE id = :account_id FOR UPDATE",
        {"account_id": probe["account_id"]},
    )
    await target_db.execute(
        """UPDATE adapter_calibrations
           SET status = 'failed', error_message = :error, finished_at = NOW()
           WHERE probe_id = :probe_id AND status = 'queued'""",
        {
            "probe_id": probe_id,
            "error": f"outbox delivery exhausted: {error}"[:480],
        },
    )
    await target_db.execute(
        """UPDATE account_operation_leases
           SET released_at = COALESCE(released_at, NOW())
           WHERE lease_id = :lease_id
             AND account_id = :account_id
             AND generation = :lease_generation
             AND operation_kind = 'adapter_probe'
             AND owner_id = :probe_id""",
        {
            "lease_id": probe["account_lease_id"],
            "account_id": probe["account_id"],
            "lease_generation": probe["account_lease_generation"],
            "probe_id": probe_id,
        },
    )
    return True


__all__ = ("settle_terminal_adapter_probe_delivery_failure",)
