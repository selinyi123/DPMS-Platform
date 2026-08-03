from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol, Sequence


class AdbError(RuntimeError):
    """An ADB command failed or returned an unusable response."""


@dataclass(frozen=True)
class AdbResult:
    returncode: int
    stdout: str
    stderr: str = ""


class AdbTransport(Protocol):
    """Minimal transport contract used by the device action engine."""

    serial: str

    def foreground_package(self) -> str:
        ...

    def dump_ui_xml(self) -> str:
        ...

    def tap(self, x: int, y: int) -> None:
        ...

    def input_text(self, value: str) -> None:
        ...

    def health(self) -> bool:
        ...


Runner = Callable[..., subprocess.CompletedProcess[str]]


class SubprocessAdb:
    """ADB transport with explicit executable, serial and command timeout.

    Every process is launched with an argument vector.  Shell execution is
    never used.  The caller must provide an absolute path to an existing ADB
    executable and a non-empty device serial; environment defaults are not
    accepted.
    """

    _FOREGROUND_RE = re.compile(
        r"(?P<package>[A-Za-z][A-Za-z0-9._]+)/[A-Za-z0-9._$]+"
    )
    _REMOTE_DUMP = "/sdcard/dpms_device_agent_window.xml"
    _MAX_XML_BYTES = 5 * 1024 * 1024

    def __init__(
        self,
        *,
        executable: str | Path,
        serial: str,
        timeout_seconds: float = 15.0,
        runner: Runner = subprocess.run,
    ) -> None:
        executable_path = Path(executable)
        if not executable_path.is_absolute():
            raise ValueError("adb executable must be an explicit absolute path")
        if not executable_path.is_file():
            raise ValueError("adb executable does not exist or is not a file")
        normalized_serial = serial.strip()
        if not normalized_serial:
            raise ValueError("device serial must be explicitly configured")
        if not (0 < timeout_seconds <= 60):
            raise ValueError("ADB timeout must be between 0 and 60 seconds")

        self.executable = str(executable_path)
        self.serial = normalized_serial
        self.timeout_seconds = float(timeout_seconds)
        self._runner = runner

    def _run(self, args: Sequence[str]) -> AdbResult:
        if not args or any(not isinstance(item, str) or not item for item in args):
            raise ValueError("ADB arguments must be non-empty strings")
        command = [self.executable, "-s", self.serial, *args]
        try:
            completed = self._runner(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise AdbError(f"ADB command timed out after {self.timeout_seconds:g}s") from exc
        except OSError as exc:
            raise AdbError(f"unable to launch ADB: {exc}") from exc

        result = AdbResult(
            returncode=int(completed.returncode),
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "unknown ADB error"
            raise AdbError(f"ADB command failed ({result.returncode}): {detail[:500]}")
        return result

    def foreground_package(self) -> str:
        result = self._run(["shell", "dumpsys", "window", "windows"])
        for line in result.stdout.splitlines():
            if "mCurrentFocus" not in line and "mFocusedApp" not in line:
                continue
            match = self._FOREGROUND_RE.search(line)
            if match:
                return match.group("package")
        raise AdbError("unable to determine foreground Android package")

    def dump_ui_xml(self) -> str:
        self._run(
            [
                "shell",
                "uiautomator",
                "dump",
                "--compressed",
                self._REMOTE_DUMP,
            ]
        )
        result = self._run(["exec-out", "cat", self._REMOTE_DUMP])
        encoded_size = len(result.stdout.encode("utf-8", errors="replace"))
        if encoded_size > self._MAX_XML_BYTES:
            raise AdbError("uiautomator XML exceeds the 5 MiB safety limit")
        if "<hierarchy" not in result.stdout:
            raise AdbError("uiautomator did not return a hierarchy document")
        return result.stdout

    def tap(self, x: int, y: int) -> None:
        if x < 0 or y < 0:
            raise ValueError("tap coordinates must be non-negative")
        self._run(["shell", "input", "tap", str(x), str(y)])

    def input_text(self, value: str) -> None:
        if not value or len(value) > 500:
            raise ValueError("comment text must contain 1 to 500 characters")
        if any(character in value for character in ("\x00", "\n", "\r")):
            raise ValueError("comment text cannot contain NUL or newlines")
        # Android's `input text` represents spaces as %s.  Exact read-back is
        # mandatory before submit, so unsupported IME/Unicode behavior fails
        # closed without sending the comment.
        encoded = value.replace(" ", "%s")
        self._run(["shell", "input", "text", encoded])

    def health(self) -> bool:
        try:
            return self._run(["get-state"]).stdout.strip() == "device"
        except AdbError:
            return False
