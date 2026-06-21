#!/usr/bin/env python3
"""Controlled DPMS worker lifecycle smoke harness.

Default mode is dry-run. It prints the exact Docker Compose commands and safety
contract without starting containers. Pass --execute to run the smoke.

The execute mode uses an isolated Compose project name and removes project
volumes on shutdown. It does not create accounts, login accounts, enqueue tasks,
open platform pages, or perform platform actions.
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
DEFAULT_PROJECT = "dpms-worker-smoke"
SERVICES = ["mysql", "redis", "core-api", "worker"]


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


def assert_repo_contract() -> None:
    if not COMPOSE_FILE.exists():
        raise SmokeFailure("docker-compose.yml is missing")
    for required in ["scripts/runtime_preflight.py", "scripts/container_runtime_smoke.py"]:
        if not (ROOT / required).exists():
            raise SmokeFailure(f"required preflight script missing: {required}")


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


def verify_worker_heartbeat(project_name: str, env: dict[str, str]) -> SmokeResult:
    cid = container_id(project_name, "worker", env)
    result = run(
        ["docker", "exec", cid, "python", "-c", "import os,sys,time;p='/tmp/worker-health';sys.exit(0 if os.path.exists(p) and time.time()-os.path.getmtime(p)<90 else 1)"],
        timeout=30,
        env=env,
    )
    return SmokeResult("worker_health_file", result.returncode == 0)


def docker_up(project_name: str, env: dict[str, str]) -> None:
    run(compose_base(project_name) + ["up", "-d"] + SERVICES, timeout=600, env=env)


def docker_down(project_name: str, env: dict[str, str]) -> None:
    run(compose_base(project_name) + ["down", "-v", "--remove-orphans"], timeout=180, check=False, env=env)


def dry_run(project_name: str, timeout_seconds: int) -> int:
    base = compose_base(project_name)
    print("Controlled worker lifecycle smoke dry-run")
    print(f"project: {project_name}")
    print(f"services: {' '.join(SERVICES)}")
    print(f"timeout_seconds: {timeout_seconds}")
    print("commands:")
    print("  " + " ".join(shlex.quote(part) for part in [sys.executable, "scripts/runtime_preflight.py"]))
    print("  " + " ".join(shlex.quote(part) for part in [sys.executable, "scripts/container_runtime_smoke.py"]))
    print("  " + " ".join(shlex.quote(part) for part in base + ["config", "--quiet"]))
    print("  " + " ".join(shlex.quote(part) for part in base + ["up", "-d"] + SERVICES))
    print("  wait for mysql, redis, core-api, worker health")
    print("  verify /tmp/worker-health inside worker")
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
        results.append(verify_worker_heartbeat(project_name, env))
    finally:
        docker_down(project_name, env)
        results.append(SmokeResult("compose_down", True, "down -v --remove-orphans attempted"))
    for item in results:
        print(f"ok: {item.step}" + (f" ({item.detail})" if item.detail else ""))
    print("ok: controlled worker lifecycle smoke passed")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Controlled DPMS worker lifecycle smoke harness")
    parser.add_argument("--execute", action="store_true", help="actually run docker compose up/down")
    parser.add_argument("--project-name", default=DEFAULT_PROJECT, help="isolated Docker Compose project name")
    parser.add_argument("--timeout-seconds", type=int, default=300, help="health wait timeout")
    args = parser.parse_args()
    try:
        assert_repo_contract()
        if args.execute:
            return execute(args.project_name, args.timeout_seconds)
        return dry_run(args.project_name, args.timeout_seconds)
    except Exception as exc:
        print(f"controlled worker lifecycle smoke failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
