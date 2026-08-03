"""Cross-process ownership fence for persistent account browser profiles."""

from __future__ import annotations

import os
import uuid

from app.db import database
from app.worker_identity import WORKER_ID
from shared.platform_ids import PLATFORM_IDS


PROFILE_CONTEXT_LEASE_TTL_SECONDS = max(
    int(os.getenv("ACCOUNT_PROFILE_CONTEXT_LEASE_TTL_SECONDS", "180")),
    120,
)


class AccountProfileContextLeaseError(RuntimeError):
    def __init__(self, code: str):
        self.code = str(
            code or "account_profile_context_lease_failed"
        )[:128]
        super().__init__(self.code)


def normalize_account_profile_context_identity(
    account_id: int,
    platform: str,
) -> tuple[int, str]:
    if isinstance(account_id, bool):
        raise AccountProfileContextLeaseError(
            "account_profile_context_account_id_invalid"
        )
    try:
        normalized_account_id = int(account_id)
    except (TypeError, ValueError) as exc:
        raise AccountProfileContextLeaseError(
            "account_profile_context_account_id_invalid"
        ) from exc
    normalized_platform = str(platform or "").strip().casefold()
    if normalized_account_id <= 0:
        raise AccountProfileContextLeaseError(
            "account_profile_context_account_id_invalid"
        )
    if normalized_platform not in PLATFORM_IDS:
        raise AccountProfileContextLeaseError(
            "account_profile_context_platform_invalid"
        )
    return normalized_account_id, normalized_platform


def normalize_profile_context_lease_token(lease_token: str) -> str:
    value = str(lease_token or "").strip()
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, TypeError, ValueError) as exc:
        raise AccountProfileContextLeaseError(
            "account_profile_context_lease_token_invalid"
        ) from exc
    if str(parsed) != value:
        raise AccountProfileContextLeaseError(
            "account_profile_context_lease_token_invalid"
        )
    return str(parsed)


def _normalize_owner_id(owner_id: str | None) -> str:
    normalized = str(owner_id or WORKER_ID).strip()
    if not normalized or len(normalized) > 128:
        raise AccountProfileContextLeaseError(
            "account_profile_context_owner_id_invalid"
        )
    return normalized


async def _require_live_account(
    target_db,
    account_id: int,
    platform: str,
) -> None:
    account = await target_db.fetch_one(
        """SELECT platform, deleted_at
           FROM accounts
           WHERE id = :account_id
           FOR UPDATE""",
        {"account_id": account_id},
    )
    if not account:
        raise AccountProfileContextLeaseError(
            "account_profile_context_account_missing"
        )
    if (
        str(account["platform"] or "").strip().casefold()
        != platform
    ):
        raise AccountProfileContextLeaseError(
            "account_profile_context_account_platform_mismatch"
        )
    if account["deleted_at"] is not None:
        raise AccountProfileContextLeaseError(
            "account_profile_context_account_deleted"
        )


async def _require_one_affected(target_db, code: str) -> None:
    affected = await target_db.fetch_one(
        "SELECT ROW_COUNT() AS affected"
    )
    if affected is None or int(affected["affected"] or 0) != 1:
        raise AccountProfileContextLeaseError(code)


async def acquire_account_profile_context_lease(
    account_id: int,
    platform: str,
    *,
    current_lease_token: str | None = None,
    owner_id: str | None = None,
    db=None,
) -> str:
    """Acquire an absent/expired lease or renew the caller's exact lease."""

    normalized_account_id, normalized_platform = (
        normalize_account_profile_context_identity(
            account_id,
            platform,
        )
    )
    normalized_owner = _normalize_owner_id(owner_id)
    normalized_current_token = (
        normalize_profile_context_lease_token(current_lease_token)
        if current_lease_token is not None
        else None
    )
    target_db = db or database
    async with target_db.transaction():
        # Account first is the global lock order shared with soft-delete and
        # renewal. Once deleted_at is visible, no lease can be resurrected.
        await _require_live_account(
            target_db,
            normalized_account_id,
            normalized_platform,
        )
        current = await target_db.fetch_one(
            """SELECT platform, lease_token, owner_id,
                      (lease_expires_at > NOW(6)) AS lease_is_active
               FROM account_profile_context_leases
               WHERE account_id = :account_id
               FOR UPDATE""",
            {"account_id": normalized_account_id},
        )
        if current and int(current["lease_is_active"] or 0) == 1:
            exact_owner = (
                normalized_current_token is not None
                and str(current["lease_token"] or "").casefold()
                == normalized_current_token
                and str(current["owner_id"] or "") == normalized_owner
                and str(current["platform"] or "").casefold()
                == normalized_platform
            )
            if not exact_owner:
                raise AccountProfileContextLeaseError(
                    "account_profile_context_lease_busy"
                )
            await target_db.execute(
                """UPDATE account_profile_context_leases
                   SET renewed_at = NOW(6),
                       lease_expires_at = TIMESTAMPADD(
                         SECOND, :ttl_seconds, NOW(6)
                       ),
                       updated_at = NOW(6)
                   WHERE account_id = :account_id
                     AND platform = :platform
                     AND lease_token = :lease_token
                     AND owner_id = :owner_id
                     AND lease_expires_at > NOW(6)""",
                {
                    "account_id": normalized_account_id,
                    "platform": normalized_platform,
                    "lease_token": normalized_current_token,
                    "owner_id": normalized_owner,
                    "ttl_seconds": PROFILE_CONTEXT_LEASE_TTL_SECONDS,
                },
            )
            await _require_one_affected(
                target_db,
                "account_profile_context_lease_renew_lost",
            )
            return normalized_current_token

        new_token = str(uuid.uuid4())
        if current:
            await target_db.execute(
                """UPDATE account_profile_context_leases
                   SET platform = :platform,
                       lease_token = :lease_token,
                       owner_id = :owner_id,
                       acquired_at = NOW(6),
                       renewed_at = NOW(6),
                       lease_expires_at = TIMESTAMPADD(
                         SECOND, :ttl_seconds, NOW(6)
                       ),
                       updated_at = NOW(6)
                   WHERE account_id = :account_id
                     AND lease_expires_at <= NOW(6)""",
                {
                    "account_id": normalized_account_id,
                    "platform": normalized_platform,
                    "lease_token": new_token,
                    "owner_id": normalized_owner,
                    "ttl_seconds": PROFILE_CONTEXT_LEASE_TTL_SECONDS,
                },
            )
        else:
            await target_db.execute(
                """INSERT INTO account_profile_context_leases
                     (
                       account_id, platform, lease_token, owner_id,
                       acquired_at, renewed_at, lease_expires_at
                     )
                   VALUES
                     (
                       :account_id, :platform, :lease_token, :owner_id,
                       NOW(6), NOW(6),
                       TIMESTAMPADD(SECOND, :ttl_seconds, NOW(6))
                     )""",
                {
                    "account_id": normalized_account_id,
                    "platform": normalized_platform,
                    "lease_token": new_token,
                    "owner_id": normalized_owner,
                    "ttl_seconds": PROFILE_CONTEXT_LEASE_TTL_SECONDS,
                },
            )
        await _require_one_affected(
            target_db,
            "account_profile_context_lease_acquire_lost",
        )
        return new_token


async def renew_account_profile_context_lease(
    account_id: int,
    platform: str,
    lease_token: str,
    *,
    owner_id: str | None = None,
    db=None,
) -> None:
    """Renew only a still-live exact lease for a non-deleted account."""

    normalized_account_id, normalized_platform = (
        normalize_account_profile_context_identity(
            account_id,
            platform,
        )
    )
    normalized_token = normalize_profile_context_lease_token(lease_token)
    normalized_owner = _normalize_owner_id(owner_id)
    target_db = db or database
    async with target_db.transaction():
        await _require_live_account(
            target_db,
            normalized_account_id,
            normalized_platform,
        )
        await target_db.execute(
            """UPDATE account_profile_context_leases
               SET renewed_at = NOW(6),
                   lease_expires_at = TIMESTAMPADD(
                     SECOND, :ttl_seconds, NOW(6)
                   ),
                   updated_at = NOW(6)
               WHERE account_id = :account_id
                 AND platform = :platform
                 AND lease_token = :lease_token
                 AND owner_id = :owner_id
                 AND lease_expires_at > NOW(6)""",
            {
                "account_id": normalized_account_id,
                "platform": normalized_platform,
                "lease_token": normalized_token,
                "owner_id": normalized_owner,
                "ttl_seconds": PROFILE_CONTEXT_LEASE_TTL_SECONDS,
            },
        )
        await _require_one_affected(
            target_db,
            "account_profile_context_lease_renew_lost",
        )


async def release_account_profile_context_lease(
    account_id: int,
    platform: str,
    lease_token: str,
    *,
    owner_id: str | None = None,
    db=None,
) -> bool:
    """Release only the caller's exact token; missing/lost leases are safe."""

    normalized_account_id, normalized_platform = (
        normalize_account_profile_context_identity(
            account_id,
            platform,
        )
    )
    normalized_token = normalize_profile_context_lease_token(lease_token)
    normalized_owner = _normalize_owner_id(owner_id)
    target_db = db or database
    async with target_db.transaction():
        await target_db.execute(
            """DELETE FROM account_profile_context_leases
               WHERE account_id = :account_id
                 AND platform = :platform
                 AND lease_token = :lease_token
                 AND owner_id = :owner_id""",
            {
                "account_id": normalized_account_id,
                "platform": normalized_platform,
                "lease_token": normalized_token,
                "owner_id": normalized_owner,
            },
        )
        affected = await target_db.fetch_one(
            "SELECT ROW_COUNT() AS affected"
        )
    if affected is None:
        raise AccountProfileContextLeaseError(
            "account_profile_context_lease_release_row_count_unavailable"
        )
    count = int(affected["affected"] or 0)
    if count not in {0, 1}:
        raise AccountProfileContextLeaseError(
            "account_profile_context_lease_release_count_invalid"
        )
    return count == 1


async def assert_no_active_account_profile_context_lease(
    account_id: int,
    platform: str,
    *,
    db=None,
) -> None:
    """Apply the DB ownership gate before the final process-flock gate."""

    normalized_account_id, normalized_platform = (
        normalize_account_profile_context_identity(
            account_id,
            platform,
        )
    )
    target_db = db or database
    row = await target_db.fetch_one(
        """SELECT platform,
                  (lease_expires_at > NOW(6)) AS lease_is_active
           FROM account_profile_context_leases
           WHERE account_id = :account_id""",
        {"account_id": normalized_account_id},
    )
    if not row:
        return
    if (
        str(row["platform"] or "").strip().casefold()
        != normalized_platform
    ):
        raise AccountProfileContextLeaseError(
            "account_profile_context_lease_platform_mismatch"
        )
    if int(row["lease_is_active"] or 0) == 1:
        raise AccountProfileContextLeaseError(
            "account_profile_context_lease_active"
        )


__all__ = (
    "AccountProfileContextLeaseError",
    "PROFILE_CONTEXT_LEASE_TTL_SECONDS",
    "acquire_account_profile_context_lease",
    "assert_no_active_account_profile_context_lease",
    "normalize_account_profile_context_identity",
    "normalize_profile_context_lease_token",
    "release_account_profile_context_lease",
    "renew_account_profile_context_lease",
)
