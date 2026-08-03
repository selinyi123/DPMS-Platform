"""Process-lifetime fence for one persistent Chromium profile directory."""

from __future__ import annotations

from dataclasses import dataclass
import errno
import os
from pathlib import Path
import stat

try:
    import fcntl
except ImportError:  # pragma: no cover - production Worker is Linux.
    fcntl = None

from app.account_profile_context_lease import (
    normalize_account_profile_context_identity,
)


class AccountProfileLockError(RuntimeError):
    def __init__(self, code: str):
        self.code = str(code or "account_profile_lock_failed")[:128]
        super().__init__(self.code)


def _directory_open_flags() -> int:
    if (
        os.name != "posix"
        or fcntl is None
        or not hasattr(os, "O_DIRECTORY")
        or not hasattr(os, "O_NOFOLLOW")
    ):
        raise AccountProfileLockError(
            "account_profile_lock_unsupported"
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


@dataclass
class AccountProfileLock:
    account_id: int
    platform: str
    root_fd: int
    platform_fd: int
    lock_fd: int
    lock_name: str
    lock_identity: object
    released: bool = False
    unlinked: bool = False

    def unlink(self) -> None:
        """Remove the exact lock inode while this owner still holds it."""

        if self.released:
            raise AccountProfileLockError(
                "account_profile_lock_already_released"
            )
        if self.unlinked:
            return
        try:
            current = os.stat(
                self.lock_name,
                dir_fd=self.platform_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            self.unlinked = True
            return
        if (
            stat.S_ISLNK(current.st_mode)
            or not stat.S_ISREG(current.st_mode)
            or int(current.st_nlink) != 1
            or not _same_inode(self.lock_identity, current)
        ):
            raise AccountProfileLockError(
                "account_profile_lock_identity_changed"
            )
        os.unlink(self.lock_name, dir_fd=self.platform_fd)
        self.unlinked = True

    def release(self) -> None:
        if self.released:
            return
        self.released = True
        try:
            if fcntl is not None:
                fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(self.lock_fd)
            os.close(self.platform_fd)
            os.close(self.root_fd)

    def __enter__(self) -> "AccountProfileLock":
        return self

    def __exit__(self, *_args) -> None:
        self.release()


def account_profile_lock_path(
    account_id: int,
    platform: str,
    *,
    profiles_root: Path | None = None,
) -> Path:
    normalized_account_id, normalized_platform = (
        normalize_account_profile_context_identity(
            account_id,
            platform,
        )
    )
    root = Path(profiles_root or "/profiles")
    if not root.is_absolute():
        raise AccountProfileLockError(
            "account_profile_lock_root_not_absolute"
        )
    return (
        root
        / normalized_platform
        / f".account_{normalized_account_id}.profile.lock"
    )


def acquire_account_profile_lock(
    account_id: int,
    platform: str,
    *,
    profiles_root: Path | None = None,
) -> AccountProfileLock:
    """Acquire one non-blocking exclusive lock for the profile lifetime."""

    lock_path = account_profile_lock_path(
        account_id,
        platform,
        profiles_root=profiles_root,
    )
    root = lock_path.parents[1]
    platform_name = lock_path.parent.name
    flags = _directory_open_flags()
    try:
        root_fd = os.open(root, flags)
    except FileNotFoundError as exc:
        raise AccountProfileLockError(
            "account_profile_lock_root_missing"
        ) from exc
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise AccountProfileLockError(
                "account_profile_lock_root_unsafe"
            ) from exc
        raise

    platform_fd = None
    lock_fd = None
    try:
        try:
            os.mkdir(
                platform_name,
                mode=0o700,
                dir_fd=root_fd,
            )
        except FileExistsError:
            pass
        try:
            platform_fd = os.open(
                platform_name,
                flags,
                dir_fd=root_fd,
            )
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise AccountProfileLockError(
                    "account_profile_lock_platform_unsafe"
                ) from exc
            raise

        lock_flags = (
            os.O_RDWR
            | os.O_CREAT
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0)
        )
        try:
            lock_fd = os.open(
                lock_path.name,
                lock_flags,
                0o600,
                dir_fd=platform_fd,
            )
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise AccountProfileLockError(
                    "account_profile_lock_path_unsafe"
                ) from exc
            raise
        opened = os.fstat(lock_fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or int(opened.st_nlink) != 1
        ):
            raise AccountProfileLockError(
                "account_profile_lock_path_unsafe"
            )
        try:
            fcntl.flock(
                lock_fd,
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                raise AccountProfileLockError(
                    "account_profile_lock_busy"
                ) from exc
            raise
        return AccountProfileLock(
            account_id=int(account_id),
            platform=platform_name,
            root_fd=root_fd,
            platform_fd=platform_fd,
            lock_fd=lock_fd,
            lock_name=lock_path.name,
            lock_identity=opened,
        )
    except BaseException:
        if lock_fd is not None:
            os.close(lock_fd)
        if platform_fd is not None:
            os.close(platform_fd)
        os.close(root_fd)
        raise


__all__ = (
    "AccountProfileLock",
    "AccountProfileLockError",
    "account_profile_lock_path",
    "acquire_account_profile_lock",
)
