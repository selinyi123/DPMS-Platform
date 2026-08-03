"""Durable cleanup of terminal browser-login profiles and QR screenshots."""

from __future__ import annotations

import asyncio
import errno
import os
from pathlib import Path
import stat
import uuid

from app.account_profile_cleanup import (
    MAX_ATTEMPTS_VALUE,
    PROFILE_CLEANUP_MAX_RETRY_SECONDS,
    PROFILE_CLEANUP_POLL_SECONDS,
    PROFILE_CLEANUP_STALE_SECONDS,
    _directory_open_flags,
    _lstat_at,
    _open_verified_directory,
    _same_inode,
    _securely_empty_directory,
)
from app.db import database
from app.utils.log import structured_log
from app.worker_identity import WORKER_ID


LOGIN_PROFILE_ROOT = Path("/profiles/login-sessions")
LOGIN_TERMINAL_STATUSES = frozenset(
    {"confirmed", "failed", "expired"}
)


class LoginProfileCleanupError(RuntimeError):
    def __init__(self, code: str):
        self.code = str(code or "login_profile_cleanup_failed")[:128]
        super().__init__(self.code)


def normalize_login_session_id(session_id: str) -> str:
    value = str(session_id or "").strip()
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, TypeError, ValueError) as exc:
        raise LoginProfileCleanupError(
            "login_profile_cleanup_session_id_invalid"
        ) from exc
    if str(parsed) != value:
        raise LoginProfileCleanupError(
            "login_profile_cleanup_session_id_invalid"
        )
    return str(parsed)


def login_profile_paths(
    session_id: str,
    *,
    profile_root: Path | None = None,
) -> tuple[Path, Path]:
    normalized = normalize_login_session_id(session_id)
    root = Path(profile_root or LOGIN_PROFILE_ROOT)
    if not root.is_absolute():
        raise LoginProfileCleanupError(
            "login_profile_cleanup_root_not_absolute"
        )
    return root / normalized / "profile", root / f"{normalized}.png"


def securely_delete_login_profile(
    session_id: str,
    *,
    profile_root: Path | None = None,
) -> tuple[bool, bool]:
    """Delete only the canonical UUID directory and sibling PNG.

    Every boundary is opened relative to an ``O_NOFOLLOW`` directory fd.
    Missing paths are successful idempotent results; symlink or non-regular
    boundary substitutions are refused.
    """

    profile_path, image_path = login_profile_paths(
        session_id,
        profile_root=profile_root,
    )
    root = profile_path.parents[1]
    session_name = profile_path.parent.name
    image_name = image_path.name
    flags = _directory_open_flags()
    try:
        root_fd = os.open(root, flags)
    except FileNotFoundError:
        return False, False
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise LoginProfileCleanupError(
                "login_profile_cleanup_root_unsafe"
            ) from exc
        raise

    profile_removed = False
    image_removed = False
    session_fd = None
    profile_fd = None
    try:
        # Preflight the sibling image before mutating either target. A symlink
        # or device node is an integrity violation, not a file to follow.
        try:
            image_before = _lstat_at(root_fd, image_name)
        except FileNotFoundError:
            image_before = None
        if image_before is not None and (
            stat.S_ISLNK(image_before.st_mode)
            or not stat.S_ISREG(image_before.st_mode)
        ):
            raise LoginProfileCleanupError(
                "login_profile_cleanup_image_path_unsafe"
            )

        try:
            session_fd, session_opened = _open_verified_directory(
                root_fd,
                session_name,
                flags,
                error_code="login_profile_cleanup_session_path_unsafe",
            )
        except FileNotFoundError:
            session_opened = None
        if session_fd is not None:
            try:
                profile_fd, profile_opened = _open_verified_directory(
                    session_fd,
                    "profile",
                    flags,
                    error_code=(
                        "login_profile_cleanup_profile_path_unsafe"
                    ),
                )
            except FileNotFoundError:
                profile_opened = None
            if profile_fd is not None:
                _securely_empty_directory(profile_fd, flags)
                os.close(profile_fd)
                profile_fd = None
                try:
                    profile_current = _lstat_at(
                        session_fd,
                        "profile",
                    )
                except FileNotFoundError:
                    profile_current = None
                if profile_current is not None:
                    if not _same_inode(
                        profile_opened,
                        profile_current,
                    ):
                        raise LoginProfileCleanupError(
                            "login_profile_cleanup_"
                            "profile_identity_changed"
                        )
                    os.rmdir("profile", dir_fd=session_fd)
                profile_removed = True
            os.close(session_fd)
            session_fd = None
            try:
                session_current = _lstat_at(root_fd, session_name)
            except FileNotFoundError:
                session_current = None
            if session_current is not None:
                if not _same_inode(session_opened, session_current):
                    raise LoginProfileCleanupError(
                        "login_profile_cleanup_session_identity_changed"
                    )
                try:
                    os.rmdir(session_name, dir_fd=root_fd)
                except OSError as exc:
                    if exc.errno not in {
                        errno.ENOTEMPTY,
                        errno.EEXIST,
                    }:
                        raise

        if image_before is not None:
            try:
                image_current = _lstat_at(root_fd, image_name)
            except FileNotFoundError:
                image_current = None
            if image_current is not None:
                if (
                    stat.S_ISLNK(image_current.st_mode)
                    or not stat.S_ISREG(image_current.st_mode)
                    or not _same_inode(image_before, image_current)
                ):
                    raise LoginProfileCleanupError(
                        "login_profile_cleanup_image_identity_changed"
                    )
                os.unlink(image_name, dir_fd=root_fd)
            image_removed = True
        return profile_removed, image_removed
    finally:
        if profile_fd is not None:
            os.close(profile_fd)
        if session_fd is not None:
            os.close(session_fd)
        os.close(root_fd)


async def enqueue_login_profile_cleanup(
    session_id: str,
    *,
    db=None,
) -> None:
    normalized = normalize_login_session_id(session_id)
    target_db = db or database
    await target_db.execute(
        """INSERT INTO login_profile_cleanup_intents
             (session_id, status, next_attempt_at)
           VALUES
             (:session_id, 'pending', NOW())
           ON DUPLICATE KEY UPDATE
             session_id = login_profile_cleanup_intents.session_id""",
        {"session_id": normalized},
    )


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
            return f"login_profile_cleanup_oserror_{errno_value}"[:128]
    return (
        f"login_profile_cleanup_{type(exc).__name__ or 'error'}"
    )[:128]


async def claim_login_profile_cleanup(
    session_id: str | None = None,
) -> dict | None:
    normalized_session_id = (
        normalize_login_session_id(session_id)
        if session_id is not None
        else None
    )
    claim_token = str(uuid.uuid4())
    exact_filter = (
        "AND session_id = :session_id"
        if normalized_session_id is not None
        else ""
    )
    values = {
        "stale_offset_seconds": -PROFILE_CLEANUP_STALE_SECONDS,
    }
    if normalized_session_id is not None:
        values["session_id"] = normalized_session_id
    async with database.transaction():
        row = await database.fetch_one(
            f"""SELECT id, session_id, status, attempts
                FROM login_profile_cleanup_intents
                WHERE (
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
                {exact_filter}
                ORDER BY
                  CASE status WHEN 'pending' THEN 0 ELSE 1 END,
                  next_attempt_at,
                  id
                LIMIT 1
                FOR UPDATE SKIP LOCKED""",
            values,
        )
        if not row:
            return None
        await database.execute(
            """UPDATE login_profile_cleanup_intents
               SET status = 'running',
                   attempts = LEAST(attempts + 1, :max_attempts),
                   claim_token = :claim_token,
                   worker_id = :worker_id,
                   claimed_at = NOW(),
                   completed_at = NULL,
                   updated_at = NOW()
               WHERE id = :id
                 AND session_id = :session_id
                 AND status = :previous_status""",
            {
                "id": row["id"],
                "session_id": row["session_id"],
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
            raise LoginProfileCleanupError(
                "login_profile_cleanup_claim_lost"
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


async def _assert_terminal_login_session(intent: dict) -> None:
    row = await database.fetch_one(
        """SELECT status
           FROM login_sessions
           WHERE session_id = :session_id""",
        {"session_id": intent["session_id"]},
    )
    if not row:
        raise LoginProfileCleanupError(
            "login_profile_cleanup_session_missing"
        )
    if str(row["status"] or "").strip().casefold() not in (
        LOGIN_TERMINAL_STATUSES
    ):
        raise LoginProfileCleanupError(
            "login_profile_cleanup_session_not_terminal"
        )


async def mark_login_profile_cleanup_succeeded(intent: dict) -> None:
    async with database.transaction():
        await database.execute(
            """UPDATE login_profile_cleanup_intents
               SET status = 'succeeded',
                   claim_token = NULL,
                   worker_id = NULL,
                   claimed_at = NULL,
                   next_attempt_at = NOW(),
                   completed_at = NOW(),
                   last_error_code = NULL,
                   updated_at = NOW()
               WHERE id = :id
                 AND session_id = :session_id
                 AND status = 'running'
                 AND claim_token = :claim_token
                 AND worker_id = :worker_id""",
            {
                "id": intent["id"],
                "session_id": intent["session_id"],
                "claim_token": intent["claim_token"],
                "worker_id": intent["worker_id"],
            },
        )
        affected = await database.fetch_one(
            "SELECT ROW_COUNT() AS affected"
        )
        if affected is None or int(affected["affected"] or 0) != 1:
            raise LoginProfileCleanupError(
                "login_profile_cleanup_completion_claim_lost"
            )


async def release_login_profile_cleanup_for_retry(
    intent: dict,
    *,
    error_code: str,
) -> bool:
    async with database.transaction():
        await database.execute(
            """UPDATE login_profile_cleanup_intents
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
                 AND session_id = :session_id
                 AND status = 'running'
                 AND claim_token = :claim_token
                 AND worker_id = :worker_id""",
            {
                "id": intent["id"],
                "session_id": intent["session_id"],
                "claim_token": intent["claim_token"],
                "worker_id": intent["worker_id"],
                "retry_seconds": _retry_seconds(
                    int(intent.get("attempts") or 1)
                ),
                "error_code": str(error_code or "")[:128],
            },
        )
        affected = await database.fetch_one(
            "SELECT ROW_COUNT() AS affected"
        )
    if affected is None:
        raise LoginProfileCleanupError(
            "login_profile_cleanup_retry_row_count_unavailable"
        )
    return int(affected["affected"] or 0) == 1


async def process_login_profile_cleanup(pool, intent: dict) -> None:
    session_id = normalize_login_session_id(intent["session_id"])
    await _assert_terminal_login_session(intent)
    profile_path, _ = login_profile_paths(session_id)
    close_profiles = getattr(
        pool,
        "close_transient_contexts_for_profile",
        None,
    )
    if close_profiles is None:
        raise LoginProfileCleanupError(
            "login_profile_cleanup_context_close_unsupported"
        )
    closed = await close_profiles(
        str(profile_path),
        reason="login_profile_cleanup",
    )
    if closed is not True:
        raise LoginProfileCleanupError(
            "login_profile_cleanup_context_close_unconfirmed"
        )
    profile_removed, image_removed = await asyncio.to_thread(
        securely_delete_login_profile,
        session_id,
    )
    await mark_login_profile_cleanup_succeeded(intent)
    structured_log(
        "info",
        "login_profile_cleanup_succeeded",
        session_id=session_id,
        profile_directory_removed=profile_removed,
        image_removed=image_removed,
        attempts=int(intent["attempts"]),
    )


async def ensure_login_profile_cleanup_completed(
    pool,
    session_id: str,
) -> bool:
    """Synchronously close/delete a terminal session before stream ACK."""

    normalized = normalize_login_session_id(session_id)
    row = await database.fetch_one(
        """SELECT session.status AS session_status,
                  cleanup.status AS cleanup_status
           FROM login_sessions AS session
           LEFT JOIN login_profile_cleanup_intents AS cleanup
             ON cleanup.session_id = session.session_id
           WHERE session.session_id = :session_id""",
        {"session_id": normalized},
    )
    if not row:
        return True
    session_status = str(
        row["session_status"] or ""
    ).strip().casefold()
    if session_status not in LOGIN_TERMINAL_STATUSES:
        return True
    cleanup_status = str(
        row["cleanup_status"] or ""
    ).strip().casefold()
    if not cleanup_status:
        raise LoginProfileCleanupError(
            "login_profile_cleanup_intent_missing"
        )
    if cleanup_status == "succeeded":
        return True
    intent = await claim_login_profile_cleanup(normalized)
    if intent is None:
        return False
    try:
        await process_login_profile_cleanup(pool, intent)
        return True
    except BaseException as exc:
        try:
            await asyncio.shield(
                release_login_profile_cleanup_for_retry(
                    intent,
                    error_code=_safe_error_code(exc),
                )
            )
        except Exception as retry_exc:
            structured_log(
                "error",
                "login_profile_cleanup_retry_release_failed",
                session_id=normalized,
                exception=retry_exc,
            )
        raise


async def _wait_for_poll(shutdown_event: asyncio.Event) -> None:
    try:
        await asyncio.wait_for(
            shutdown_event.wait(),
            timeout=PROFILE_CLEANUP_POLL_SECONDS,
        )
    except asyncio.TimeoutError:
        pass


async def login_profile_cleanup_loop(
    pool,
    shutdown_event: asyncio.Event,
) -> None:
    while not shutdown_event.is_set():
        intent = None
        try:
            intent = await claim_login_profile_cleanup()
            if intent is None:
                await _wait_for_poll(shutdown_event)
                continue
            await process_login_profile_cleanup(pool, intent)
        except asyncio.CancelledError:
            if intent is not None:
                try:
                    await asyncio.shield(
                        release_login_profile_cleanup_for_retry(
                            intent,
                            error_code=(
                                "login_profile_cleanup_worker_cancelled"
                            ),
                        )
                    )
                except Exception as retry_exc:
                    structured_log(
                        "error",
                        "login_profile_cleanup_cancel_release_failed",
                        session_id=intent["session_id"],
                        exception=retry_exc,
                    )
            raise
        except Exception as exc:
            if intent is not None:
                try:
                    await release_login_profile_cleanup_for_retry(
                        intent,
                        error_code=_safe_error_code(exc),
                    )
                except Exception as retry_exc:
                    structured_log(
                        "error",
                        "login_profile_cleanup_retry_release_failed",
                        session_id=intent["session_id"],
                        exception=retry_exc,
                    )
            structured_log(
                "error",
                "login_profile_cleanup_failed",
                session_id=(
                    intent["session_id"] if intent is not None else None
                ),
                error_code=_safe_error_code(exc),
            )
            await _wait_for_poll(shutdown_event)


__all__ = (
    "LOGIN_PROFILE_ROOT",
    "LoginProfileCleanupError",
    "claim_login_profile_cleanup",
    "enqueue_login_profile_cleanup",
    "ensure_login_profile_cleanup_completed",
    "login_profile_cleanup_loop",
    "login_profile_paths",
    "normalize_login_session_id",
    "process_login_profile_cleanup",
    "release_login_profile_cleanup_for_retry",
    "securely_delete_login_profile",
)
