from __future__ import annotations

import hashlib
import json
import os
import queue
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from .engine import ActionRequest, ActionResult, DeviceActionEngine


class CommandSource(Protocol):
    """Future Core/Redis adapters can implement this without changing the loop."""

    def next_command(self, timeout_seconds: float) -> ActionRequest | None:
        ...


class QueueCommandSource:
    def __init__(self, commands: queue.Queue[ActionRequest]) -> None:
        self.commands = commands

    def next_command(self, timeout_seconds: float) -> ActionRequest | None:
        try:
            return self.commands.get(timeout=timeout_seconds)
        except queue.Empty:
            return None


@dataclass
class HealthHeartbeat:
    path: Path
    account_id: str
    serial: str
    clock: Callable[[], float] = time.time

    def __init__(
        self,
        *,
        path: str | Path,
        account_id: str,
        serial: str,
        clock=time.time,
    ) -> None:
        self.path = Path(path)
        self.account_id = account_id
        self.serial = serial
        self.clock = clock

    @staticmethod
    def _hash(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]

    def write(
        self,
        *,
        status: str,
        healthy: bool,
        last_result: ActionResult | None = None,
    ) -> None:
        payload: dict[str, object] = {
            "version": 1,
            "status": status,
            "healthy": bool(healthy),
            "observed_at": float(self.clock()),
            "account_hash": self._hash(self.account_id),
            "device_hash": self._hash(self.serial),
        }
        if last_result is not None:
            payload["last_result"] = last_result.to_dict()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self.path.parent,
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            text=True,
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.path)
        finally:
            if temporary_path.exists():
                temporary_path.unlink()


class ResidentDeviceLoop:
    """Blocking resident loop with a deliberately small integration surface.

    This loop has no DPMS/Redis implementation.  It only consumes the
    ``CommandSource`` protocol and stops immediately on blocked or unknown
    outcomes.
    """

    def __init__(
        self,
        *,
        engine: DeviceActionEngine,
        source: CommandSource,
        heartbeat: HealthHeartbeat,
        poll_seconds: float = 5.0,
    ) -> None:
        if not (0.05 <= poll_seconds <= 60):
            raise ValueError("poll_seconds must be between 0.05 and 60")
        self.engine = engine
        self.source = source
        self.heartbeat = heartbeat
        self.poll_seconds = float(poll_seconds)

    def run(
        self,
        *,
        stop_event: threading.Event | None = None,
        max_iterations: int | None = None,
    ) -> ActionResult | None:
        stop = stop_event or threading.Event()
        iterations = 0
        last_result: ActionResult | None = None
        final_status = "stopped"
        final_healthy = True
        self.heartbeat.write(
            status="starting", healthy=self.engine.adb.health(), last_result=None
        )
        while not stop.is_set():
            if max_iterations is not None and iterations >= max_iterations:
                break
            iterations += 1
            healthy = self.engine.adb.health()
            if not healthy:
                self.heartbeat.write(
                    status="device_unhealthy", healthy=False, last_result=last_result
                )
                final_status = "device_unhealthy"
                final_healthy = False
                break
            command = self.source.next_command(self.poll_seconds)
            if command is None:
                self.heartbeat.write(
                    status="idle", healthy=True, last_result=last_result
                )
                continue
            self.heartbeat.write(
                status="executing", healthy=True, last_result=last_result
            )
            last_result = self.engine.execute(command)
            self.heartbeat.write(
                status="halted" if last_result.halt else "idle",
                healthy=not last_result.halt,
                last_result=last_result,
            )
            if last_result.halt:
                final_status = "halted"
                final_healthy = False
                break
        self.heartbeat.write(
            status=final_status,
            healthy=final_healthy,
            last_result=last_result,
        )
        return last_result
