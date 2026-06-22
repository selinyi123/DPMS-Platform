#!/usr/bin/env python3
"""Controlled DPMS browser lifecycle smoke harness.

Default mode is dry-run and does not require Docker. Pass --execute to start the
minimal service set, wait for worker health, run a Chromium launch/context/close
smoke inside the worker container, and clean the isolated Compose project.

The smoke does not create accounts, login accounts, enqueue tasks, open platform
pages, or perform platform actions.
"""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = ROOT / "docker-compose.yml"
DEFAULT_PROJECT = "dpms-browser-smoke"
SERVICES = ["mysql", "redis", "core-api", "worker"]

BROWSER_SMOKE_CODE = r'''
import asyncio
import subprocess

from playwright.async_api import async_playwright


def chromium_process_lines():
    result = subprocess.run(
        ["sh", "-lc", "pgrep -fa 'chromium|chrome' || true"],
        text=True,
        capture_output=True,
        check=False,
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


async def main():
    before = chromium_process_lines()
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context()
        await context.close()
        await browser.close()
    await asyncio.sleep(1)
    after = chromium_process_lines()
    print(f"chromium_before={len(before)}")
    print(f"chromium_after={len(after)}")
    if len(after) > len(before):
        print("orphan_chromium_processes_detected")
        for line in after:
            print(line)
        raise SystemExit(1)


asyncio.run(main())
'''


class SmokeFailure(Exception):
    pass


@dataclass
class SmokeResult:
    step: str
    ok: bool
    detail: str = ""


def docker_compose_cmd() -> list[str]:
    if shutil.which("docker"):
        return ["docker", "compose"]
    if shutil.which("docker-compose"):
        return ["docker-compose"]
    raise SmokeFailure("Docker Compose is not installed")


def display_compose_cmd() -> list[str]:
    return ["docker", "compose"]


def run(cmd: list[str], *, timeout: int = 120, check: bool = True, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    result = subprocess.run(
        cmd,
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        timeout=timeout,
        env=env,
    )
    if check and result.returncode != 0:
        raise SmokeFailure(
            "command failed:\n"
            + " ".join(shlex.quote(part) for part in cmd)
            + f"\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def compose_base(project_name: str) -> list[str]:
    return docker_compose_cmd() + ["-p", project_name, "-f", str(COMPOSE_FILE)]


def display_compose_base(project_name: str) -> list[str]:
    return display_compose_cmd() + ["-p", project_name, "-f", str(COMPOSE_FILE)]


def assert_repo_contract() -> None:
    if not COMPOSE_FILE.exists():
        raise SmokeFailure("docker-compose.yml is missing")
    for required in [
        "scripts/runtime_preflight.py",
        "scripts/container_runtime_smoke.py",
        "scripts/controlled_worker_lifecycle_smoke.py",
    ]:
        if not (ROOT / required).exists():
            raise SmokeFailure(f"required smoke dependency missing: {required}")


def assert_env_contract() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        raise SmokeFailure(
            ".env is missing. Create it from .env.example before --execute so compose env_file resolution is explicit."
        )


def run_static_gates(project_name: str, env: dict[str, str]) -> list[SmokeResult]:
    results: list[SmokeResult] = []
    run([sys.executable, "scripts/runtime_preflight.py"], timeout=120, env=env)
    results.append(SmokeResult("runtime_preflight", True))
    run([sys.executable, "scripts/container_runtime_smoke.py"], timeout=120, env=env)
    results.append(SmokeResult("container_runtime_smoke", True))
    run([sys.executable, "scripts/controlled_worker_lifecycle_smoke.py"], timeout=120, env=env)
    results.append(SmokeResult("worker_lifecycle_dry_run", True))
    run(compose_base(project_name) + ["config", "--quiet"], timeout=120, env=env)
    results.append(SmokeResult("compose_config", True))
    return results


def container_id(project_name: str, service: str, env: dict[str, str]) -> str:
    result = run(compose_base(project_name) + ["ps", "-q", service], timeout=30, env=env)
    cid = result.stdout.strip()
    if not cid:
        raise SmokeFailure(f"container id missing for service: {service}")
    return cid


def inspect_health(container: str, env: dict[str, str]) -> str:
    result = run(
        ["docker", "inspect", "--format", "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}", container],
        timeout=30,
        env=env,
    )
    return result.stdout.strip()


def wait_for_health(project_name: str, services: list[str], timeout_seconds: int, env: dict[str, str]) -> list[SmokeResult]:
    deadline = time.time() + timeout_seconds
    pending = set(services)
    results: list[SmokeResult] = []
    while pending and time.time() < deadline:
        for service in list(pending):
            cid = container_id(project_name, service, env)
            status = inspect_health(cid, env)
            if status == "healthy":
                results.append(SmokeResult(f"health:{service}", True, status))
                pending.remove(service)
            elif status in {"exited", "dead", "unhealthy"}:
                raise SmokeFailure(f"service {service} became {status}")
        if pending:
            time.sleep(5)
    if pending:
        raise SmokeFailure(f"health timeout waiting for: {', '.join(sorted(pending))}")
    return results


def docker_up(project_name: str, env: dict[str, str]) -> None:
    run(compose_base(project_name) + ["up", "-d"] + SERVICES, timeout=600, env=env)


def docker_down(project_name: str, env: dict[str, str]) -> None:
    run(compose_base(project_name) + ["down", "-v", "--remove-orphans"], timeout=180, check=False, env=env)


def run_worker_browser_smoke(project_name: str, env: dict[str, str]) -> SmokeResult:
    worker = container_id(project_name, "worker", env)
    result = run(["docker", "exec", worker, "python", "-c", BROWSER_SMOKE_CODE], timeout=120, env=env)
    details = " ".join(line.strip() for line in result.stdout.splitlines() if line.strip())
    return SmokeResult("browser_launch_context_close", True, details)


def dry_run(project_name: str, timeout_seconds: int) -> int:
    base = display_compose_base(project_name)
    print("Controlled browser lifecycle smoke dry-run")
    print(f"project: {project_name}")
    print(f"services: {' '.join(SERVICES)}")
    print(f"timeout_seconds: {timeout_seconds}")
    print("commands:")
    print("  " + " ".join(shlex.quote(part) for part in [sys.executable, "scripts/runtime_preflight.py"]))
    print("  " + " ".join(shlex.quote(part) for part in [sys.executable, "scripts/container_runtime_smoke.py"]))
    print("  " + " ".join(shlex.quote(part) for part in [sys.executable, "scripts/controlled_worker_lifecycle_smoke.py"]))
    print("  " + " ".join(shlex.quote(part) for part in base + ["config", "--quiet"]))
    print("  " + " ".join(shlex.quote(part) for part in base + ["up", "-d"] + SERVICES))
    print("  wait for mysql, redis, core-api, worker health")
    print("  docker exec <worker> python -c '<launch chromium; new_context; close context; close browser; verify no orphan chromium>'")
    print("  " + " ".join(shlex.quote(part) for part in base + ["down", "-v", "--remove-orphans"]))
    print("safety: no account login, no task enqueue, no platform page open, no platform action")
    print("pass --execute to run")
    return 0


def execute(project_name: str, timeout_seconds: int) -> int:
    assert_repo_contract()
    assert_env_contract()
    env = os.environ.copy()
    env.setdefault("REAL_RUN_ENABLED", "false")
    env.setdefault("DEPLOYMENT_MODE", "dev")
    results: list[SmokeResult] = []
    try:
        results.extend(run_static_gates(project_name, env))
        docker_up(project_name, env)
        results.append(SmokeResult("compose_up", True, " ".join(SERVICES)))
        results.extend(wait_for_health(project_name, SERVICES, timeout_seconds, env))
        results.append(run_worker_browser_smoke(project_name, env))
    finally:
        docker_down(project_name, env)
        results.append(SmokeResult("compose_down", True, "down -v --remove-orphans attempted"))
    for item in results:
        print(f"ok: {item.step}" + (f" ({item.detail})" if item.detail else ""))
    print("ok: controlled browser lifecycle smoke passed")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Controlled DPMS browser lifecycle smoke harness")
    parser.add_argument("--execute", action="store_true", help="actually run docker compose up, browser smoke, and cleanup")
    parser.add_argument("--project-name", default=DEFAULT_PROJECT, help="isolated Docker Compose project name")
    parser.add_argument("--timeout-seconds", type=int, default=300, help="health wait timeout")
    args = parser.parse_args()
    try:
        assert_repo_contract()
        if args.execute:
            return execute(args.project_name, args.timeout_seconds)
        return dry_run(args.project_name, args.timeout_seconds)
    except Exception as exc:
        print(f"controlled browser lifecycle smoke failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
