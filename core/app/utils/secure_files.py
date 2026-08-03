"""Race-resistant reads for files inside trusted Linux volume roots."""

from __future__ import annotations

import os
from pathlib import Path
import stat
from dataclasses import dataclass
from typing import BinaryIO, Iterator


class SecureFileError(OSError):
    pass


@dataclass
class SecureFileSnapshot:
    """One descriptor-pinned, bounded regular file for chunked delivery."""

    stream: BinaryIO
    size: int
    signature: tuple[int, int, int, int, int]

    def iter_chunks(self, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        if chunk_size <= 0:
            self.stream.close()
            raise ValueError("secure_file_chunk_size_invalid")
        remaining = self.size
        try:
            while remaining:
                content = self.stream.read(min(chunk_size, remaining))
                if not content:
                    raise SecureFileError("secure_file_changed_during_read")
                remaining -= len(content)
                yield content
            if self.stream.read(1):
                raise SecureFileError("secure_file_changed_during_read")
            after = os.fstat(self.stream.fileno())
            after_signature = (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
            if after_signature != self.signature:
                raise SecureFileError("secure_file_changed_during_read")
        except SecureFileError:
            raise
        except OSError as exc:
            raise SecureFileError("secure_file_unavailable") from exc
        finally:
            self.stream.close()


def _resolved_file_beneath_root(
    root: Path,
    candidate: Path,
) -> tuple[Path, Path]:
    root = Path(root)
    candidate = Path(candidate)
    if not root.is_absolute() or not candidate.is_absolute():
        raise SecureFileError("secure_file_path_not_absolute")
    try:
        resolved_root = root.resolve(strict=True)
    except OSError as exc:
        raise SecureFileError("secure_file_unavailable") from exc
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise SecureFileError("secure_file_outside_root") from exc
    if (
        not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise SecureFileError("secure_file_path_invalid")
    return resolved_root, resolved_root.joinpath(*relative.parts)


def open_file_beneath_resolved_root(
    resolved_root: Path,
    resolved_candidate: Path,
):
    """Open an exact regular-file candidate without following mutable links."""

    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
        raise SecureFileError("secure_file_open_unsupported")
    if not resolved_root.is_absolute():
        raise SecureFileError("secure_file_root_not_absolute")
    try:
        relative = resolved_candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise SecureFileError("secure_file_outside_root") from exc
    if (
        not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise SecureFileError("secure_file_path_invalid")

    close_on_exec = getattr(os, "O_CLOEXEC", 0)
    directory_flags = (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
        | close_on_exec
    )
    file_flags = (
        os.O_RDONLY
        | os.O_NOFOLLOW
        | os.O_NONBLOCK
        | close_on_exec
    )
    directory_fds: list[int] = []
    file_fd: int | None = None
    try:
        current_fd = os.open(os.path.sep, directory_flags)
        directory_fds.append(current_fd)
        for part in resolved_root.parts[1:]:
            current_fd = os.open(
                part,
                directory_flags,
                dir_fd=current_fd,
            )
            directory_fds.append(current_fd)
        for part in relative.parts[:-1]:
            current_fd = os.open(
                part,
                directory_flags,
                dir_fd=current_fd,
            )
            directory_fds.append(current_fd)
        file_fd = os.open(
            relative.parts[-1],
            file_flags,
            dir_fd=current_fd,
        )
        stream = os.fdopen(file_fd, "rb")
        file_fd = None
        return stream
    finally:
        if file_fd is not None:
            os.close(file_fd)
        for directory_fd in reversed(directory_fds):
            os.close(directory_fd)


def open_bounded_regular_file_beneath_root(
    root: Path,
    candidate: Path,
    *,
    max_bytes: int,
) -> SecureFileSnapshot:
    """Pin a bounded regular file without buffering its contents in Core."""

    if max_bytes <= 0:
        raise ValueError("secure_file_max_bytes_invalid")
    resolved_root, resolved_candidate = _resolved_file_beneath_root(
        root,
        candidate,
    )
    try:
        stream = open_file_beneath_resolved_root(
            resolved_root,
            resolved_candidate,
        )
        try:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode):
                raise SecureFileError("secure_file_not_regular")
            if opened.st_size < 0 or opened.st_size > max_bytes:
                raise SecureFileError("secure_file_size_invalid")
            return SecureFileSnapshot(
                stream=stream,
                size=opened.st_size,
                signature=(
                    opened.st_dev,
                    opened.st_ino,
                    opened.st_size,
                    opened.st_mtime_ns,
                    opened.st_ctime_ns,
                ),
            )
        except Exception:
            stream.close()
            raise
    except SecureFileError:
        raise
    except OSError as exc:
        raise SecureFileError("secure_file_unavailable") from exc


def read_bounded_regular_file_beneath_root(
    root: Path,
    candidate: Path,
    *,
    max_bytes: int,
) -> bytes:
    """Return one stable file snapshot or fail closed on path/size drift."""

    if max_bytes <= 0:
        raise ValueError("secure_file_max_bytes_invalid")
    resolved_root, resolved_candidate = _resolved_file_beneath_root(
        root,
        candidate,
    )

    try:
        with open_file_beneath_resolved_root(
            resolved_root,
            resolved_candidate,
        ) as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise SecureFileError("secure_file_not_regular")
            if before.st_size < 0 or before.st_size > max_bytes:
                raise SecureFileError("secure_file_size_invalid")
            signature = (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            )
            content = stream.read(max_bytes + 1)
            after = os.fstat(stream.fileno())
            after_signature = (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
    except SecureFileError:
        raise
    except OSError as exc:
        raise SecureFileError("secure_file_unavailable") from exc
    if (
        len(content) > max_bytes
        or len(content) != before.st_size
        or after_signature != signature
    ):
        raise SecureFileError("secure_file_changed_during_read")
    return content


__all__ = (
    "SecureFileError",
    "SecureFileSnapshot",
    "open_bounded_regular_file_beneath_root",
    "open_file_beneath_resolved_root",
    "read_bounded_regular_file_beneath_root",
)
