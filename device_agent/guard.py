from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


class AccountLockError(RuntimeError):
    """Another process owns the account's device execution lock."""


class RateLimitStateError(RuntimeError):
    """Persistent limiter state is malformed and execution must stop."""


def _account_key(account_id: str) -> str:
    normalized = account_id.strip()
    if not normalized:
        raise ValueError("account_id must be explicitly configured")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]


class AccountFileLock:
    """Cross-process account lock using only Python's standard library."""

    def __init__(
        self,
        *,
        state_dir: str | Path,
        account_id: str,
        timeout_seconds: float = 0.0,
        poll_seconds: float = 0.05,
    ) -> None:
        if timeout_seconds < 0 or timeout_seconds > 60:
            raise ValueError("lock timeout must be between 0 and 60 seconds")
        if poll_seconds <= 0 or poll_seconds > 1:
            raise ValueError("lock polling interval must be between 0 and 1 second")
        self.state_dir = Path(state_dir)
        self.account_key = _account_key(account_id)
        self.timeout_seconds = float(timeout_seconds)
        self.poll_seconds = float(poll_seconds)
        self.path = self.state_dir / f"account-{self.account_key}.lock"
        self._handle = None

    def acquire(self) -> None:
        if self._handle is not None:
            raise RuntimeError("account lock is already held")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._handle = handle
                return
            except OSError as exc:
                if time.monotonic() >= deadline:
                    handle.close()
                    raise AccountLockError("account execution lock is already held") from exc
                time.sleep(self.poll_seconds)

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            self._handle = None

    def __enter__(self) -> "AccountFileLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()


@dataclass(frozen=True)
class RateLimitPolicy:
    min_interval_seconds: float = 30.0
    max_actions_per_hour: int = 20
    unknown_cooldown_seconds: float = 300.0
    blocked_cooldown_seconds: float = 900.0

    def __post_init__(self) -> None:
        if self.min_interval_seconds < 0:
            raise ValueError("minimum action interval cannot be negative")
        if self.max_actions_per_hour <= 0:
            raise ValueError("hourly action limit must be positive")
        if self.unknown_cooldown_seconds < 0 or self.blocked_cooldown_seconds < 0:
            raise ValueError("cooldown durations cannot be negative")


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    reason: str | None = None
    retry_after_seconds: float = 0.0


class PersistentRateLimiter:
    """Account-scoped rate and cooldown state.

    The caller must hold ``AccountFileLock`` while using this class.  The state
    is deliberately fail-closed when corrupted instead of silently resetting.
    """

    def __init__(
        self,
        *,
        state_dir: str | Path,
        account_id: str,
        policy: RateLimitPolicy,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.state_dir = Path(state_dir)
        self.account_key = _account_key(account_id)
        self.policy = policy
        self.clock = clock
        self.path = self.state_dir / f"account-{self.account_key}.rate.json"

    def _load(self) -> dict[str, object]:
        if not self.path.exists():
            return {"version": 1, "action_times": [], "cooldown_until": 0.0}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RateLimitStateError(f"unable to read limiter state: {exc}") from exc
        if not isinstance(raw, dict) or raw.get("version") != 1:
            raise RateLimitStateError("limiter state has an unsupported format")
        action_times = raw.get("action_times")
        cooldown_until = raw.get("cooldown_until")
        if (
            not isinstance(action_times, list)
            or any(not isinstance(value, (int, float)) for value in action_times)
            or not isinstance(cooldown_until, (int, float))
        ):
            raise RateLimitStateError("limiter state contains invalid values")
        return {
            "version": 1,
            "action_times": [float(value) for value in action_times],
            "cooldown_until": float(cooldown_until),
        }

    def _save(self, state: dict[str, object]) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self.state_dir,
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            text=True,
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(state, handle, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.path)
        finally:
            if temporary_path.exists():
                temporary_path.unlink()

    def check(self) -> RateLimitDecision:
        now = float(self.clock())
        state = self._load()
        cooldown_until = float(state["cooldown_until"])
        if cooldown_until > now:
            return RateLimitDecision(
                allowed=False,
                reason="cooldown_active",
                retry_after_seconds=cooldown_until - now,
            )
        recent = sorted(
            value
            for value in state["action_times"]  # type: ignore[union-attr]
            if now - float(value) < 3600
        )
        if len(recent) >= self.policy.max_actions_per_hour:
            return RateLimitDecision(
                allowed=False,
                reason="hourly_rate_limit",
                retry_after_seconds=max(0.0, 3600 - (now - float(recent[0]))),
            )
        if recent and now - float(recent[-1]) < self.policy.min_interval_seconds:
            return RateLimitDecision(
                allowed=False,
                reason="minimum_interval",
                retry_after_seconds=(
                    self.policy.min_interval_seconds - (now - float(recent[-1]))
                ),
            )
        return RateLimitDecision(allowed=True)

    def record_mutation(self) -> None:
        now = float(self.clock())
        state = self._load()
        recent = [
            float(value)
            for value in state["action_times"]  # type: ignore[union-attr]
            if now - float(value) < 3600
        ]
        recent.append(now)
        state["action_times"] = recent
        self._save(state)

    def record_cooldown(self, *, blocked: bool) -> None:
        state = self._load()
        duration = (
            self.policy.blocked_cooldown_seconds
            if blocked
            else self.policy.unknown_cooldown_seconds
        )
        state["cooldown_until"] = max(
            float(state["cooldown_until"]), float(self.clock()) + duration
        )
        self._save(state)
