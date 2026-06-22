#!/usr/bin/env python3
"""DPMS runtime preflight checks.

The checks are intentionally static / structural. They do not start browsers,
open pages, connect to platform accounts, or dispatch tasks. They are meant to
catch missing migration and lifecycle wiring before container-level smoke tests.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRODUCT_VERSION_RE = re.compile(r"Product Version:\s+0\.3\.\d+")


class CheckFailure(Exception):
    pass


def read(path: str) -> str:
    file_path = ROOT / path
    if not file_path.exists():
        raise CheckFailure(f"missing file: {path}")
    return file_path.read_text(encoding="utf-8")


def require_contains(path: str, needles: list[str]) -> None:
    text = read(path)
    missing = [needle for needle in needles if needle not in text]
    if missing:
        raise CheckFailure(f"{path} missing: {', '.join(missing)}")


def require_files(paths: list[str]) -> None:
    for path in paths:
        if not (ROOT / path).exists():
            raise CheckFailure(f"required file missing: {path}")


def run_migration_smoke() -> None:
    script = ROOT / "scripts" / "migration_smoke.py"
    if not script.exists():
        raise CheckFailure("scripts/migration_smoke.py is missing")
    result = subprocess.run([sys.executable, str(script)], cwd=str(ROOT), text=True, capture_output=True)
    if result.returncode != 0:
        raise CheckFailure(f"migration_smoke failed:\n{result.stdout}\n{result.stderr}")


def check_migration_contract() -> None:
    require_files(
        [
            "core/migrations/0002_worker_lease_deadletter.sql",
            "core/migrations/0003_task_event_outbox.sql",
            "core/migrations/0004_task_terminal_outbox_trigger.sql",
            "core/migrations/0005_terminal_notify_outbox_trigger.sql",
        ]
    )


def check_smoke_script_contract() -> None:
    require_files(
        [
            "scripts/container_runtime_smoke.py",
            "scripts/controlled_worker_lifecycle_smoke.py",
            "scripts/controlled_browser_lifecycle_smoke.py",
        ]
    )
    require_contains(
        "scripts/controlled_browser_lifecycle_smoke.py",
        [
            "Default mode is dry-run",
            "--execute",
            "chromium.launch",
            "browser.new_context",
            "context.close",
            "browser.close",
            "no account login",
            "no task enqueue",
            "no platform page open",
            "no platform action",
        ],
    )


def check_browser_lifecycle_contract() -> None:
    require_contains(
        "worker/app/browser_pool.py",
        [
            "PersistentContextState",
            "WORKER_PERSISTENT_CONTEXT_TTL_SECONDS",
            "WORKER_PERSISTENT_CONTEXT_IDLE_SECONDS",
            "WORKER_MAX_PERSISTENT_CONTEXTS",
            "prune_persistent_contexts",
            "context_reaper_loop",
            "account_context_memory_snapshot",
            "close_browser",
        ],
    )
    require_contains(
        "worker/app/main.py",
        [
            "context_reaper_task",
            "pool.context_reaper_loop",
            "WORKER_CONTEXT_REAPER_INTERVAL_SECONDS",
            "await pool.close_browser",
            "await pool.close_account_context",
        ],
    )


def check_runtime_status_contract() -> None:
    text = read("VERSION.md")
    if not PRODUCT_VERSION_RE.search(text):
        raise CheckFailure("VERSION.md missing Product Version: 0.3.x")
    for required in [
        "Production Readiness: Not Ready",
        "Real-run Status: Gated / Calibration Required",
    ]:
        if required not in text:
            raise CheckFailure(f"VERSION.md missing: {required}")


def check_schema_boundary_contract() -> None:
    require_contains(
        "core/app/db.py",
        [
            "GuardedDatabase",
            "allow_schema_writes",
            "runtime_schema_write_skipped_in_production",
        ],
    )
    require_contains(
        "core/app/migrations_runner.py",
        [
            "verify_production_schema",
            "Production schema drift detected",
            "allow_schema_writes",
        ],
    )


def main() -> int:
    checks = [
        ("migration_smoke", run_migration_smoke),
        ("migration_contract", check_migration_contract),
        ("smoke_script_contract", check_smoke_script_contract),
        ("schema_boundary_contract", check_schema_boundary_contract),
        ("browser_lifecycle_contract", check_browser_lifecycle_contract),
        ("runtime_status_contract", check_runtime_status_contract),
    ]
    failed: list[str] = []
    for name, fn in checks:
        try:
            fn()
            print(f"ok: {name}")
        except Exception as exc:
            failed.append(f"{name}: {exc}")
            print(f"failed: {name}: {exc}", file=sys.stderr)
    if failed:
        print("\nPreflight failed:", file=sys.stderr)
        for item in failed:
            print(f"- {item}", file=sys.stderr)
        return 1
    print("ok: runtime preflight passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
