"""Process-unique, bounded identity shared by every Worker subsystem."""

import hashlib
import os
import re
import secrets
import socket


MAX_WORKER_ID_LENGTH = 128
_UNSAFE_ID_CHARACTER = re.compile(r"[^A-Za-z0-9._-]+")
_CONFIGURED_ID_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
)
_RANDOM_WORKER_ID_PATTERN = re.compile(
    r"^[A-Za-z0-9._-]+:[0-9]+:[0-9a-f]{32}$"
)


def build_worker_instance_id(
    *,
    configured_id: str | None = None,
    base: str | None = None,
    pid: int | None = None,
    instance_nonce: str | None = None,
) -> str:
    """Build one Redis/DB-safe identity without conflating sibling processes."""

    explicit = str(configured_id or "").strip()
    if explicit:
        if (
            not explicit.startswith("dpms-worker-")
            or not _CONFIGURED_ID_PATTERN.fullmatch(explicit)
        ):
            raise ValueError("worker_instance_id_invalid")
        return explicit

    raw_base = str(
        base
        if base is not None
        else (
            os.getenv("HOSTNAME")
            or socket.gethostname()
            or "worker"
        )
    ).strip()
    normalized_base = _UNSAFE_ID_CHARACTER.sub("_", raw_base).strip("._-")
    if not normalized_base:
        normalized_base = "worker"
    process_id = max(int(os.getpid() if pid is None else pid), 0)
    raw_nonce = (
        secrets.token_hex(16)
        if instance_nonce is None
        else str(instance_nonce)
    )
    normalized_nonce = _UNSAFE_ID_CHARACTER.sub("_", raw_nonce).strip("._-")
    if not normalized_nonce:
        normalized_nonce = "instance"
    normalized_nonce = normalized_nonce[:32]
    candidate = f"{normalized_base}:{process_id}:{normalized_nonce}"
    if len(candidate) <= MAX_WORKER_ID_LENGTH:
        return candidate

    digest = hashlib.sha256(normalized_base.encode("utf-8")).hexdigest()[:16]
    suffix = f":{digest}:{process_id}:{normalized_nonce}"
    prefix_length = max(MAX_WORKER_ID_LENGTH - len(suffix), 1)
    return f"{normalized_base[:prefix_length]}{suffix}"


def is_worker_instance_id(value: object) -> bool:
    """Recognise only identities this Worker implementation can have issued."""

    candidate = str(value or "").strip()
    return bool(
        (
            candidate.startswith("dpms-worker-")
            and _CONFIGURED_ID_PATTERN.fullmatch(candidate)
        )
        or _RANDOM_WORKER_ID_PATTERN.fullmatch(candidate)
    )


WORKER_ID = build_worker_instance_id(
    configured_id=os.getenv("DPMS_WORKER_INSTANCE_ID")
)
