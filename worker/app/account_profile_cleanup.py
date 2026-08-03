"""Retryable, platform-scoped cleanup of deleted account browser profiles."""

from __future__ import annotations

import asyncio
import errno
import os
from pathlib import Path
import stat
import uuid

from app.db import database
from app.account_profile_context_lease import (
    assert_no_active_account_profile_context_lease,
)
from app.account_profile_lock import acquire_account_profile_lock
from app.utils.log import structured_log
from app.worker_identity import WORKER_ID
from shared.platform_ids import PLATFORM_IDS


PROFILES_ROOT = Path("/profiles")
PROFILE_CLEANUP_POLL_SECONDS = max(
    float(os.getenv("ACCOUNT_PROFILE_CLEANUP_POLL_SECONDS", "5")),
    0.1,
)
PROFILE_CLEANUP_STALE_SECONDS = max(
    int(os.getenv("ACCOUNT_PROFILE_CLEANUP_STALE_SECONDS", "900")),
    60,
)
PROFILE_CLEANUP_MAX_RETRY_SECONDS = max(
    int(os.getenv("ACCOUNT_PROFILE_CLEANUP_MAX_RETRY_SECONDS", "300")),
    5,
)
MAX_ATTEMPTS_VALUE = 2_147_483_647


class AccountProfileCleanupError(RuntimeError):
    def __init__(self, code: str):
        self.code = str(code or "account_profile_cleanup_failed")[:128]
        super().__init__(self.code)


def normalize_cleanup_identity(
    account_id: int,
    platform: str,
) -> tuple[int, str]:
    if isinstance(account_id, bool):
        raise AccountProfileCleanupError(
            "account_profile_cleanup_account_id_invalid"
        )
    try:
        normalized_account_id = int(account_id)
    except (TypeError, ValueError) as exc:
        raise AccountProfileCleanupError(
            "account_profile_cleanup_account_id_invalid"
        ) from exc
    normalized_platform = str(platform or "").strip().casefold()
    if normalized_account_id <= 0:
        raise AccountProfileCleanupError(
            "account_profile_cleanup_account_id_invalid"
        )
    if normalized_platform not in PLATFORM_IDS:
        raise AccountProfileCleanupError(
            "account_profile_cleanup_platform_invalid"
        )
    return normalized_account_id, normalized_platform


def account_profile_path(
    account_id: int,
    platform: str,
    *,
    profiles_root: Path | None = None,
) -> Path:
    normalized_account_id, normalized_platform = normalize_cleanup_identity(
        account_id,
        platform,
    )
    root = Path(profiles_root or PROFILES_ROOT)
    if not root.is_absolute():
        raise AccountProfileCleanupError(
            "account_profile_cleanup_root_not_absolute"
        )
    return (
        root
        / normalized_platform
        / f"account_{normalized_account_id}"
    )


def _directory_open_flags() -> int:
    required = ("O_DIRECTORY", "O_NOFOLLOW")
    if os.name != "posix" or any(
        not hasattr(os, name) for name in required
    ):
        raise AccountProfileCleanupError(
            "account_profile_cleanup_secure_delete_unsupported"
        )
    return (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )


def _same_inode(left, right) -> bool:
    return (
        int(left.st_dev) == int(right.st_dev)
        and int(left.st_ino) == int(right.st_ino)
    )


def _lstat_at(parent_fd: int, name: str):
    return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)


def _open_verified_directory(
    parent_fd: int,
    name: str,
    flags: int,
    *,
    error_code: str,
) -> tuple[int, object]:
    before = _lstat_at(parent_fd, name)
    if stat.S_ISLNK(before.st_mode):
        raise AccountProfileCleanupError(error_code)
    if not stat.S_ISDIR(before.st_mode):
        raise AccountProfileCleanupError(error_code)
    try:
        directory_fd = os.open(
            name,
            flags,
            dir_fd=parent_fd,
        )
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise AccountProfileCleanupError(error_code) from exc
        raise
    opened = os.fstat(directory_fd)
    if not _same_inode(before, opened):
        os.close(directory_fd)
        raise AccountProfileCleanupError(error_code)
    return directory_fd, opened


def _securely_empty_directory(directory_fd: int, flags: int) -> None:
    for name in os.listdir(directory_fd):
        try:
            metadata = _lstat_at(directory_fd, name)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode):
            os.unlink(name, dir_fd=directory_fd)
            continue
        if not stat.S_ISDIR(metadata.st_mode):
            os.unlink(name, dir_fd=directory_fd)
            continue

        child_fd, opened = _open_verified_directory(
            directory_fd,
            name,
            flags,
            error_code="account_profile_cleanup_nested_symlink_race",
        )
        try:
            _securely_empty_directory(child_fd, flags)
        finally:
            os.close(child_fd)
        try:
            current = _lstat_at(directory_fd, name)
        except FileNotFoundError:
            continue
        if not _same_inode(opened, current):
            raise AccountProfileCleanupError(
                "account_profile_cleanup_nested_identity_changed"
            )
        os.rmdir(name, dir_fd=directory_fd)


def securely_delete_account_profile(
    account_id: int,
    platform: str,
    *,
    profiles_root: Path | None = None,
) -> bool:
    """Delete only the fixed account directory without following symlinks.

    Returns ``True`` when a directory was removed and ``False`` when the exact
    root/platform/account path was already absent. Any symlink at the trusted
    root, platform, or account boundary is refused rather than followed.
    """

    target = account_profile_path(
        account_id,
        platform,
        profiles_root=profiles_root,
    )
    root = target.parents[1]
    platform_name = target.parent.name
    account_name = target.name
    flags = _directory_open_flags()

    try:
        root_fd = os.open(root, flags)
    except FileNotFoundError:
        return False
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise AccountProfileCleanupError(
                "account_profile_cleanup_root_unsafe"
            ) from exc
        raise

    platform_fd = None
    account_fd = None
    try:
        try:
            platform_fd, _ = _open_verified_directory(
                root_fd,
                platform_name,
                flags,
                error_code="account_profile_cleanup_platform_path_unsafe",
            )
        except FileNotFoundError:
            return False
        try:
            account_fd, opened = _open_verified_directory(
                platform_fd,
                account_name,
                flags,
                error_code="account_profile_cleanup_account_path_unsafe",
            )
        except FileNotFoundError:
            return False

        _securely_empty_directory(account_fd, flags)
        os.close(account_fd)
        account_fd = None
        try:
            current = _lstat_at(platform_fd, account_name)
        except FileNotFoundError:
            return True
        if not _same_inode(opened, current):
            raise AccountProfileCleanupError(
                "account_profile_cleanup_account_identity_changed"
            )
        os.rmdir(account_name, dir_fd=platform_fd)
        return True
    finally:
        if account_fd is not None:
            os.close(account_fd)
        if platform_fd is not None:
            os.close(platform_fd)
        os.close(root_fd)


def securely_delete_account_profile_with_lock(
    account_id: int,
    platform: str,
    *,
    profiles_root: Path | None = None,
) -> bool:
    """Hold the process fence until synchronous deletion fully finishes.

    This composite operation deliberately runs in one ``to_thread`` call.
    Cancelling the asyncio caller cannot release the flock while its deletion
    thread is still walking the Chromium profile.
    """

    root = Path(profiles_root or PROFILES_ROOT)
    profile_lock = acquire_account_profile_lock(
        account_id,
        platform,
        profiles_root=root,
    )
    try:
        removed = securely_delete_account_profile(
            account_id,
            platform,
            profiles_root=root,
        )
        profile_lock.unlink()
        return removed
    finally:
        profile_lock.release()


def _retry_seconds(attempts: int) -> int:
    exponent = min(max(int(attempts), 1), 8)
    return min(2 ** exponent, PROFILE_CLEANUP_MAX_RETRY_SECONDS)


def _safe_error_code(exc: BaseException) -> str:
    explicit_code = getattr(exc, "code", None)
    if explicit_code:
        return str(explicit_code)[:128]
    if isinstance(exc, OSError):
        errno_value = getattr(exc, "errno", None)
        if isinstance(errno_value, int):
            return f"account_profile_cleanup_oserror_{errno_value}"[:128]
    return (
        f"account_profile_cleanup_{type(exc).__name__ or 'error'}"
    )[:128]


async def claim_account_profile_cleanup(
    platform: str,
) -> dict | None:
    _, normalized_platform = normalize_cleanup_identity(1, platform)
    claim_token = str(uuid.uuid4())
    async with database.transaction():
        row = await database.fetch_one(
            """SELECT id, account_id, platform, status, attempts
               FROM account_profile_cleanup_intents
               WHERE platform = :platform
                 AND (
                   (
                     status = 'pending'
                     AND next_attempt_at <= NOW()
                   )
                   OR (
                     status = 'running'
                     AND claimed_at < TIMESTAMPADD(
                           SECOND, :stale_offset_seconds, NOW()
                         )
                   )
                 )
               ORDER BY
                 CASE status WHEN 'pending' THEN 0 ELSE 1 END,
                 next_attempt_at,
                 id
               LIMIT 1
               FOR UPDATE SKIP LOCKED""",
            {
                "platform": normalized_platform,
                "stale_offset_seconds": -PROFILE_CLEANUP_STALE_SECONDS,
            },
        )
        if not row:
            return None
        await database.execute(
            """UPDATE account_profile_cleanup_intents
               SET status = 'running',
                   attempts = LEAST(attempts + 1, :max_attempts),
                   claim_token = :claim_token,
                   worker_id = :worker_id,
                   claimed_at = NOW(),
                   completed_at = NULL,
                   updated_at = NOW()
               WHERE id = :id
                 AND platform = :platform
                 AND status = :previous_status""",
            {
                "id": row["id"],
                "platform": normalized_platform,
                "previous_status": row["status"],
                "claim_token": claim_token,
                "worker_id": WORKER_ID,
                "max_attempts": MAX_ATTEMPTS_VALUE,
            },
        )
        affected = await database.fetch_one(
            "SELECT ROW_COUNT() AS affected"
        )
        if affected is None or int(affected["affected"] or 0) != 1:
            raise AccountProfileCleanupError(
                "account_profile_cleanup_claim_lost"
            )
        claimed = dict(row)
        claimed["status"] = "running"
        claimed["attempts"] = min(
            int(row["attempts"] or 0) + 1,
            MAX_ATTEMPTS_VALUE,
        )
        claimed["claim_token"] = claim_token
        claimed["worker_id"] = WORKER_ID
        return claimed


async def _assert_deleted_account_binding(intent: dict) -> None:
    row = await database.fetch_one(
        """SELECT platform, deleted_at, encrypted_credential
           FROM accounts
           WHERE id = :account_id""",
        {"account_id": int(intent["account_id"])},
    )
    if not row:
        raise AccountProfileCleanupError(
            "account_profile_cleanup_account_missing"
        )
    if (
        str(row["platform"] or "").strip().casefold()
        != str(intent["platform"] or "").strip().casefold()
    ):
        raise AccountProfileCleanupError(
            "account_profile_cleanup_account_platform_mismatch"
        )
    if row["deleted_at"] is None:
        raise AccountProfileCleanupError(
            "account_profile_cleanup_account_not_deleted"
        )
    credential = row["encrypted_credential"]
    if isinstance(credential, memoryview):
        credential = credential.tobytes()
    if not (
        credential is None
        or credential == ""
        or credential == b""
    ):
        raise AccountProfileCleanupError(
            "account_profile_cleanup_database_credential_present"
        )


async def mark_account_profile_cleanup_succeeded(intent: dict) -> None:
    async with database.transaction():
        await database.execute(
            """UPDATE account_profile_cleanup_intents
               SET status = 'succeeded',
                   claim_token = NULL,
                   worker_id = NULL,
                   claimed_at = NULL,
                   next_attempt_at = NOW(),
                   completed_at = NOW(),
                   last_error_code = NULL,
                   updated_at = NOW()
               WHERE id = :id
                 AND platform = :platform
                 AND status = 'running'
                 AND claim_token = :claim_token
                 AND worker_id = :worker_id""",
            {
                "id": intent["id"],
                "platform": intent["platform"],
                "claim_token": intent["claim_token"],
                "worker_id": intent["worker_id"],
            },
        )
        affected = await database.fetch_one(
            "SELECT ROW_COUNT() AS affected"
        )
        if affected is None or int(affected["affected"] or 0) != 1:
            raise AccountProfileCleanupError(
                "account_profile_cleanup_completion_claim_lost"
            )


async def release_account_profile_cleanup_for_retry(
    intent: dict,
    *,
    error_code: str,
) -> bool:
    retry_seconds = _retry_seconds(int(intent.get("attempts") or 1))
    async with database.transaction():
        await database.execute(
            """UPDATE account_profile_cleanup_intents
               SET status = 'pending',
                   claim_token = NULL,
                   worker_id = NULL,
                   claimed_at = NULL,
                   next_attempt_at = TIMESTAMPADD(
                     SECOND, :retry_seconds, NOW()
                   ),
                   completed_at = NULL,
                   last_error_code = :error_code,
                   updated_at = NOW()
               WHERE id = :id
                 AND platform = :platform
                 AND status = 'running'
                 AND claim_token = :claim_token
                 AND worker_id = :worker_id""",
            {
                "id": intent["id"],
                "platform": intent["platform"],
                "claim_token": intent["claim_token"],
                "worker_id": intent["worker_id"],
                "retry_seconds": retry_seconds,
                "error_code": str(error_code or "")[:128],
            },
        )
        affected = await database.fetch_one(
            "SELECT ROW_COUNT() AS affected"
        )
    if affected is None:
        raise AccountProfileCleanupError(
            "account_profile_cleanup_retry_row_count_unavailable"
        )
    return int(affected["affected"] or 0) == 1


async def process_account_profile_cleanup(
    pool,
    intent: dict,
) -> None:
    account_id, platform = normalize_cleanup_identity(
        intent["account_id"],
        intent["platform"],
    )
    await _assert_deleted_account_binding(intent)
    closed = await pool.close_account_context(
        account_id,
        reason="account_profile_cleanup",
    )
    if closed is not True:
        raise AccountProfileCleanupError(
            "account_profile_cleanup_context_close_unconfirmed"
        )
    # A sibling platform Worker replica may own the same persistent profile.
    # Account deletion prevents any future acquire/renew, but an already-issued
    # lease remains authoritative until that replica releases it or its bounded
    # TTL expires.
    await assert_no_active_account_profile_context_lease(
        account_id,
        platform,
    )
    # TTL expiry only revokes database ownership; it cannot prove that the
    # previous Chromium process stopped. The process-lifetime flock is the
    # final deletion fence and is held through both deletion and settlement.
    removed = await asyncio.to_thread(
        securely_delete_account_profile_with_lock,
        account_id,
        platform,
        profiles_root=PROFILES_ROOT,
    )
    await mark_account_profile_cleanup_succeeded(intent)
    structured_log(
        "info",
        "account_profile_cleanup_succeeded",
        account_id=account_id,
        platform=platform,
        profile_removed=bool(removed),
        attempts=int(intent["attempts"]),
    )


async def _wait_for_poll(
    shutdown_event: asyncio.Event,
    timeout: float = PROFILE_CLEANUP_POLL_SECONDS,
) -> None:
    try:
        await asyncio.wait_for(
            shutdown_event.wait(),
            timeout=max(float(timeout), 0.1),
        )
    except asyncio.TimeoutError:
        pass


async def _account_profile_cleanup_platform_loop(
    pool,
    shutdown_event: asyncio.Event,
    platform: str,
) -> None:
    while not shutdown_event.is_set():
        intent = None
        try:
            intent = await claim_account_profile_cleanup(platform)
            if intent is None:
                await _wait_for_poll(shutdown_event)
                continue
            await process_account_profile_cleanup(pool, intent)
        except asyncio.CancelledError:
            if intent is not None:
                try:
                    await asyncio.shield(
                        release_account_profile_cleanup_for_retry(
                            intent,
                            error_code=(
                                "account_profile_cleanup_worker_cancelled"
                            ),
                        )
                    )
                except Exception as retry_exc:
                    structured_log(
                        "error",
                        "account_profile_cleanup_cancel_release_failed",
                        platform=platform,
                        exception=retry_exc,
                    )
            raise
        except Exception as exc:
            if intent is not None:
                try:
                    await release_account_profile_cleanup_for_retry(
                        intent,
                        error_code=_safe_error_code(exc),
                    )
                except Exception as retry_exc:
                    structured_log(
                        "error",
                        "account_profile_cleanup_retry_release_failed",
                        platform=platform,
                        exception=retry_exc,
                    )
            structured_log(
                "error",
                "account_profile_cleanup_failed",
                platform=platform,
                account_id=(
                    int(intent["account_id"])
                    if intent is not None
                    else None
                ),
                error_code=_safe_error_code(exc),
            )
            await _wait_for_poll(shutdown_event)


async def account_profile_cleanup_loop(
    pool,
    shutdown_event: asyncio.Event,
    *,
    platforms,
) -> None:
    selected = tuple(
        dict.fromkeys(
            str(platform or "").strip().casefold()
            for platform in (platforms or ())
        )
    )
    if not selected or any(
        platform not in PLATFORM_IDS for platform in selected
    ):
        raise AccountProfileCleanupError(
            "account_profile_cleanup_platform_scope_invalid"
        )
    lane_tasks = tuple(
        asyncio.create_task(
            _account_profile_cleanup_platform_loop(
                pool,
                shutdown_event,
                platform,
            ),
            name=f"account-profile-cleanup:{platform}",
        )
        for platform in selected
    )
    try:
        await asyncio.gather(*lane_tasks)
    finally:
        for task in lane_tasks:
            task.cancel()
        await asyncio.gather(*lane_tasks, return_exceptions=True)


__all__ = (
    "AccountProfileCleanupError",
    "account_profile_cleanup_loop",
    "account_profile_path",
    "claim_account_profile_cleanup",
    "process_account_profile_cleanup",
    "release_account_profile_cleanup_for_retry",
    "securely_delete_account_profile",
    "securely_delete_account_profile_with_lock",
)
