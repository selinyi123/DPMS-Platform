#!/usr/bin/env python3
"""DPMS container runtime smoke checks.

Default mode is static and safe: it validates docker-compose.yml wiring and, when
Docker Compose is installed, runs `docker compose config --quiet`.

It does not start platform actions, login accounts, open target pages, or dispatch
lottery tasks. Use this before a real container startup smoke.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = ROOT / "docker-compose.yml"
REQUIRED_SERVICES = {"nginx", "core-api", "worker", "mysql", "redis"}
REQUIRED_HEALTHCHECKS = REQUIRED_SERVICES
REQUIRED_SERVICE_HEALTH_DEPENDS = {
    "nginx": ["core-api"],
    "core-api": ["mysql", "redis"],
    "worker": ["core-api"],
}


class SmokeFailure(Exception):
    pass


def read_compose() -> str:
    if not COMPOSE_FILE.exists():
        raise SmokeFailure("docker-compose.yml is missing")
    return COMPOSE_FILE.read_text(encoding="utf-8")


def service_block(text: str, service: str) -> str:
    match = re.search(rf"(?m)^  {re.escape(service)}:\n", text)
    if match is None:
        raise SmokeFailure(f"service missing: {service}")
    start = match.start()
    next_start = len(text)
    for other in REQUIRED_SERVICES - {service}:
        other_match = re.search(rf"(?m)^  {re.escape(other)}:\n", text[start + 1 :])
        if other_match is not None:
            next_start = min(next_start, start + 1 + other_match.start())
    for top_level in ("networks", "volumes"):
        top_match = re.search(rf"(?m)^{top_level}:\n", text[start + 1 :])
        if top_match is not None:
            next_start = min(next_start, start + 1 + top_match.start())
    return text[start:next_start]


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)


def check_services(text: str) -> None:
    for service in sorted(REQUIRED_SERVICES):
        service_block(text, service)


def check_healthchecks(text: str) -> None:
    for service in sorted(REQUIRED_HEALTHCHECKS):
        block = service_block(text, service)
        require("healthcheck:" in block, f"{service} missing healthcheck")
        require("interval:" in block, f"{service} healthcheck missing interval")
        require("timeout:" in block, f"{service} healthcheck missing timeout")
        require("retries:" in block, f"{service} healthcheck missing retries")


def check_depends_on_health(text: str) -> None:
    for service, deps in REQUIRED_SERVICE_HEALTH_DEPENDS.items():
        block = service_block(text, service)
        require("depends_on:" in block, f"{service} missing depends_on")
        for dep in deps:
            require(f"{dep}:" in block, f"{service} missing dependency {dep}")
            dep_pos = block.find(f"{dep}:")
            condition_pos = block.find("condition: service_healthy", dep_pos)
            require(condition_pos >= 0, f"{service}->{dep} must use condition: service_healthy")


def check_worker_container_contract(text: str) -> None:
    block = service_block(text, "worker")
    active_block = "\n".join(line for line in block.splitlines() if not line.lstrip().startswith("#"))
    require("shm_size:" in block, "worker missing shm_size for Chromium stability")
    require("no-new-privileges:true" in active_block, "worker missing no-new-privileges security option")
    require("privileged: true" not in active_block, "worker must not run privileged")
    require("SYS_ADMIN" not in active_block, "worker must not add SYS_ADMIN capability")


def check_volume_contract(text: str) -> None:
    core = service_block(text, "core-api")
    worker = service_block(text, "worker")
    mysql = service_block(text, "mysql")
    require("./browser-profiles:/profiles" in core, "core-api must mount browser profiles")
    require("./browser-profiles:/profiles" in worker, "worker must mount browser profiles")
    require("./releases:/app/releases" in core, "core-api must mount releases for managed updates")
    require("./core/app:/app/app" in core, "core-api dev bind mount changed; hot-update guard depends on this boundary")
    require(
        "./docker/mysql/001-bootstrap.sql:/docker-entrypoint-initdb.d/001-bootstrap.sql:ro" in mysql,
        "mysql must mount the empty-database bootstrap SQL",
    )


def check_build_context_contract() -> None:
    dockerignore = ROOT / ".dockerignore"
    require(dockerignore.exists(), ".dockerignore is required when services build from repo root")
    text = dockerignore.read_text(encoding="utf-8")
    for pattern in [".env", ".venv/", "frontend/node_modules/", "browser-profiles/", "logs/"]:
        require(pattern in text, f".dockerignore missing {pattern}")


def docker_compose_cmd() -> list[str] | None:
    if shutil.which("docker"):
        return ["docker", "compose"]
    if shutil.which("docker-compose"):
        return ["docker-compose"]
    return None


def run_compose_config(skip_docker: bool) -> str:
    if skip_docker:
        return "skipped"
    cmd = docker_compose_cmd()
    if not cmd:
        return "docker-not-installed"
    result = subprocess.run(
        cmd + ["-f", str(COMPOSE_FILE), "config", "--quiet"],
        cwd=str(ROOT),
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        raise SmokeFailure(f"docker compose config failed:\n{result.stdout}\n{result.stderr}")
    return "ok"


def main() -> int:
    parser = argparse.ArgumentParser(description="DPMS container runtime smoke checks")
    parser.add_argument("--skip-docker", action="store_true", help="skip docker compose config --quiet")
    args = parser.parse_args()

    try:
        text = read_compose()
        check_services(text)
        print("ok: required services")
        check_healthchecks(text)
        print("ok: healthchecks")
        check_depends_on_health(text)
        print("ok: service_healthy dependencies")
        check_worker_container_contract(text)
        print("ok: worker container contract")
        check_volume_contract(text)
        print("ok: volume contract")
        check_build_context_contract()
        print("ok: build context contract")
        compose_status = run_compose_config(args.skip_docker)
        print(f"ok: compose config: {compose_status}")
    except Exception as exc:
        print(f"container runtime smoke failed: {exc}", file=sys.stderr)
        return 1
    print("ok: container runtime smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
