"""Headless Core business runner for one exact platform.

The shared ``core-api`` remains the HTTP/control-plane process.  This entrypoint
owns only discovery, recovery and durable Outbox lanes for the platform named
by ``DPMS_PLATFORM_SCOPE``.
"""

from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path

from app.config import settings
from app.db import database, redis
from app.services.outbox import (
    outbox_reconciliation_startup_phase_seconds,
    reconcile_owned_stream_epochs,
    start_outbox_dispatcher,
)
from app.platform_modules import get_platform_module
from app.migrations_runner import verify_migrations_current
from app.services.discovery_requests import discovery_scan_request_loop
from app.services.recovery_daemon import start_recovery_daemon
from app.services.scheduler import scheduler_loop
from app.utils.log import structured_log
from shared.platform_scope import exact_platform_scope
from shared.redis_acl import verify_redis_acl
from shared.database_credentials import require_production_database_credentials
from shared.runtime_secrets import require_production_encryption_key
from shared.platform_security import require_platform_runtime_identity


PLATFORM_RUNNER_HEALTH_INTERVAL_SECONDS = 10
PLATFORM_RUNNER_HEALTH_PROBE_TIMEOUT_SECONDS = 5


def selected_platform() -> str:
    return exact_platform_scope(
        os.getenv("DPMS_PLATFORM_SCOPE", "")
    )


def validate_platform_runner_instance_identity(
    deployment_mode: str,
    configured_instance_id: str | None = None,
) -> None:
    """Rolling production replicas must never share one consumer identity."""

    explicit = str(
        configured_instance_id
        if configured_instance_id is not None
        else os.getenv("DPMS_CORE_RUNNER_INSTANCE_ID", "")
    ).strip()
    if (
        str(deployment_mode or "").strip().casefold() == "production"
        and explicit
    ):
        raise RuntimeError(
            "core_runner_fixed_instance_id_forbidden_in_production"
        )


def platform_runner_health_file(platform: str) -> Path:
    return Path(f"/tmp/core-platform-runner-{platform}-health")


def preflight_core_platform_module(platform: str) -> None:
    """Import and validate only this runner's exact platform module."""

    platform_module = get_platform_module(platform)
    if (
        platform_module is None
        or platform_module.platform_id != platform
    ):
        raise RuntimeError(
            f"core_platform_preflight_failed:{platform}"
        )


async def reconcile_platform_runner_startup_streams(
    platform: str,
) -> int:
    """Phase strict startup continuity work across platform deployments."""

    phase_seconds = outbox_reconciliation_startup_phase_seconds(
        platform
    )
    if phase_seconds:
        await asyncio.sleep(phase_seconds)
    return await reconcile_owned_stream_epochs(
        platforms=(platform,),
        include_shared=False,
        require_all_owned_lanes=True,
    )


async def platform_runner_heartbeat(
    shutdown_event: asyncio.Event,
    *,
    platform: str,
    supervised_tasks: tuple[asyncio.Task, ...],
) -> None:
    if not supervised_tasks:
        raise ValueError("platform_runner_supervised_tasks_required")
    marker = platform_runner_health_file(platform)
    marker.unlink(missing_ok=True)
    while not shutdown_event.is_set():
        if any(task.done() for task in supervised_tasks):
            marker.unlink(missing_ok=True)
            raise RuntimeError(
                f"platform_runner_owned_loop_exited:{platform}"
            )
        try:
            db_row = await asyncio.wait_for(
                database.fetch_one("SELECT 1 AS healthy"),
                timeout=PLATFORM_RUNNER_HEALTH_PROBE_TIMEOUT_SECONDS,
            )
            if db_row is None:
                raise RuntimeError(
                    "platform_runner_database_probe_empty"
                )
            redis_ready = await asyncio.wait_for(
                redis.ping(),
                timeout=PLATFORM_RUNNER_HEALTH_PROBE_TIMEOUT_SECONDS,
            )
            if not redis_ready:
                raise RuntimeError(
                    "platform_runner_redis_probe_unhealthy"
                )
            if any(task.done() for task in supervised_tasks):
                raise RuntimeError(
                    f"platform_runner_owned_loop_exited:{platform}"
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            marker.unlink(missing_ok=True)
            structured_log(
                "error",
                "core_platform_runner_health_probe_failed",
                platform=platform,
                cause_type=type(exc).__name__,
            )
        else:
            marker.write_text("ok", encoding="utf-8")
        try:
            await asyncio.wait_for(
                shutdown_event.wait(),
                timeout=PLATFORM_RUNNER_HEALTH_INTERVAL_SECONDS,
            )
        except asyncio.TimeoutError:
            pass


def start_platform_runner_tasks(
    shutdown_event: asyncio.Event,
    *,
    platform: str,
) -> tuple[asyncio.Task, ...]:
    scope = (platform,)
    owned_tasks = (
        asyncio.create_task(
            start_recovery_daemon(
                platforms=scope,
                include_shared=False,
                fail_closed=True,
            ),
            name=f"core-platform:{platform}:recovery",
        ),
        asyncio.create_task(
            start_outbox_dispatcher(
                platforms=scope,
                include_shared=False,
            ),
            name=f"core-platform:{platform}:outbox",
        ),
        asyncio.create_task(
            scheduler_loop(
                platforms=scope,
                include_global=False,
                fail_closed=True,
            ),
            name=f"core-platform:{platform}:scheduler",
        ),
        asyncio.create_task(
            discovery_scan_request_loop(platform),
            name=f"core-platform:{platform}:manual-discovery",
        ),
    )
    heartbeat_task = asyncio.create_task(
        platform_runner_heartbeat(
            shutdown_event,
            platform=platform,
            supervised_tasks=owned_tasks,
        ),
        name=f"core-platform:{platform}:heartbeat",
    )
    return (
        *owned_tasks,
        heartbeat_task,
    )


async def supervise_platform_runner_tasks(
    tasks: tuple[asyncio.Task, ...],
    shutdown_event: asyncio.Event,
) -> None:
    if not tasks:
        raise ValueError("platform_runner_tasks_required")
    shutdown_waiter = asyncio.create_task(
        shutdown_event.wait(),
        name="core-platform:shutdown-waiter",
    )
    try:
        done, _pending = await asyncio.wait(
            (*tasks, shutdown_waiter),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if shutdown_waiter in done or shutdown_event.is_set():
            return
        completed = next(task for task in tasks if task in done)
        try:
            completed.result()
        except asyncio.CancelledError as exc:
            raise RuntimeError(
                f"platform_runner_task_cancelled:{completed.get_name()}"
            ) from exc
        except BaseException:
            raise
        raise RuntimeError(
            f"platform_runner_task_returned:{completed.get_name()}"
        )
    finally:
        shutdown_event.set()
        if not shutdown_waiter.done():
            shutdown_waiter.cancel()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(
            shutdown_waiter,
            *tasks,
            return_exceptions=True,
        )


async def main() -> None:
    platform = selected_platform()
    # Container restarts preserve the writable layer. Clear the last process'
    # marker before any import or dependency preflight can fail, otherwise the
    # compose mtime grace window can advertise a dead lane as healthy.
    platform_runner_health_file(platform).unlink(missing_ok=True)
    preflight_core_platform_module(platform)
    deployment_mode = str(settings.deployment_mode or "").strip().casefold()
    validate_platform_runner_instance_identity(deployment_mode)
    require_production_database_credentials(
        settings.database_url,
        deployment_mode=deployment_mode,
        role="runtime",
        expected_username=settings.mysql_runtime_user,
    )
    require_production_encryption_key(
        settings.encryption_key,
        deployment_mode=deployment_mode,
    )
    require_platform_runtime_identity(
        platform=platform,
        role="core",
        deployment_mode=deployment_mode,
        security_mode=settings.platform_security_mode,
        database_username=settings.mysql_runtime_user,
        redis_username=settings.redis_expected_username,
        encryption_key=settings.encryption_key,
    )
    try:
        await database.connect()
        await redis.initialize(
            platforms=(platform,),
            include_shared=False,
        )
        await database.fetch_one("SELECT 1")
        await verify_migrations_current()
        await redis.ping()
        if (
            settings.redis_acl_preflight_required
            or deployment_mode == "production"
        ):
            await verify_redis_acl(
                redis,
                redis_url=settings.redis_url,
                expected_username=settings.redis_expected_username,
                role="core",
                configured_username=settings.redis_username,
                configured_password=settings.redis_password,
                reject_development_passwords=(
                    deployment_mode == "production"
                ),
                platforms=(platform,),
                include_shared=False,
            )
        await reconcile_platform_runner_startup_streams(
            platform,
        )

        shutdown_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for current_signal in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(
                current_signal,
                shutdown_event.set,
            )
        tasks = start_platform_runner_tasks(
            shutdown_event,
            platform=platform,
        )
        structured_log(
            "info",
            "core_platform_runner_started",
            platform=platform,
        )
        await supervise_platform_runner_tasks(tasks, shutdown_event)
    finally:
        try:
            platform_runner_health_file(platform).unlink(
                missing_ok=True
            )
        finally:
            await database.disconnect()
            await redis.close()
        structured_log(
            "info",
            "core_platform_runner_stopped",
            platform=platform,
        )


if __name__ == "__main__":
    asyncio.run(main())
