import asyncio
import json
import os
import signal
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent))

from app.browser_pool import BrowserPool
from app.account_calibrator import (
    calibration_loop,
    legacy_calibration_fanout_loop,
)
from app.account_profile_cleanup import account_profile_cleanup_loop
from app.adapter_probe import (
    legacy_probe_fanout_loop,
    probe_loop,
)
from app.config import settings
from app.heartbeat_retention import heartbeat_retention_loop
from app.db import database, redis as redis_client
from app.evidence_storage import (
    DEFAULT_EVIDENCE_DIRECTORIES,
    EvidenceStoragePreflightError,
    preflight_evidence_storage,
)
from app.event_store.service import record_event, verify_event_schema
from app.login_broker import login_loop
from app.login_profile_cleanup import login_profile_cleanup_loop
from app.platform_modules.registry import get_platform_module
from app.runtime_lane_health import (
    runtime_lane_health_snapshot,
    runtime_lanes_ready,
)
from app.services.task_outbox import start_task_outbox_dispatcher
from app.runtime_scope import (
    WorkerRuntimePlan,
    build_worker_runtime_plan,
    validate_worker_deployment_plan,
)
from app.task_runner import (
    CONSUMER_NAME as TASK_CONSUMER_NAME,
    task_lane_health_snapshot,
    task_loop,
)
from app.task_streams import task_stream_bindings
from app.utils.log import structured_log
from app.worker_identity import WORKER_ID
from app.xiaohongshu_target_pursuit import (
    xiaohongshu_target_pursuit_loop,
)
from shared.redis_acl import verify_redis_acl
from shared.database_credentials import require_production_database_credentials
from shared.runtime_secrets import require_production_encryption_key
from shared.platform_security import require_platform_runtime_identity


HEALTH_FILE = Path("/tmp/worker-health")
STARTUP_CONNECT_ATTEMPTS = int(os.getenv("WORKER_STARTUP_CONNECT_ATTEMPTS", "30"))
STARTUP_CONNECT_DELAY_SECONDS = float(os.getenv("WORKER_STARTUP_CONNECT_DELAY_SECONDS", "2"))
WORKER_REPAIR_CAPABILITY = "repair_execution_intent_v1"
WORKER_HEARTBEAT_DETAIL_MAX_BYTES = 16 * 1024
WORKER_HEALTH_INTERVAL_SECONDS = 10
WORKER_HEALTH_PROBE_TIMEOUT_SECONDS = 5
WORKER_SHUTDOWN_TASK_TIMEOUT_SECONDS = float(
    os.getenv("WORKER_SHUTDOWN_TASK_TIMEOUT_SECONDS", "10")
)
WORKER_SHUTDOWN_STEP_TIMEOUT_SECONDS = float(
    os.getenv("WORKER_SHUTDOWN_STEP_TIMEOUT_SECONDS", "30")
)


def clear_stale_health_marker() -> None:
    """A failed restart must not inherit a briefly healthy old heartbeat."""

    try:
        HEALTH_FILE.unlink(missing_ok=True)
    except OSError as exc:
        structured_log(
            "critical",
            "worker_health_marker_reset_failed",
            path=str(HEALTH_FILE),
            errno=getattr(exc, "errno", None),
            cause_type=type(exc).__name__,
        )
        raise


async def ensure_evidence_storage_ready() -> tuple[str, ...]:
    """Fail startup before any browser/task loop if evidence is unsafe."""

    try:
        checked = await asyncio.to_thread(
            preflight_evidence_storage,
            DEFAULT_EVIDENCE_DIRECTORIES,
        )
    except EvidenceStoragePreflightError as exc:
        structured_log(
            "critical",
            "worker_evidence_storage_preflight_failed",
            code=exc.code,
            directory=exc.directory,
            component=exc.component,
            operation=exc.operation,
            errno=exc.errno_value,
            cause_type=exc.cause_type,
            primary_code=exc.primary_code,
        )
        raise
    except Exception as exc:
        structured_log(
            "critical",
            "worker_evidence_storage_preflight_failed",
            code="evidence_storage_preflight_unclassified",
            directory="<unknown>",
            operation="preflight",
            errno=getattr(exc, "errno", None),
            cause_type=type(exc).__name__,
        )
        raise

    structured_log(
        "info",
        "worker_evidence_storage_preflight_succeeded",
        directories=len(checked),
        capabilities=(
            "private_mode,openat,nofollow,flock,exclusive_create,"
            "file_fsync,directory_fsync,identity_unlink"
        ),
    )
    return checked


async def connect_with_retry(name: str, connect_call) -> None:
    for attempt in range(1, STARTUP_CONNECT_ATTEMPTS + 1):
        try:
            await connect_call()
            return
        except Exception as exc:
            if attempt >= STARTUP_CONNECT_ATTEMPTS:
                structured_log("error", f"{name}_connect_failed", attempt=attempt, exception=exc)
                raise
            structured_log(
                "warning",
                f"{name}_connect_retry",
                attempt=attempt,
                max_attempts=STARTUP_CONNECT_ATTEMPTS,
                delay_seconds=STARTUP_CONNECT_DELAY_SECONDS,
                error=str(exc),
            )
            await asyncio.sleep(STARTUP_CONNECT_DELAY_SECONDS)


def build_worker_heartbeat_detail(
    runtime_plan: WorkerRuntimePlan | None = None,
) -> str:
    """Build a bounded heartbeat document without queue data or error text."""

    runtime_plan = runtime_plan or build_worker_runtime_plan()
    include_legacy_fanout = bool(
        runtime_plan.owns_legacy_fanout
        and settings.legacy_control_stream_drain_enabled
    )
    detail = {
        "max_browsers": settings.worker_max_browsers,
        "execution_intent_contract_version": 1,
        "capabilities": (
            [WORKER_REPAIR_CAPABILITY]
            if runtime_plan.owns_platform_lanes
            else []
        ),
        "runtime_role": runtime_plan.role,
        "platform_scope": list(runtime_plan.platforms),
        "task_consumer_name": TASK_CONSUMER_NAME,
        "task_lane_health": task_lane_health_snapshot(
            runtime_plan.platforms
        ),
        "runtime_lane_health": runtime_lane_health_snapshot(
            runtime_plan,
            include_legacy_fanout=include_legacy_fanout,
        ),
    }
    encoded = json.dumps(
        detail,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    if len(encoded.encode("utf-8")) > WORKER_HEARTBEAT_DETAIL_MAX_BYTES:
        raise RuntimeError("worker_heartbeat_detail_exceeds_limit")
    return encoded


def worker_owned_lanes_ready(runtime_plan: WorkerRuntimePlan) -> bool:
    """Require fresh evidence from every lane owned by this exact role."""

    if runtime_plan.owns_platform_lanes:
        task_health = task_lane_health_snapshot(
            runtime_plan.platforms
        )
        observed_by_stream = {
            str(lane.get("stream") or ""): lane
            for lane in task_health.get("lanes") or ()
        }
        expected_streams = {
            binding.stream_key
            for binding in task_stream_bindings(
                include_legacy=False
            )
            if binding.platform in runtime_plan.platforms
        }
        if (
            not expected_streams
            or set(observed_by_stream) != expected_streams
            or any(
                observed_by_stream[stream_key].get("status")
                != "healthy"
                for stream_key in expected_streams
            )
        ):
            return False
    return runtime_lanes_ready(
        runtime_plan,
        include_legacy_fanout=bool(
            runtime_plan.owns_legacy_fanout
            and settings.legacy_control_stream_drain_enabled
        ),
    )


def _set_worker_health_marker(healthy: bool) -> None:
    if healthy:
        HEALTH_FILE.write_text("ok", encoding="utf-8")
    else:
        HEALTH_FILE.unlink(missing_ok=True)


async def publish_worker_heartbeat_once(
    runtime_plan: WorkerRuntimePlan,
) -> bool:
    """Persist one heartbeat, refreshing liveness only after all checks pass."""

    lanes_ready = worker_owned_lanes_ready(runtime_plan)
    if not lanes_ready:
        _set_worker_health_marker(False)

    try:
        redis_ready = await asyncio.wait_for(
            redis_client.ping(),
            timeout=WORKER_HEALTH_PROBE_TIMEOUT_SECONDS,
        )
        if not redis_ready:
            raise RuntimeError("worker_redis_ping_unhealthy")
    except Exception as exc:
        _set_worker_health_marker(False)
        structured_log(
            "error",
            "worker_heartbeat_redis_failed",
            exception=exc,
        )
        return False

    status = "ok" if lanes_ready else "degraded"
    try:
        await asyncio.wait_for(
            database.execute(
                """INSERT INTO worker_heartbeats (worker_id, service_name, status, pid, detail, last_seen_at)
                   VALUES (:worker_id, 'worker', :status, :pid, :detail, NOW())
                   ON DUPLICATE KEY UPDATE
                     status = :status,
                     pid = :pid,
                     detail = :detail,
                     last_seen_at = NOW(),
                     updated_at = NOW()""",
                {
                    "worker_id": WORKER_ID,
                    "status": status,
                    "pid": os.getpid(),
                    "detail": build_worker_heartbeat_detail(
                        runtime_plan
                    ),
                },
            ),
            timeout=WORKER_HEALTH_PROBE_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        _set_worker_health_marker(False)
        structured_log(
            "error",
            "worker_heartbeat_db_failed",
            exception=exc,
        )
        return False

    if (
        not lanes_ready
        or not worker_owned_lanes_ready(runtime_plan)
    ):
        _set_worker_health_marker(False)
        return False
    _set_worker_health_marker(True)
    return True


async def heartbeat_loop(
    shutdown_event: asyncio.Event,
    runtime_plan: WorkerRuntimePlan | None = None,
):
    runtime_plan = runtime_plan or build_worker_runtime_plan()
    while not shutdown_event.is_set():
        await publish_worker_heartbeat_once(runtime_plan)
        try:
            await asyncio.wait_for(
                shutdown_event.wait(),
                timeout=WORKER_HEALTH_INTERVAL_SECONDS,
            )
        except asyncio.TimeoutError:
            pass


async def reload_signal_loop(shutdown_event: asyncio.Event):
    pubsub = redis_client.pubsub()
    await pubsub.subscribe("worker:reload")
    try:
        while not shutdown_event.is_set():
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1)
            if not message:
                await asyncio.sleep(0.2)
                continue
            signal_value = message.get("data")
            structured_log("warning", "worker_reload_signal_received", signal=signal_value)
            await record_event(
                aggregate="worker",
                aggregate_id=WORKER_ID,
                event_type="WorkerReloadSignalReceived",
                payload={"signal": signal_value},
                actor_type="system",
                actor_id=WORKER_ID,
            )
            shutdown_event.set()
            break
    except asyncio.CancelledError:
        pass
    finally:
        try:
            await pubsub.unsubscribe("worker:reload")
            await pubsub.close()
        except Exception:
            pass


def _worker_task_name(task: asyncio.Task) -> str:
    try:
        return task.get_name()
    except AttributeError:
        return "unnamed-worker-task"


async def supervise_worker_tasks(
    tasks: tuple[asyncio.Task, ...],
    shutdown_event: asyncio.Event,
) -> None:
    """Exit the Worker when any critical loop stops before shutdown.

    The heartbeat must never outlive task consumption, outbox delivery, or the
    other control loops. A normal return is therefore as fatal as an exception.
    This helper also owns deterministic sibling cancellation for both failure
    and requested shutdown.
    """

    if not tasks:
        raise ValueError("worker_critical_tasks_required")

    shutdown_waiter = asyncio.create_task(
        shutdown_event.wait(),
        name="worker:shutdown-waiter",
    )
    try:
        done, _ = await asyncio.wait(
            (*tasks, shutdown_waiter),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if shutdown_waiter in done or shutdown_event.is_set():
            return

        completed = next(task for task in tasks if task in done)
        task_name = _worker_task_name(completed)
        try:
            completed.result()
        except asyncio.CancelledError as exc:
            failure = RuntimeError(
                f"worker_critical_task_cancelled:{task_name}"
            )
            structured_log(
                "critical",
                "worker_critical_task_exited",
                task=task_name,
                outcome="cancelled",
                exception=exc,
            )
            shutdown_event.set()
            raise failure from exc
        except BaseException as exc:
            structured_log(
                "critical",
                "worker_critical_task_exited",
                task=task_name,
                outcome="exception",
                exception=exc,
            )
            shutdown_event.set()
            raise

        failure = RuntimeError(
            f"worker_critical_task_returned:{task_name}"
        )
        structured_log(
            "critical",
            "worker_critical_task_exited",
            task=task_name,
            outcome="unexpected_return",
            exception=failure,
        )
        shutdown_event.set()
        raise failure
    finally:
        shutdown_event.set()
        if not shutdown_waiter.done():
            shutdown_waiter.cancel()
        for task in tasks:
            if not task.done():
                task.cancel()
        joined = (shutdown_waiter, *tasks)
        joined_done, pending = await asyncio.wait(
            joined,
            timeout=max(WORKER_SHUTDOWN_TASK_TIMEOUT_SECONDS, 0.001),
        )
        for task in joined_done:
            if task.cancelled():
                continue
            try:
                task.exception()
            except (asyncio.CancelledError, Exception):
                pass
        if pending:
            structured_log(
                "error",
                "worker_shutdown_tasks_timeout",
                pending_tasks=sorted(
                    _worker_task_name(task) for task in pending
                ),
            )


def start_worker_runtime_tasks(
    pool: BrowserPool,
    shutdown_event: asyncio.Event,
    runtime_plan: WorkerRuntimePlan,
) -> tuple[asyncio.Task, ...]:
    """Start exactly the loops owned by one mutually-exclusive Worker role."""

    tasks = [
        asyncio.create_task(
            pool.context_reaper_loop(
                shutdown_event,
                interval_seconds=int(
                    os.getenv(
                        "WORKER_CONTEXT_REAPER_INTERVAL_SECONDS",
                        "60",
                    )
                ),
            ),
            name="worker:context-reaper",
        ),
        asyncio.create_task(
            heartbeat_loop(shutdown_event, runtime_plan),
            name="worker:heartbeat",
        ),
        asyncio.create_task(
            reload_signal_loop(shutdown_event),
            name="worker:reload-signal",
        ),
    ]
    if runtime_plan.owns_control_loops:
        tasks.extend(
            (
                asyncio.create_task(
                    start_task_outbox_dispatcher(shutdown_event),
                    name="worker:task-outbox",
                ),
                asyncio.create_task(
                    login_loop(pool, shutdown_event),
                    name="worker:login",
                ),
                asyncio.create_task(
                    login_profile_cleanup_loop(
                        pool,
                        shutdown_event,
                    ),
                    name="worker:login-profile-cleanup",
                ),
                asyncio.create_task(
                    heartbeat_retention_loop(shutdown_event),
                    name="worker:heartbeat-retention",
                ),
            )
        )
    if (
        runtime_plan.owns_legacy_fanout
        and settings.legacy_control_stream_drain_enabled
    ):
        tasks.extend(
            (
                asyncio.create_task(
                    legacy_probe_fanout_loop(shutdown_event),
                    name="worker:legacy-probe-fanout",
                ),
                asyncio.create_task(
                    legacy_calibration_fanout_loop(
                        shutdown_event
                    ),
                    name="worker:legacy-calibration-fanout",
                ),
            )
        )
    if runtime_plan.owns_platform_lanes:
        tasks.extend(
            (
                asyncio.create_task(
                    calibration_loop(
                        pool,
                        shutdown_event,
                        platforms=runtime_plan.platforms,
                        include_legacy_fanout=False,
                    ),
                    name="worker:calibration",
                ),
                asyncio.create_task(
                    probe_loop(
                        pool,
                        shutdown_event,
                        platforms=runtime_plan.platforms,
                        include_legacy_fanout=False,
                    ),
                    name="worker:probe",
                ),
                asyncio.create_task(
                    task_loop(
                        pool,
                        shutdown_event,
                        platforms=runtime_plan.platforms,
                    ),
                    name="worker:task-consumer",
                ),
                asyncio.create_task(
                    account_profile_cleanup_loop(
                        pool,
                        shutdown_event,
                        platforms=runtime_plan.platforms,
                    ),
                    name="worker:account-profile-cleanup",
                ),
            )
        )
        if "xiaohongshu" in runtime_plan.platforms:
            tasks.append(
                asyncio.create_task(
                    xiaohongshu_target_pursuit_loop(
                        pool,
                        shutdown_event,
                    ),
                    name="worker:xiaohongshu-target-pursuit",
                )
            )
    return tuple(tasks)


def preflight_worker_platform_modules(
    runtime_plan: WorkerRuntimePlan,
) -> None:
    """Import only the module owned by an isolated platform Worker."""

    if runtime_plan.role != "platform":
        return
    for platform in runtime_plan.platforms:
        module = get_platform_module(platform)
        if module is None or module.platform_id != platform:
            raise RuntimeError(
                f"worker_platform_preflight_mismatch:{platform}"
            )


async def shutdown_worker_resources(
    pool: BrowserPool,
    runtime_plan: WorkerRuntimePlan,
    *,
    worker_started: bool = True,
) -> tuple[str, ...]:
    """Release every independent resource even when an earlier step fails."""

    failed_steps = []
    try:
        _set_worker_health_marker(False)
    except Exception as exc:
        failed_steps.append("health_marker")
        structured_log(
            "error",
            "worker_shutdown_step_failed",
            step="health_marker",
            exception=exc,
        )
    try:
        await asyncio.wait_for(
            database.execute(
                """UPDATE worker_heartbeats
                   SET status = 'stopped',
                       last_seen_at = NOW(),
                       updated_at = NOW()
                   WHERE worker_id = :worker_id""",
                {"worker_id": WORKER_ID},
            ),
            timeout=max(
                WORKER_HEALTH_PROBE_TIMEOUT_SECONDS,
                0.001,
            ),
        )
    except Exception as exc:
        failed_steps.append("heartbeat_stopped")
        structured_log(
            "error",
            "worker_shutdown_step_failed",
            step="heartbeat_stopped",
            exception=exc,
        )
    try:
        await asyncio.wait_for(
            pool.close(),
            timeout=max(
                WORKER_SHUTDOWN_STEP_TIMEOUT_SECONDS,
                0.001,
            ),
        )
    except Exception as exc:
        failed_steps.append("browser_pool")
        structured_log(
            "error",
            "worker_shutdown_step_failed",
            step="browser_pool",
            exception=exc,
        )

    if worker_started:
        try:
            await asyncio.wait_for(
                record_event(
                    aggregate="worker",
                    aggregate_id=WORKER_ID,
                    event_type="WorkerStopped",
                    payload={
                        "pid": os.getpid(),
                        "runtime_role": runtime_plan.role,
                        "platform_scope": list(runtime_plan.platforms),
                    },
                    actor_type="system",
                    actor_id=WORKER_ID,
                ),
                timeout=max(
                    WORKER_SHUTDOWN_STEP_TIMEOUT_SECONDS,
                    0.001,
                ),
            )
        except Exception as exc:
            failed_steps.append("worker_stopped_event")
            structured_log(
                "error",
                "worker_shutdown_step_failed",
                step="worker_stopped_event",
                exception=exc,
            )

    try:
        await asyncio.wait_for(
            database.disconnect(),
            timeout=max(
                WORKER_SHUTDOWN_STEP_TIMEOUT_SECONDS,
                0.001,
            ),
        )
    except Exception as exc:
        failed_steps.append("database")
        structured_log(
            "error",
            "worker_shutdown_step_failed",
            step="database",
            exception=exc,
        )

    try:
        await asyncio.wait_for(
            redis_client.close(),
            timeout=max(
                WORKER_SHUTDOWN_STEP_TIMEOUT_SECONDS,
                0.001,
            ),
        )
    except Exception as exc:
        failed_steps.append("redis")
        structured_log(
            "error",
            "worker_shutdown_step_failed",
            step="redis",
            exception=exc,
        )
    return tuple(failed_steps)


async def main():
    clear_stale_health_marker()
    deployment_mode = str(
        settings.deployment_mode or ""
    ).strip().casefold()
    runtime_plan = validate_worker_deployment_plan(
        build_worker_runtime_plan(),
        deployment_mode=deployment_mode,
        configured_instance_id=os.getenv(
            "DPMS_WORKER_INSTANCE_ID",
        ),
    )
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
    if runtime_plan.role == "platform" and runtime_plan.platforms:
        require_platform_runtime_identity(
            platform=runtime_plan.platforms[0],
            role="worker",
            deployment_mode=deployment_mode,
            security_mode=settings.platform_security_mode,
            database_username=settings.mysql_runtime_user,
            redis_username=settings.redis_expected_username,
            encryption_key=settings.encryption_key,
        )
    preflight_worker_platform_modules(runtime_plan)
    if runtime_plan.owns_platform_lanes:
        await ensure_evidence_storage_ready()
    pool = BrowserPool(max_browsers=settings.worker_max_browsers)
    worker_started = False
    try:
        await connect_with_retry("worker_db", database.connect)
        await connect_with_retry("worker_redis", redis_client.initialize)
        if (
            settings.redis_acl_preflight_required
            or deployment_mode == "production"
        ):
            try:
                await verify_redis_acl(
                    redis_client,
                    redis_url=settings.redis_url,
                    expected_username=settings.redis_expected_username,
                    role="worker",
                    configured_username=settings.redis_username,
                    configured_password=settings.redis_password,
                    reject_development_passwords=(
                        deployment_mode == "production"
                    ),
                    platforms=runtime_plan.platforms,
                    include_shared=runtime_plan.owns_control_loops,
                )
            except Exception as exc:
                structured_log(
                    "critical",
                    "worker_redis_acl_preflight_failed",
                    role="worker",
                    exception=exc,
                )
                raise
            structured_log(
                "info",
                "worker_redis_acl_preflight_succeeded",
                role="worker",
            )
        # Application processes never perform DDL. Core's dedicated migration
        # command owns schema changes; every Worker verifies the minimum
        # contract before advertising a heartbeat or consuming a lane.
        await verify_event_schema()
        structured_log("info", "worker_db_connected")
        await record_event(
            aggregate="worker",
            aggregate_id=WORKER_ID,
            event_type="WorkerStarted",
            payload={
                "pid": os.getpid(),
                "max_browsers": settings.worker_max_browsers,
                "runtime_role": runtime_plan.role,
                "platform_scope": list(runtime_plan.platforms),
            },
            actor_type="system",
            actor_id=WORKER_ID,
        )
        worker_started = True
        await pool.init()

        shutdown_event = asyncio.Event()

        def handle_sigterm():
            structured_log("info", "shutdown_signal_received")
            shutdown_event.set()

        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGTERM, handle_sigterm)
        loop.add_signal_handler(signal.SIGINT, handle_sigterm)

        tasks = start_worker_runtime_tasks(
            pool,
            shutdown_event,
            runtime_plan,
        )
        await supervise_worker_tasks(tasks, shutdown_event)
    finally:
        structured_log("info", "shutdown_started")
        failed_steps = await shutdown_worker_resources(
            pool,
            runtime_plan,
            worker_started=worker_started,
        )
        structured_log(
            "info",
            "shutdown_complete",
            failed_steps=list(failed_steps),
        )


if __name__ == "__main__":
    asyncio.run(main())
