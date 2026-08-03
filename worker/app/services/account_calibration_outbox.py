"""Worker-side transactional producer for account calibration deliveries."""

from __future__ import annotations

import json

from app.account_calibration_streams import (
    ACCOUNT_CALIBRATION_OUTBOX_DEDUP_PREFIX,
    account_calibration_stream_binding_for_platform,
    validate_account_calibration_stream_message,
)
from app.db import database


def build_account_calibration_message(
    *,
    calibration_id: str,
    account_id: int,
    platform: str,
    check_url: str,
    calibration_kind: str = "browser_session",
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


async def enqueue_account_calibration_outbox(
    message: dict[str, str],
) -> None:
    """Insert the delivery in the caller's already-open DB transaction."""

    binding = account_calibration_stream_binding_for_platform(
        str(message.get("platform") or "")
    )
    validate_account_calibration_stream_message(binding, message)
    await database.execute(
        """INSERT INTO outbox_events
             (stream_key, payload, status, dedup_key)
           VALUES (:stream_key, :payload, 'pending', :dedup_key)""",
        {
            "stream_key": binding.stream_key,
            "payload": json.dumps(
                message,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            "dedup_key": (
                f"{ACCOUNT_CALIBRATION_OUTBOX_DEDUP_PREFIX}"
                f"{message['calibration_id']}"
            ),
        },
    )


__all__ = (
    "build_account_calibration_message",
    "enqueue_account_calibration_outbox",
)
