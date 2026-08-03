"""Transactional browser-login request delivery and failure convergence."""

from __future__ import annotations

import json

from app.db import database
from app.event_store.service import record_event
from app.services.account_profile_cleanup import (
    enqueue_login_profile_cleanup,
)
from app.login_streams import (
    LOGIN_REQUEST_OUTBOX_DEDUP_PREFIX,
    LOGIN_REQUEST_STREAM_KEY,
    is_login_request_stream,
    validate_login_request_stream_message,
)


def build_login_request_message(
    *,
    session_id: str,
    platform: str,
    login_url: str,
) -> dict[str, str]:
    message = {
        "session_id": str(session_id or "").strip().casefold(),
        "platform": str(platform or "").strip().casefold(),
        "login_url": str(login_url or "").strip(),
    }
    validate_login_request_stream_message(message)
    return message


async def enqueue_login_request_outbox(
    message: dict[str, str],
) -> None:
    validate_login_request_stream_message(message)
    from app.services.outbox import enqueue_outbox

    await enqueue_outbox(
        message,
        LOGIN_REQUEST_STREAM_KEY,
        dedup_key=(
            f"{LOGIN_REQUEST_OUTBOX_DEDUP_PREFIX}"
            f"{message['session_id']}"
        ),
    )


async def settle_terminal_login_request_delivery_failure(
    current: dict,
    attempts: int,
    error: str,
    *,
    db=None,
) -> bool:
    """Fail the exact queued session after its Redis relay exhausts retries."""

    del attempts
    target_db = db or database
    stream_key = str(current.get("stream_key") or "").strip()
    if not is_login_request_stream(stream_key):
        return False
    dedup_key = str(current.get("dedup_key") or "").strip()
    if not dedup_key.startswith(LOGIN_REQUEST_OUTBOX_DEDUP_PREFIX):
        return True
    session_id = dedup_key[len(LOGIN_REQUEST_OUTBOX_DEDUP_PREFIX) :]
    try:
        payload = current.get("payload")
        message = (
            json.loads(payload)
            if isinstance(payload, str)
            else dict(payload or {})
        )
        validate_login_request_stream_message(message)
    except (TypeError, ValueError, json.JSONDecodeError):
        message = None
    if (
        message is None
        or str(message.get("session_id") or "").strip() != session_id
    ):
        return True

    session = await target_db.fetch_one(
        """SELECT session_id, platform, login_url, status
           FROM login_sessions
           WHERE session_id = :session_id
           FOR UPDATE""",
        {"session_id": session_id},
    )
    if not session:
        return True
    if (
        str(session["status"] or "").strip().casefold() != "queued"
        or str(session["platform"] or "").strip().casefold()
        != message["platform"]
        or str(session["login_url"] or "").strip()
        != message["login_url"]
    ):
        return True
    del error
    await target_db.execute(
        """UPDATE login_sessions
           SET status = 'failed',
               error_message = :error,
               completed_at = NOW(),
               updated_at = NOW()
           WHERE session_id = :session_id
             AND status = 'queued'""",
        {
            "session_id": session_id,
            "error": "Login request delivery exhausted",
        },
    )
    await enqueue_login_profile_cleanup(
        session_id,
        db=target_db,
    )
    await record_event(
        aggregate="browser",
        aggregate_id=session_id,
        event_type="QrLoginFailed",
        payload={
            "platform": message["platform"],
            "error": "login_request_delivery_exhausted",
        },
        correlation_id=session_id,
    )
    return True


__all__ = (
    "build_login_request_message",
    "enqueue_login_request_outbox",
    "settle_terminal_login_request_delivery_failure",
)
