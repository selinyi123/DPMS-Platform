"""Durable account-calibration enqueue and terminal failure convergence."""

from __future__ import annotations

import json
import uuid

from app.account_calibration_streams import (
    ACCOUNT_CALIBRATION_OUTBOX_DEDUP_PREFIX,
    SAFE_ACCOUNT_FALLBACK_STATUSES,
    account_calibration_stream_binding_for_key,
    account_calibration_stream_binding_for_platform,
    validate_account_calibration_stream_message,
)
from app.db import database, execute_affected_rows


def build_account_calibration_message(
    *,
    calibration_id: str,
    account_id: int,
    platform: str,
    check_url: str,
    calibration_kind: str,
    fallback_account_status: str = "login_required",
) -> dict[str, str]:
    message = {
        "calibration_id": str(calibration_id),
        "account_id": str(int(account_id)),
        "platform": str(platform or "").strip().casefold(),
        "check_url": str(check_url or "").strip(),
        "calibration_kind": str(calibration_kind or "").strip().casefold(),
        "fallback_account_status": str(
            fallback_account_status or "login_required"
        )
        .strip()
        .casefold(),
    }
    binding = account_calibration_stream_binding_for_platform(
        message["platform"]
    )
    validate_account_calibration_stream_message(binding, message)
    return message


async def enqueue_account_calibration_outbox(message: dict[str, str]) -> None:
    """Persist one delivery in the caller's open calibration transaction."""

    binding = account_calibration_stream_binding_for_platform(
        str(message.get("platform") or "")
    )
    validate_account_calibration_stream_message(binding, message)
    # Lazy import avoids a module cycle when the generic dispatcher registers
    # this module's terminal-failure handler.
    from app.services.outbox import enqueue_outbox

    await enqueue_outbox(
        message,
        binding.stream_key,
        dedup_key=(
            f"{ACCOUNT_CALIBRATION_OUTBOX_DEDUP_PREFIX}"
            f"{message['calibration_id']}"
        ),
    )


def _payload_dict(payload) -> dict:
    if isinstance(payload, dict):
        return dict(payload)
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8", errors="strict")
    if isinstance(payload, str):
        parsed = json.loads(payload)
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("account_calibration_outbox_payload_invalid")


async def settle_terminal_account_calibration_delivery_failure(
    current,
    attempts: int,
    error: str,
) -> bool:
    """Fail only the exact still-queued calibration after relay exhaustion.

    The generic outbox dispatcher calls this inside the transaction that owns
    the locked outbox row.  An ambiguous XADD may already have been claimed by
    a Worker, so ``running`` and terminal rows are deliberately left alone.
    """

    current_data = dict(current or {})
    if (
        str(current_data.get("status") or "").strip().casefold() != "sending"
        or int(current_data.get("attempts") or 0) != int(attempts)
    ):
        return False
    stream_key = str(current_data.get("stream_key") or "").strip()
    binding = account_calibration_stream_binding_for_key(stream_key)
    if binding is None:
        return False
    dedup_key = str(current_data.get("dedup_key") or "").strip()
    if not dedup_key.startswith(ACCOUNT_CALIBRATION_OUTBOX_DEDUP_PREFIX):
        return False
    calibration_id = dedup_key[
        len(ACCOUNT_CALIBRATION_OUTBOX_DEDUP_PREFIX) :
    ]
    try:
        if str(uuid.UUID(calibration_id)) != calibration_id.casefold():
            return False
    except (AttributeError, TypeError, ValueError):
        return False

    message = None
    try:
        candidate = _payload_dict(current_data.get("payload"))
        validate_account_calibration_stream_message(binding, candidate)
        if str(candidate.get("calibration_id") or "").strip() == calibration_id:
            message = candidate
    except (TypeError, ValueError, json.JSONDecodeError, UnicodeDecodeError):
        # The dedup key and calibration row remain sufficient authority to
        # converge a corrupted control envelope. Never recover a permissive
        # prior status from malformed payload bytes; quarantine it below.
        message = None

    calibration = await database.fetch_one(
        """SELECT id, calibration_id, account_id, platform, check_url, status
             FROM account_calibrations
            WHERE calibration_id = :calibration_id
            FOR UPDATE""",
        {"calibration_id": calibration_id},
    )
    if not calibration:
        return False
    calibration_platform = str(
        calibration["platform"] or ""
    ).strip().casefold()
    if (
        str(calibration["status"] or "").strip().casefold() != "queued"
        or (
            binding.platform is not None
            and calibration_platform != binding.platform
        )
        or (
            message is not None
            and (
                int(calibration["account_id"] or 0)
                != int(message["account_id"])
                or calibration_platform
                != str(message["platform"]).strip().casefold()
                or str(calibration["check_url"] or "").strip()
                != str(message["check_url"]).strip()
            )
        )
    ):
        return False

    account = await database.fetch_one(
        """SELECT id, status, deleted_at
             FROM accounts
            WHERE id = :account_id
            FOR UPDATE""",
        {"account_id": calibration["account_id"]},
    )
    updated = await execute_affected_rows(
        """UPDATE account_calibrations
              SET status = 'failed',
                  error_message = :error,
                  finished_at = NOW()
            WHERE calibration_id = :calibration_id
              AND account_id = :account_id
              AND platform = :platform
              AND status = 'queued'""",
        {
            "calibration_id": calibration_id,
            "account_id": calibration["account_id"],
            "platform": calibration["platform"],
            "error": f"outbox delivery exhausted: {error}"[:480],
        },
        db=database,
    )
    if updated != 1:
        return False

    fallback = str(
        (
            message.get("fallback_account_status")
            if message is not None
            else "frozen"
        )
        or "frozen"
    ).strip().casefold()
    if fallback not in SAFE_ACCOUNT_FALLBACK_STATUSES:
        fallback = "login_required"
    if (
        account
        and account["deleted_at"] is None
        and str(account["status"] or "").strip().casefold() == "warming"
    ):
        latest = await database.fetch_one(
            """SELECT calibration_id
                 FROM account_calibrations
                WHERE account_id = :account_id
                ORDER BY id DESC
                LIMIT 1
                FOR UPDATE""",
            {"account_id": calibration["account_id"]},
        )
        if (
            latest
            and str(latest["calibration_id"] or "").strip() == calibration_id
        ):
            await database.execute(
                """UPDATE accounts
                      SET status = :fallback_status,
                          updated_at = NOW(),
                          version = version + 1
                    WHERE id = :account_id
                      AND status = 'warming'
                      AND deleted_at IS NULL""",
                {
                    "account_id": calibration["account_id"],
                    "fallback_status": fallback,
                },
            )
    return True


__all__ = (
    "ACCOUNT_CALIBRATION_OUTBOX_DEDUP_PREFIX",
    "build_account_calibration_message",
    "enqueue_account_calibration_outbox",
    "settle_terminal_account_calibration_delivery_failure",
)
