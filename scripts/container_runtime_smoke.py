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
PLATFORMS = ("bilibili", "weibo", "xiaohongshu", "douyin")
PLATFORM_CORE_SERVICES = {
    f"core-{platform}-runner" for platform in PLATFORMS
}
PLATFORM_WORKER_SERVICES = {
    f"worker-{platform}" for platform in PLATFORMS
}
REQUIRED_SERVICES = {
    "profiles-login-init",
    *(f"storage-{platform}-init" for platform in PLATFORMS),
    "nginx",
    "core-api",
    "core-migrate",
    "worker",
    "mysql",
    "redis",
    *PLATFORM_CORE_SERVICES,
    *PLATFORM_WORKER_SERVICES,
}
REQUIRED_HEALTHCHECKS = REQUIRED_SERVICES - {
    "core-migrate",
    "profiles-login-init",
    *(f"storage-{platform}-init" for platform in PLATFORMS),
}
REQUIRED_SERVICE_DEPENDS = {
    "nginx": {"core-api": "service_healthy"},
    "core-api": {
        "mysql": "service_healthy",
        "redis": "service_healthy",
    },
    "worker": {
        "mysql": "service_healthy",
        "redis": "service_healthy",
        "profiles-login-init": "service_completed_successfully",
    },
    **{
        f"core-{platform}-runner": {
            "mysql": "service_healthy",
            "redis": "service_healthy",
            f"storage-{platform}-init": "service_completed_successfully",
        }
        for platform in PLATFORMS
    },
    **{
        f"worker-{platform}": {
            "mysql": "service_healthy",
            "redis": "service_healthy",
            f"storage-{platform}-init": "service_completed_successfully",
        }
        for platform in PLATFORMS
    },
}


class SmokeFailure(Exception):
    pass


def read_compose() -> str:
    if not COMPOSE_FILE.exists():
        raise SmokeFailure("docker-compose.yml is missing")
    return COMPOSE_FILE.read_text(encoding="utf-8")


def service_block(text: str, service: str) -> str:
    match = re.search(
        rf"(?m)^  {re.escape(service)}:"
        rf"(?:\s+&[A-Za-z0-9_-]+)?\s*$",
        text,
    )
    if match is None:
        raise SmokeFailure(f"service missing: {service}")
    start = match.start()
    next_start = len(text)
    other_match = re.search(
        r"(?m)^  [A-Za-z0-9][A-Za-z0-9_.-]*:"
        r"(?:\s+&[A-Za-z0-9_-]+)?\s*$",
        text[start + 1 :],
    )
    if other_match is not None:
        next_start = min(
            next_start,
            start + 1 + other_match.start(),
        )
    for top_level in ("networks", "volumes"):
        top_match = re.search(rf"(?m)^{top_level}:\n", text[start + 1 :])
        if top_match is not None:
            next_start = min(next_start, start + 1 + top_match.start())
    block = text[start:next_start]
    for alias in re.findall(
        r"(?m)^\s+<<:\s+\*([A-Za-z0-9_-]+)\s*$",
        block,
    ):
        anchor_match = re.search(
            rf"(?m)^[^\s].*:\s*&{re.escape(alias)}\s*$",
            text,
        )
        if anchor_match is None:
            raise SmokeFailure(
                f"service {service} references missing anchor {alias}"
            )
        anchor_end_match = re.search(
            r"(?m)^[^\s#].*:\s*(?:&[A-Za-z0-9_-]+\s*)?$",
            text[anchor_match.end() + 1 :],
        )
        anchor_end = (
            len(text)
            if anchor_end_match is None
            else anchor_match.end()
            + 1
            + anchor_end_match.start()
        )
        block += "\n" + text[anchor_match.start() : anchor_end]
    return block


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
    for service, deps in REQUIRED_SERVICE_DEPENDS.items():
        block = service_block(text, service)
        require("depends_on:" in block, f"{service} missing depends_on")
        for dep, condition in deps.items():
            require(
                re.search(
                    rf"(?m)^\s+{re.escape(dep)}:\s*\n"
                    rf"\s+condition:\s+{re.escape(condition)}\s*$",
                    block,
                )
                is not None,
                f"{service}->{dep} must use condition {condition}",
            )


def check_platform_startup_failure_domains(text: str) -> None:
    """Keep platform restarts independent from sibling/control processes."""

    for service in (
        "worker",
        *sorted(PLATFORM_CORE_SERVICES),
        *sorted(PLATFORM_WORKER_SERVICES),
    ):
        block = service_block(text, service)
        depends_on = block.split("depends_on:", 1)[1]
        require(
            re.search(r"(?m)^\s+core-api:\s*$", depends_on) is None,
            f"{service} must not require core-api for startup",
        )
        for peer in sorted(PLATFORM_CORE_SERVICES | PLATFORM_WORKER_SERVICES):
            if peer == service:
                continue
            require(
                re.search(
                    rf"(?m)^\s+{re.escape(peer)}:\s*$",
                    depends_on,
                )
                is None,
                f"{service} must not require peer process {peer} for startup",
            )


def check_worker_container_contract(text: str) -> None:
    block = service_block(text, "worker")
    active_block = "\n".join(line for line in block.splitlines() if not line.lstrip().startswith("#"))
    require("shm_size:" in block, "worker missing shm_size for Chromium stability")
    require('user: "1000:1000"' in block, "worker must use the Playwright non-root UID")
    require("read_only: true" in block, "worker root filesystem must be read-only")
    require("no-new-privileges:true" in active_block, "worker missing no-new-privileges security option")
    require(
        "seccomp=./docker/seccomp/playwright-v1.44.0.json" in active_block,
        "worker must use the pinned Playwright seccomp profile",
    )
    require("- SYS_CHROOT" in active_block, "worker sandbox requires only SYS_CHROOT")
    require("privileged: true" not in active_block, "worker must not run privileged")
    require("SYS_ADMIN" not in active_block, "worker must not add SYS_ADMIN capability")
    browser_pool = (
        ROOT / "worker" / "app" / "browser_pool.py"
    ).read_text(encoding="utf-8")
    require(
        'CHROMIUM_SANDBOX_IGNORED_DEFAULT_ARGS = ["--no-sandbox"]'
        in browser_pool
        and "ignore_default_args=CHROMIUM_SANDBOX_IGNORED_DEFAULT_ARGS"
        in browser_pool,
        "Playwright default --no-sandbox argument must be removed",
    )


def check_core_container_contract(text: str) -> None:
    for service in ("core-api", *sorted(PLATFORM_CORE_SERVICES)):
        block = service_block(text, service)
        require(
            'user: "65532:65532"' in block,
            f"{service} must use the dedicated non-root UID",
        )
        require(
            "read_only: true" in block,
            f"{service} root filesystem must be read-only",
        )
        require(
            "cap_drop:" in block and "- ALL" in block,
            f"{service} must drop all capabilities",
        )
        require(
            "no-new-privileges:true" in block,
            f"{service} must set no-new-privileges",
        )


def check_platform_isolation_contract(text: str) -> None:
    worker = service_block(text, "worker")
    require(
        "DPMS_WORKER_ROLE: control" in worker,
        "stable worker service name must own only control loops",
    )
    require(
        "DPMS_WORKER_INSTANCE_ID:" not in worker,
        "control worker identity must be unique per process instance",
    )
    require(
        "DPMS_WORKER_ROLE: all" not in worker,
        "default Compose must not start the compatibility monolith",
    )
    require(
        not re.search(r"(?m)^  worker-(?:control|monolith):\n", text),
        "default Compose must not retain a second control/monolith worker",
    )
    for platform in PLATFORMS:
        core_name = f"core-{platform}-runner"
        worker_name = f"worker-{platform}"
        core = service_block(text, core_name)
        platform_worker = service_block(text, worker_name)
        require(
            f"DPMS_PLATFORM_SCOPE: {platform}" in core,
            f"{core_name} must use exact platform scope",
        )
        require(
            "DPMS_CORE_RUNNER_INSTANCE_ID:" not in core,
            f"{core_name} identity must be unique per process instance",
        )
        require(
            "DPMS_WORKER_ROLE: platform" in platform_worker
            and f"DPMS_PLATFORM_SCOPE: {platform}" in platform_worker,
            f"{worker_name} must own only its exact platform lanes",
        )
        require(
            "DPMS_WORKER_INSTANCE_ID:" not in platform_worker,
            f"{worker_name} identity must be unique per process instance",
        )
        require(
            f"{core_name}:" not in platform_worker,
            f"{worker_name} startup must remain independent from its Core runner",
        )
        for peer in PLATFORM_CORE_SERVICES - {core_name}:
            require(
                f"{peer}:" not in platform_worker,
                f"{worker_name} must not depend on peer runner {peer}",
            )


def check_redis_acl_contract(text: str) -> None:
    core = service_block(text, "core-api")
    worker = service_block(text, "worker")
    platform_core = service_block(text, "core-bilibili-runner")
    platform_worker = service_block(text, "worker-bilibili")
    redis = service_block(text, "redis")
    require("REDIS_USERNAME: core" in core, "core-api must use the core Redis ACL user")
    require("REDIS_CORE_PASSWORD" in core, "core-api Redis ACL password is not wired")
    require(
        'REDIS_ACL_PREFLIGHT_REQUIRED: "true"' in core,
        "core-api Redis ACL preflight must be enabled",
    )
    require("REDIS_USERNAME: worker" in worker, "worker must use the worker Redis ACL user")
    require("REDIS_WORKER_PASSWORD" in worker, "worker Redis ACL password is not wired")
    require(
        'REDIS_ACL_PREFLIGHT_REQUIRED: "true"' in worker,
        "worker Redis ACL preflight must be enabled",
    )
    require(
        "./docker/redis/entrypoint.sh:/usr/local/bin/dpms-redis-entrypoint.sh:ro"
        in redis,
        "redis ACL entrypoint must be mounted read-only",
    )
    require("--user health" in redis, "redis healthcheck must use its named ACL user")
    require("REDIS_HEALTH_PASSWORD" in redis, "redis health ACL password is not wired")
    require(
        "REDIS_GROUP_ADMIN_PASSWORD" in redis,
        "redis group-admin ACL password is not wired",
    )
    require(
        "DEPLOYMENT_MODE" in redis,
        "redis production secret guard must receive deployment mode",
    )
    require(
        "REDIS_GROUP_ADMIN_PASSWORD" not in core
        and "REDIS_GROUP_ADMIN_PASSWORD" not in worker
        and "REDIS_GROUP_ADMIN_PASSWORD" not in platform_core
        and "REDIS_GROUP_ADMIN_PASSWORD" not in platform_worker,
        "group-admin password must not be injected into runtime services",
    )
    for runtime_name, runtime_block in (
        ("core-api", core),
        ("core-bilibili-runner", platform_core),
        ("worker", worker),
        ("worker-bilibili", platform_worker),
    ):
        require(
            "env_file:" not in runtime_block,
            f"{runtime_name} must use an explicit environment allowlist",
        )
        forbidden_redis_secrets = {
            "core-api": (
                "REDIS_WORKER_PASSWORD",
                "REDIS_HEALTH_PASSWORD",
                "REDIS_GROUP_ADMIN_PASSWORD",
            ),
            "core-bilibili-runner": (
                "REDIS_WORKER_PASSWORD",
                "REDIS_HEALTH_PASSWORD",
                "REDIS_GROUP_ADMIN_PASSWORD",
            ),
            "worker": (
                "REDIS_CORE_PASSWORD",
                "REDIS_HEALTH_PASSWORD",
                "REDIS_GROUP_ADMIN_PASSWORD",
            ),
            "worker-bilibili": (
                "REDIS_CORE_PASSWORD",
                "REDIS_HEALTH_PASSWORD",
                "REDIS_GROUP_ADMIN_PASSWORD",
            ),
        }[runtime_name]
        require(
            not any(
                variable in runtime_block
                for variable in forbidden_redis_secrets
            ),
            f"{runtime_name} receives another Redis role's secret",
        )
    require(
        "REDIS_GROUP_ADMIN_URL" not in text,
        "group-admin URL must remain a short-lived operator secret",
    )
    require(
        "./docker/redis/consumer-groups.tsv:/usr/local/share/dpms/consumer-groups.tsv:ro"
        in redis,
        "fixed Redis consumer-group topology must be mounted read-only",
    )
    for contract in (
        "read_only: true",
        "no-new-privileges:true",
        "cap_drop:",
        "- ALL",
        "cap_add:",
        "- CHOWN",
        "- DAC_READ_SEARCH",
        "- SETGID",
        "- SETUID",
    ):
        require(contract in redis, f"redis security contract missing {contract}")

    entrypoint = ROOT / "docker" / "redis" / "entrypoint.sh"
    require(entrypoint.exists(), "docker/redis/entrypoint.sh is missing")
    acl_text = entrypoint.read_text(encoding="utf-8")
    for contract in (
        "user default off",
        "user health on",
        "user core on",
        "user worker on",
        "user bootstrap off",
        "user group-admin on",
        "+acl|whoami",
        "+acl|dryrun",
        "--reuid redis",
        "--aclfile",
        "validate_production_password",
        "chown -h redis:redis",
        "Redis ACL passwords must be mutually distinct",
    ):
        require(contract in acl_text, f"redis ACL entrypoint missing {contract}")
    for password_name, development_value in (
        ("REDIS_CORE_PASSWORD", "dpms-core-local-only-change-me-2026"),
        (
            "REDIS_WORKER_PASSWORD",
            "dpms-worker-local-only-change-me-2026",
        ),
        (
            "REDIS_HEALTH_PASSWORD",
            "dpms-health-local-only-change-me-2026",
        ),
        (
            "REDIS_GROUP_ADMIN_PASSWORD",
            "dpms-group-admin-local-only-change-me-2026",
        ),
    ):
        require(
            f"validate_production_password \\\n  {password_name}" in acl_text
            and development_value in acl_text,
            f"{password_name} production guard is incomplete",
        )
    acl_lines = {
        line.split()[1]: line
        for line in acl_text.splitlines()
        if line.startswith("user ")
    }
    for role in ("core", "worker"):
        runtime_acl = acl_lines[role].casefold()
        for forbidden_grant in (
            "+flushdb",
            "+flushall",
            "+xgroup|create",
            "+xgroup|destroy",
            "+xgroup|setid",
        ):
            require(
                forbidden_grant not in runtime_acl,
                f"runtime Redis ACL grants forbidden command {forbidden_grant}",
            )
    require(
        "+xgroup|delconsumer" in acl_lines["worker"].casefold(),
        "worker ACL must retain only bounded stale-consumer cleanup",
    )
    core_acl = acl_lines["core"].casefold()
    require(
        "+xgroup|delconsumer" in core_acl,
        "core ACL must clean only its discovery/notification consumers",
    )
    core_delconsumer_selector = next(
        selector
        for selector in core_acl.split(") ")
        if selector.startswith("(+xgroup|delconsumer ")
    )
    require(
        "~notify_events" in core_delconsumer_selector.split()
        and "~discovery_scan_requests:v1:*"
        in core_delconsumer_selector.split()
        and "~lottery_tasks" not in core_delconsumer_selector.split()
        and "~login_requests" not in core_delconsumer_selector.split(),
        "core DELCONSUMER selector must be control-lane scoped",
    )
    worker_acl = acl_lines["worker"].casefold()
    require(
        "(+eval +xadd" not in worker_acl,
        "worker XADD must not inherit the full runtime stream selector",
    )
    xadd_selector = next(
        selector
        for selector in worker_acl.split(") ")
        if selector.startswith("(+xadd ")
    )
    for required_stream in (
        "~notify_events",
        "~failed_task_messages",
        "~adapter_probe_requests:*",
        "~account_calibration_requests:*",
    ):
        require(
            required_stream in xadd_selector.split(),
            f"worker XADD selector missing {required_stream}",
        )
    for forbidden_stream in (
        "~lottery_tasks",
        "~lottery_tasks:*",
        "~lottery_repair_tasks:v1:*",
        "~login_requests",
        "~discovery_scan_requests:v1:*",
    ):
        require(
            forbidden_stream not in xadd_selector.split(),
            f"worker XADD must not target {forbidden_stream}",
        )
    bootstrap_acl = next(
        line
        for line in acl_text.splitlines()
        if line.startswith("user bootstrap on")
    ).casefold()
    require(
        "+xgroup|create" in bootstrap_acl
        and "+xgroup|destroy" not in bootstrap_acl,
        "one-time bootstrap must own CREATE but not destructive XGROUP commands",
    )
    group_admin_acl = acl_lines["group-admin"].casefold()
    for required_grant in (
        "+eval",
        "+xinfo|groups",
        "+xinfo|consumers",
        "+xpending",
        "+xrange",
        "+xdel",
        "+xgroup|destroy",
        "+xgroup|delconsumer",
    ):
        require(
            required_grant in group_admin_acl,
            f"group-admin ACL missing retirement grant {required_grant}",
        )
    require(
        "+xgroup|create" not in group_admin_acl,
        "group-admin must not create consumer groups",
    )
    require(
        " ~*" not in acl_text,
        "redis ACL entrypoint must not grant an unrestricted key pattern",
    )


def check_volume_contract(text: str) -> None:
    core = service_block(text, "core-api")
    worker = service_block(text, "worker")
    mysql = service_block(text, "mysql")
    login_init = service_block(text, "profiles-login-init")
    require(
        "./browser-profiles/login-sessions:/profiles/login-sessions:ro"
        in core,
        "core-api must read the isolated login profile root",
    )
    require(
        "./browser-profiles/login-sessions:/profiles/login-sessions"
        in worker,
        "control worker must own only the login profile root",
    )
    require(
        "/evidence/" not in worker,
        "control worker must not mount any platform evidence root",
    )
    require(
        "./browser-profiles/login-sessions:/profiles/login-sessions"
        in login_init
        and "/evidence/" not in login_init,
        "login init must touch only the shared login profile root",
    )
    require(
        ".dpms-storage-permissions-v1" in text
        and 'if [ ! -f "$$profile_marker" ]' in text
        and 'if [ ! -f "$$evidence_marker" ]' in text
        and 'if [ ! -f "$$permission_marker" ]' in text,
        "storage permission migration must be guarded by root-local markers",
    )
    for artifact_mount in (
        "./releases:/app/releases:ro",
        "./backups:/app/backups:ro",
        "./logs:/app/logs:ro",
    ):
        require(
            artifact_mount in core,
            f"immutable Core must mount operator artifact read-only: {artifact_mount}",
        )
    require(
        all(
            source not in text
            for source in (
                "./core/app:/app/app",
                "./core/migrations:/app/migrations",
                "./worker/app:/app/app",
                "./shared:/app/shared",
            )
        ),
        "runtime services must execute one immutable image source tree",
    )
    for platform in PLATFORMS:
        storage_init = service_block(
            text,
            f"storage-{platform}-init",
        )
        core_runner = service_block(
            text,
            f"core-{platform}-runner",
        )
        platform_worker = service_block(
            text,
            f"worker-{platform}",
        )
        require(
            f"./browser-profiles/{platform}:/profiles/{platform}"
            in platform_worker,
            f"worker-{platform} must mount only its profile root",
        )
        require(
            f"evidence-{platform}:/evidence/{platform}"
            in platform_worker,
            f"worker-{platform} must mount only its evidence volume",
        )
        require(
            f"./browser-profiles/{platform}:/profiles/{platform}"
            in storage_init
            and f"evidence-{platform}:/evidence/{platform}"
            in storage_init,
            f"storage-{platform}-init must touch only its platform roots",
        )
        require(
            f"evidence-{platform}:/evidence/{platform}:ro"
            in core_runner,
            f"core-{platform}-runner must read only its evidence volume",
        )
        for peer in set(PLATFORMS) - {platform}:
            require(
                f"/profiles/{peer}" not in platform_worker
                and f"/evidence/{peer}" not in platform_worker,
                f"worker-{platform} must not mount {peer} artifacts",
            )
            require(
                f"/evidence/{peer}" not in core_runner,
                f"core-{platform}-runner must not mount {peer} evidence",
            )
            require(
                f"/profiles/{peer}" not in storage_init
                and f"/evidence/{peer}" not in storage_init,
                f"storage-{platform}-init must not touch {peer}",
            )
        require(
            f"evidence-{platform}:/evidence/{platform}:ro" in core,
            f"core-api must read {platform} evidence",
        )
        require(
            f"./browser-profiles/{platform}:/profiles/{platform}:ro"
            in core,
            f"core-api must read {platform} calibration artifacts",
        )
    require(
        "dockerfile: docker/mysql/Dockerfile" in mysql,
        "mysql must bake role bootstrap into its managed image",
    )


def check_mysql_role_contract(text: str) -> None:
    mysql = service_block(text, "mysql")
    core = service_block(text, "core-api")
    worker = service_block(text, "worker")
    platform_core = service_block(text, "core-bilibili-runner")
    platform_worker = service_block(text, "worker-bilibili")
    migrate = service_block(text, "core-migrate")
    for variable in (
        "MYSQL_ROOT_PASSWORD",
        "MYSQL_RUNTIME_USER",
        "MYSQL_RUNTIME_PASSWORD",
        "MYSQL_MIGRATION_USER",
        "MYSQL_MIGRATION_PASSWORD",
        "DEPLOYMENT_MODE",
    ):
        require(variable in mysql, f"mysql missing role environment {variable}")
    require(
        "mysqladmin ping --protocol=socket -uroot" in mysql,
        "mysql health must work before runtime roles exist on an old volume",
    )
    require(
        "MYSQL_RUNTIME_PASSWORD" in core
        and "MYSQL_RUNTIME_PASSWORD" in worker,
        "runtime processes must use the runtime MySQL role",
    )
    require(
        "MYSQL_MIGRATION_PASSWORD" in migrate
        and "MYSQL_RUNTIME_PASSWORD" not in migrate,
        "explicit migration command must use only the migration role",
    )
    require(
        "MYSQL_ROOT_PASSWORD" not in core
        and "MYSQL_ROOT_PASSWORD" not in worker
        and "MYSQL_ROOT_PASSWORD" not in platform_core
        and "MYSQL_ROOT_PASSWORD" not in platform_worker
        and "MYSQL_ROOT_PASSWORD" not in migrate,
        "MySQL root credentials must never enter application containers",
    )
    for runtime_name, runtime_block in (
        ("core-api", core),
        ("core-bilibili-runner", platform_core),
        ("worker", worker),
        ("worker-bilibili", platform_worker),
    ):
        require(
            "MYSQL_MIGRATION_PASSWORD" not in runtime_block
            and "MYSQL_MIGRATION_USER" not in runtime_block,
            f"{runtime_name} must not receive the migration MySQL role",
        )
        require(
            "env_file:" not in runtime_block,
            f"{runtime_name} must not import an unbounded environment file",
        )

    dockerfile = (
        ROOT / "docker" / "mysql" / "Dockerfile"
    ).read_text(encoding="utf-8")
    provision = (
        ROOT / "docker" / "mysql" / "provision-roles.sh"
    ).read_text(encoding="utf-8")
    entrypoint = (
        ROOT / "docker" / "mysql" / "entrypoint.sh"
    ).read_text(encoding="utf-8")
    require(
        "ENTRYPOINT" in dockerfile
        and "dpms-mysql-entrypoint" in dockerfile
        and "DPMS_MYSQL_VALIDATE_ONLY=1" in entrypoint,
        "MySQL must validate production role secrets on every volume start",
    )
    for contract in (
        "REVOKE ALL PRIVILEGES, GRANT OPTION",
        "GRANT SELECT, INSERT, UPDATE, DELETE, EXECUTE",
        "GRANT ALL PRIVILEGES",
        "mysql_runtime_password_is_development_default",
        "mysql_migration_password_is_development_default",
        "mysql_root_password_is_missing_default_or_short",
    ):
        require(
            contract in provision,
            f"MySQL role provisioner missing {contract}",
        )


def check_build_context_contract() -> None:
    dockerignore = ROOT / ".dockerignore"
    require(dockerignore.exists(), ".dockerignore is required when services build from repo root")
    text = dockerignore.read_text(encoding="utf-8")
    for pattern in [".env", ".venv/", "frontend/node_modules/", "browser-profiles/", "logs/"]:
        require(pattern in text, f".dockerignore missing {pattern}")


def check_frontend_chunk_recovery_contract() -> None:
    nginx = (ROOT / "nginx.conf").read_text(encoding="utf-8")
    main = (ROOT / "frontend" / "src" / "main.jsx").read_text(
        encoding="utf-8"
    )
    recovery = (
        ROOT / "frontend" / "src" / "preloadRecovery.js"
    ).read_text(encoding="utf-8")
    require(
        "location = /index.html" in nginx
        and 'Cache-Control "no-cache"' in nginx,
        "index.html must revalidate after a chunked frontend deployment",
    )
    api_location = nginx.split("location /api", 1)[1].split(
        "location /uploads",
        1,
    )[0]
    require(
        "proxy_read_timeout 150s;" in api_location
        and "proxy_send_timeout 150s;" in api_location,
        "API proxy timeout must exceed the 135s durable manual-scan wait",
    )
    require(
        "installPreloadErrorRecovery(window)" in main,
        "frontend must install dynamic chunk recovery",
    )
    require(
        "'vite:preloadError'" in recovery
        and "PRELOAD_RELOAD_SCOPES_KEY" in recovery
        and "previousScopes.has(scope)" in recovery,
        "dynamic chunk recovery must remain bounded against reload loops",
    )


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
        check_platform_startup_failure_domains(text)
        print("ok: process startup failure-domain isolation")
        check_worker_container_contract(text)
        print("ok: worker container contract")
        check_core_container_contract(text)
        print("ok: core container contract")
        check_platform_isolation_contract(text)
        print("ok: platform isolation contract")
        check_redis_acl_contract(text)
        print("ok: redis ACL contract")
        check_volume_contract(text)
        print("ok: volume contract")
        check_mysql_role_contract(text)
        print("ok: MySQL role contract")
        check_build_context_contract()
        print("ok: build context contract")
        check_frontend_chunk_recovery_contract()
        print("ok: frontend chunk recovery contract")
        compose_status = run_compose_config(args.skip_docker)
        print(f"ok: compose config: {compose_status}")
    except Exception as exc:
        print(f"container runtime smoke failed: {exc}", file=sys.stderr)
        return 1
    print("ok: container runtime smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
