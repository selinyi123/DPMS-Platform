#!/usr/bin/env python3
"""Static DPMS runtime readiness contract checks.

This script does not start Docker, connect to services, or execute runtime work.
It verifies that the repository contains the smoke gates required before a later
readiness run.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION_RE = re.compile(r"Product Version:\s+0\.3\.\d+")

REQUIRED_FILES = [
    "docker-compose.yml",
    "scripts/runtime_preflight.py",
    "scripts/container_runtime_smoke.py",
    "scripts/controlled_worker_lifecycle_smoke.py",
    "scripts/controlled_browser_lifecycle_smoke.py",
    "worker/app/task_runner.py",
    "worker/app/services/task_outbox.py",
]

REQUIRED_MARKERS = {
    "worker/app/task_runner.py": [
        'STREAM_KEY = "lottery_tasks"',
        'GROUP_NAME = "workers"',
        "worker_id",
        "lease_expires_at",
        "failed_task_messages",
    ],
    "worker/app/services/task_outbox.py": [
        "task_outbox_events",
        "status = 'sending'",
        "status = 'pending'",
        "TASK_OUTBOX_SENDING_RECLAIM_SECONDS",
    ],
    "VERSION.md": [
        "Real-run Status: Gated / Bilibili API Adapter Wired",
        "Production Readiness: Not Ready",
    ],
}


class ContractFailure(Exception):
    pass


def read(path: str) -> str:
    target = ROOT / path
    if not target.exists():
        raise ContractFailure(f"missing file: {path}")
    return target.read_text(encoding="utf-8")


def check_required_files() -> None:
    for path in REQUIRED_FILES:
        if not (ROOT / path).exists():
            raise ContractFailure(f"missing file: {path}")


def check_required_markers() -> None:
    for path, markers in REQUIRED_MARKERS.items():
        text = read(path)
        missing = [marker for marker in markers if marker not in text]
        if missing:
            raise ContractFailure(f"{path} missing markers: {', '.join(missing)}")


def check_version() -> None:
    text = read("VERSION.md")
    if not VERSION_RE.search(text):
        raise ContractFailure("VERSION.md missing Product Version: 0.3.x")


def main() -> int:
    checks = [
        ("required_files", check_required_files),
        ("required_markers", check_required_markers),
        ("version", check_version),
    ]
    failures: list[str] = []
    for name, fn in checks:
        try:
            fn()
            print(f"ok: {name}")
        except Exception as exc:
            failures.append(f"{name}: {exc}")
            print(f"failed: {name}: {exc}", file=sys.stderr)
    if failures:
        print("contract readiness failed", file=sys.stderr)
        for item in failures:
            print(f"- {item}", file=sys.stderr)
        return 1
    print("ok: runtime readiness contract passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
