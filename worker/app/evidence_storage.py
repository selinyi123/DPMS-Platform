"""Fail-closed filesystem primitives for Worker screenshot evidence.

The evidence writer must not assume that a mounted directory implements the
POSIX operations its safety model depends on.  This module currently owns the
startup preflight and exposes reusable filesystem primitives.

``task_runner`` still has compatibility helpers whose tests use directories
under world-writable ``/tmp`` and whose exceptions are part of existing error
handling.  Converging those helpers requires a separate, low-priority cleanup
that first supplies private test ancestors and maps the legacy error codes; it
is intentionally not implied by this startup-only change.
"""

from __future__ import annotations

import errno
import os
import stat
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable

try:
    import fcntl
except ImportError:  # pragma: no cover - production workers are Linux-only
    fcntl = None


EVIDENCE_ROOT = Path(os.getenv("EVIDENCE_ROOT", "/profiles"))
TASK_FAILURE_EVIDENCE_DIR = EVIDENCE_ROOT / "task-failures"
SHADOW_EVIDENCE_DIR = EVIDENCE_ROOT / "shadow-runs"
ADAPTER_PROBE_EVIDENCE_DIR = EVIDENCE_ROOT / "adapter-probes"
DEFAULT_EVIDENCE_DIRECTORIES = (
    TASK_FAILURE_EVIDENCE_DIR,
    SHADOW_EVIDENCE_DIR,
    ADAPTER_PROBE_EVIDENCE_DIR,
)

_PRIVATE_DIRECTORY_FORBIDDEN_MODE = stat.S_IWGRP | stat.S_IWOTH
_PROBE_PAYLOAD = b"dpms-evidence-storage-preflight\n"
PREFLIGHT_LOCK_TIMEOUT_SECONDS = 2.0
PREFLIGHT_LOCK_RETRY_SECONDS = 0.05


@dataclass(frozen=True)
class EvidenceStoragePreflightError(RuntimeError):
    """A stable, non-secret startup error suitable for structured logging."""

    code: str
    directory: str
    component: str | None = None
    operation: str | None = None
    errno_value: int | None = None
    cause_type: str | None = None
    primary_code: str | None = None

    def __str__(self) -> str:
        fields = [self.code, self.directory]
        if self.component:
            fields.append(self.component)
        if self.operation:
            fields.append(self.operation)
        if self.errno_value is not None:
            fields.append(f"errno={self.errno_value}")
        return ":".join(fields)


def preflight_evidence_storage(
    directories: Iterable[Path] = DEFAULT_EVIDENCE_DIRECTORIES,
) -> tuple[str, ...]:
    """Prove the mounted evidence store supports the required safety model.

    For every directory this validates absolute openat traversal without
    following symlinks, private modes on every non-root component, non-blocking
    advisory locks, exclusive file creation, file fsync, directory fsync, and
    identity-checked unlink.  A random probe is always removed before return.
    Existing evidence names are never opened or modified.
    """

    _require_posix_capabilities()
    checked: list[str] = []
    for raw_directory in directories:
        directory = Path(raw_directory)
        directory_fd = -1
        try:
            directory_fd, directory_identity = open_locked_evidence_directory(
                directory,
                create=True,
                lock_timeout_seconds=PREFLIGHT_LOCK_TIMEOUT_SECONDS,
            )
            _run_durable_probe(directory_fd, directory, directory_identity)
            checked.append(str(Path(os.path.abspath(str(directory)))))
        except EvidenceStoragePreflightError:
            raise
        except OSError as exc:
            raise _filesystem_error(
                "evidence_storage_preflight_failed",
                directory,
                operation="preflight",
                exc=exc,
            ) from exc
        finally:
            if directory_fd >= 0:
                close_locked_evidence_directory(directory_fd)
    return tuple(checked)


def open_locked_evidence_directory(
    directory: Path,
    *,
    create: bool = False,
    nonblocking: bool = False,
    lock_timeout_seconds: float | None = None,
) -> tuple[int, tuple[int, int]]:
    """Pin an absolute directory with openat/O_NOFOLLOW and take a flock.

    ``create`` may create only the final component, with mode ``0700``.  Every
    parent must already exist and every non-root component must reject group or
    world writes.  The returned descriptor owns the advisory lock and must be
    released with :func:`close_locked_evidence_directory`.
    """

    _require_posix_capabilities()
    absolute = Path(os.path.abspath(str(directory)))
    if not directory.is_absolute() or not absolute.is_absolute():
        raise EvidenceStoragePreflightError(
            "evidence_directory_not_absolute",
            str(directory),
            operation="openat",
        )

    flags = (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        current_fd = os.open(os.sep, flags)
    except OSError as exc:
        raise _filesystem_error(
            "evidence_directory_root_open_failed",
            absolute,
            component=os.sep,
            operation="openat",
            exc=exc,
        ) from exc

    current_path = Path(os.sep)
    try:
        parts = absolute.parts[1:]
        if not parts:
            raise EvidenceStoragePreflightError(
                "evidence_directory_root_forbidden",
                str(absolute),
                component=os.sep,
                operation="openat",
            )

        for index, part in enumerate(parts):
            is_final = index == len(parts) - 1
            next_path = current_path / part
            try:
                next_fd = os.open(part, flags, dir_fd=current_fd)
            except FileNotFoundError as exc:
                if not create or not is_final:
                    raise _filesystem_error(
                        "evidence_directory_missing",
                        absolute,
                        component=next_path,
                        operation="openat",
                        exc=exc,
                    ) from exc
                _create_final_directory(current_fd, part, absolute, next_path)
                try:
                    next_fd = os.open(part, flags, dir_fd=current_fd)
                except OSError as open_exc:
                    raise _filesystem_error(
                        "evidence_directory_open_failed",
                        absolute,
                        component=next_path,
                        operation="openat",
                        exc=open_exc,
                    ) from open_exc
            except OSError as exc:
                raise _filesystem_error(
                    "evidence_directory_open_failed",
                    absolute,
                    component=next_path,
                    operation="openat",
                    exc=exc,
                ) from exc

            os.close(current_fd)
            current_fd = next_fd
            current_path = next_path
            component_stat = os.fstat(current_fd)
            if not stat.S_ISDIR(component_stat.st_mode):
                raise EvidenceStoragePreflightError(
                    "evidence_directory_component_not_directory",
                    str(absolute),
                    component=str(current_path),
                    operation="fstat",
                )
            if component_stat.st_mode & _PRIVATE_DIRECTORY_FORBIDDEN_MODE:
                raise EvidenceStoragePreflightError(
                    "evidence_directory_mode_insecure",
                    str(absolute),
                    component=str(current_path),
                    operation="mode_check",
                )

        _acquire_directory_lock(
            current_fd,
            absolute,
            current_path,
            nonblocking=nonblocking,
            lock_timeout_seconds=lock_timeout_seconds,
        )
        directory_stat = os.fstat(current_fd)
        return current_fd, (directory_stat.st_dev, directory_stat.st_ino)
    except Exception:
        os.close(current_fd)
        raise


def close_locked_evidence_directory(directory_fd: int) -> None:
    """Release a descriptor returned by ``open_locked_evidence_directory``."""

    try:
        if fcntl is not None:
            fcntl.flock(directory_fd, fcntl.LOCK_UN)
    finally:
        os.close(directory_fd)


def _require_posix_capabilities() -> None:
    required_dir_fd = (os.open, os.mkdir, os.stat, os.unlink)
    missing_dir_fd = any(
        operation not in getattr(os, "supports_dir_fd", set())
        for operation in required_dir_fd
    )
    if (
        os.name != "posix"
        or fcntl is None
        or not hasattr(fcntl, "LOCK_NB")
        or not hasattr(os, "O_DIRECTORY")
        or not hasattr(os, "O_NOFOLLOW")
        or missing_dir_fd
    ):
        raise EvidenceStoragePreflightError(
            "evidence_storage_posix_capabilities_missing",
            "<all>",
            operation="capability_check",
        )


def _create_final_directory(
    parent_fd: int,
    name: str,
    directory: Path,
    component: Path,
) -> None:
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
        os.fsync(parent_fd)
    except FileExistsError:
        # A concurrent creator is safe only if the subsequent O_NOFOLLOW open
        # and mode checks accept what appeared.
        return
    except OSError as exc:
        raise _filesystem_error(
            "evidence_directory_create_failed",
            directory,
            component=component,
            operation="mkdirat_or_parent_fsync",
            exc=exc,
        ) from exc


def _run_durable_probe(
    directory_fd: int,
    directory: Path,
    directory_identity: tuple[int, int],
) -> None:
    probe_name = f".dpms-evidence-preflight-{os.getpid()}-{uuid.uuid4().hex}"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )
    probe_fd = -1
    probe_created = False
    probe_identity: tuple[int, int, int, int] | None = None
    primary_error: BaseException | None = None
    try:
        try:
            probe_fd = os.open(probe_name, flags, 0o600, dir_fd=directory_fd)
            probe_created = True
        except OSError as exc:
            raise _filesystem_error(
                "evidence_probe_create_failed",
                directory,
                component=directory / probe_name,
                operation="openat_exclusive",
                exc=exc,
            ) from exc

        try:
            created = os.fstat(probe_fd)
        except OSError as exc:
            raise _filesystem_error(
                "evidence_probe_identity_failed",
                directory,
                component=directory / probe_name,
                operation="fstat",
                exc=exc,
            ) from exc
        if not stat.S_ISREG(created.st_mode):
            raise EvidenceStoragePreflightError(
                "evidence_probe_not_regular",
                str(directory),
                component=str(directory / probe_name),
                operation="fstat",
            )
        probe_identity = (
            directory_identity[0],
            directory_identity[1],
            created.st_dev,
            created.st_ino,
        )
        written = os.write(probe_fd, _PROBE_PAYLOAD)
        if written != len(_PROBE_PAYLOAD):
            raise EvidenceStoragePreflightError(
                "evidence_probe_write_incomplete",
                str(directory),
                component=str(directory / probe_name),
                operation="write",
            )
        try:
            os.fsync(probe_fd)
        except OSError as exc:
            raise _filesystem_error(
                "evidence_probe_file_fsync_unsupported",
                directory,
                component=directory / probe_name,
                operation="file_fsync",
                exc=exc,
            ) from exc
        final = os.fstat(probe_fd)
        if (
            (final.st_dev, final.st_ino) != probe_identity[2:]
            or final.st_size != len(_PROBE_PAYLOAD)
        ):
            raise EvidenceStoragePreflightError(
                "evidence_probe_write_verification_failed",
                str(directory),
                component=str(directory / probe_name),
                operation="fstat",
            )
        try:
            os.fsync(directory_fd)
        except OSError as exc:
            raise _filesystem_error(
                "evidence_probe_directory_fsync_unsupported",
                directory,
                component=directory,
                operation="directory_fsync_after_create",
                exc=exc,
            ) from exc
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        cleanup_error: EvidenceStoragePreflightError | None = None
        if probe_fd >= 0:
            try:
                os.close(probe_fd)
            except OSError as exc:
                cleanup_error = _filesystem_error(
                    "evidence_probe_close_failed",
                    directory,
                    component=directory / probe_name,
                    operation="close",
                    exc=exc,
                )
        if probe_identity is not None:
            try:
                _unlink_probe_if_identity_matches(
                    directory_fd,
                    directory,
                    probe_name,
                    probe_identity,
                )
                os.fsync(directory_fd)
            except Exception as cleanup_exc:
                if isinstance(cleanup_exc, EvidenceStoragePreflightError):
                    cleanup_error = cleanup_exc
                else:
                    cleanup_error = _filesystem_error(
                        "evidence_probe_cleanup_failed",
                        directory,
                        component=directory / probe_name,
                        operation="unlinkat_or_directory_fsync",
                        exc=cleanup_exc,
                    )
        elif probe_created and cleanup_error is None:
            # The exclusive create succeeded but no descriptor identity was
            # established. Deleting by pathname would risk removing a file
            # substituted by a process that ignores the advisory lock, so
            # leave it in place and make the cleanup failure explicit.
            cleanup_error = EvidenceStoragePreflightError(
                "evidence_probe_cleanup_identity_unavailable",
                str(directory),
                component=str(directory / probe_name),
                operation="identity_bound_cleanup",
            )
        if cleanup_error is not None:
            # Cleanup failure is the most safety-relevant result: startup must
            # report that it could not prove its random probe was removed.
            if primary_error is not None:
                cleanup_error = replace(
                    cleanup_error,
                    primary_code=(
                        primary_error.code
                        if isinstance(primary_error, EvidenceStoragePreflightError)
                        else type(primary_error).__name__
                    ),
                )
                raise cleanup_error from primary_error
            raise cleanup_error


def _acquire_directory_lock(
    directory_fd: int,
    directory: Path,
    component: Path,
    *,
    nonblocking: bool,
    lock_timeout_seconds: float | None,
) -> None:
    if lock_timeout_seconds is not None and lock_timeout_seconds < 0:
        raise ValueError("lock_timeout_seconds must be non-negative")

    bounded_retry = lock_timeout_seconds is not None
    use_nonblocking = nonblocking or bounded_retry
    lock_mode = fcntl.LOCK_EX | (fcntl.LOCK_NB if use_nonblocking else 0)
    deadline = (
        time.monotonic() + lock_timeout_seconds
        if lock_timeout_seconds is not None
        else None
    )
    while True:
        try:
            fcntl.flock(directory_fd, lock_mode)
            return
        except OSError as exc:
            contended = exc.errno in {errno.EACCES, errno.EAGAIN}
            if not contended:
                raise _filesystem_error(
                    "evidence_directory_lock_unsupported",
                    directory,
                    component=component,
                    operation="flock",
                    exc=exc,
                ) from exc
            if deadline is None or time.monotonic() >= deadline:
                raise _filesystem_error(
                    "evidence_directory_lock_contended",
                    directory,
                    component=component,
                    operation="flock",
                    exc=exc,
                ) from exc
            time.sleep(
                min(
                    PREFLIGHT_LOCK_RETRY_SECONDS,
                    max(0.0, deadline - time.monotonic()),
                )
            )


def _unlink_probe_if_identity_matches(
    directory_fd: int,
    directory: Path,
    probe_name: str,
    expected_identity: tuple[int, int, int, int],
) -> None:
    directory_stat = os.fstat(directory_fd)
    if (directory_stat.st_dev, directory_stat.st_ino) != expected_identity[:2]:
        raise EvidenceStoragePreflightError(
            "evidence_probe_cleanup_directory_changed",
            str(directory),
            component=str(directory),
            operation="fstat",
        )
    try:
        current = os.stat(probe_name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError as exc:
        raise _filesystem_error(
            "evidence_probe_cleanup_missing",
            directory,
            component=directory / probe_name,
            operation="fstatat",
            exc=exc,
        ) from exc
    if (
        not stat.S_ISREG(current.st_mode)
        or (current.st_dev, current.st_ino) != expected_identity[2:]
    ):
        raise EvidenceStoragePreflightError(
            "evidence_probe_cleanup_identity_mismatch",
            str(directory),
            component=str(directory / probe_name),
            operation="fstatat",
        )
    try:
        os.unlink(probe_name, dir_fd=directory_fd)
    except OSError as exc:
        raise _filesystem_error(
            "evidence_probe_cleanup_failed",
            directory,
            component=directory / probe_name,
            operation="unlinkat",
            exc=exc,
        ) from exc


def _filesystem_error(
    code: str,
    directory: Path,
    *,
    operation: str,
    exc: BaseException,
    component: Path | str | None = None,
) -> EvidenceStoragePreflightError:
    return EvidenceStoragePreflightError(
        code=code,
        directory=str(directory),
        component=str(component) if component is not None else None,
        operation=operation,
        errno_value=getattr(exc, "errno", None),
        cause_type=type(exc).__name__,
    )
