"""Durable account-profile cleanup intents.

The caller must enqueue inside the same transaction that removes the account's
database credential.  Workers derive the filesystem path from the immutable
``(platform, account_id)`` pair; no caller-controlled path is persisted.
"""

from __future__ import annotations

import uuid

from app.db import database
from shared.platform_ids import PLATFORM_IDS


def normalize_cleanup_identity(account_id: int, platform: str) -> tuple[int, str]:
    if isinstance(account_id, bool):
        raise ValueError("account_profile_cleanup_account_id_invalid")
    try:
        normalized_account_id = int(account_id)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "account_profile_cleanup_account_id_invalid"
        ) from exc
    normalized_platform = str(platform or "").strip().casefold()
    if normalized_account_id <= 0:
        raise ValueError("account_profile_cleanup_account_id_invalid")
    if normalized_platform not in PLATFORM_IDS:
        raise ValueError("account_profile_cleanup_platform_invalid")
    return normalized_account_id, normalized_platform


async def enqueue_account_profile_cleanup(
    account_id: int,
    platform: str,
) -> None:
    """Insert one cleanup intent in the caller's open account transaction."""

    normalized_account_id, normalized_platform = normalize_cleanup_identity(
        account_id,
        platform,
    )
    await database.execute(
        """INSERT INTO account_profile_cleanup_intents
             (account_id, platform, status, next_attempt_at)
           VALUES
             (:account_id, :platform, 'pending', NOW())""",
        {
            "account_id": normalized_account_id,
            "platform": normalized_platform,
        },
    )


def normalize_login_session_id(session_id: str) -> str:
    value = str(session_id or "").strip()
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(
            "login_profile_cleanup_session_id_invalid"
        ) from exc
    if str(parsed) != value:
        raise ValueError("login_profile_cleanup_session_id_invalid")
    return str(parsed)


async def enqueue_login_profile_cleanup(
    session_id: str,
    *,
    db=None,
) -> None:
    """Idempotently bind one terminal login session to durable cleanup."""

    normalized_session_id = normalize_login_session_id(session_id)
    target_db = db or database
    await target_db.execute(
        """INSERT INTO login_profile_cleanup_intents
             (session_id, status, next_attempt_at)
           VALUES
             (:session_id, 'pending', NOW())
           ON DUPLICATE KEY UPDATE
             session_id = login_profile_cleanup_intents.session_id""",
        {"session_id": normalized_session_id},
    )


__all__ = (
    "enqueue_account_profile_cleanup",
    "enqueue_login_profile_cleanup",
    "normalize_cleanup_identity",
    "normalize_login_session_id",
)
