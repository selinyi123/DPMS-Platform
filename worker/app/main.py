import asyncio
import json
import os
import signal
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent))

from app.browser_pool import BrowserPool
from app.account_calibrator import calibration_loop
from app.adapter_probe import probe_loop
from app.config import settings
from app.db import database, redis as redis_client
from app.evidence_storage import (
    DEFAULT_EVIDENCE_DIRECTORIES,
    EvidenceStoragePreflightError,
    preflight_evidence_storage,
)
from app.event_store.service import ensure_event_schema, record_event
from app.login_broker import login_loop
from app.services.task_outbox import start_task_outbox_dispatcher
from app.task_runner import task_loop
from app.utils.log import structured_log


HEALTH_FILE = Path("/tmp/worker-health")
WORKER_ID = os.getenv("HOSTNAME") or f"worker-{os.getpid()}"
STARTUP_CONNECT_ATTEMPTS = int(os.getenv("WORKER_STARTUP_CONNECT_ATTEMPTS", "30"))
STARTUP_CONNECT_DELAY_SECONDS = float(os.getenv("WORKER_STARTUP_CONNECT_DELAY_SECONDS", "2"))


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


async def heartbeat_loop(shutdown_event: asyncio.Event):
    while not shutdown_event.is_set():
        HEALTH_FILE.write_text("ok", encoding="utf-8")
        try:
            await database.execute(
                """INSERT INTO worker_heartbeats (worker_id, service_name, status, pid, detail, last_seen_at)
                   VALUES (:worker_id, 'worker', 'ok', :pid, :detail, NOW())
                   ON DUPLICATE KEY UPDATE
                     status = 'ok',
                     pid = :pid,
                     detail = :detail,
                     last_seen_at = NOW(),
                     updated_at = NOW()""",
                {
                    "worker_id": WORKER_ID,
                    "pid": os.getpid(),
                    "detail": json.dumps({"max_browsers": settings.worker_max_browsers}),
                },
            )
        except Exception as e:
            structured_log("error", "worker_heartbeat_db_failed", exception=e)
        await asyncio.sleep(10)


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


async def main():
    clear_stale_health_marker()
    await ensure_evidence_storage_ready()
    await connect_with_retry("worker_db", database.connect)
    await connect_with_retry("worker_redis", redis_client.initialize)
    await ensure_event_schema()
    structured_log("info", "worker_db_connected")
    await record_event(
        aggregate="worker",
        aggregate_id=WORKER_ID,
        event_type="WorkerStarted",
        payload={"pid": os.getpid(), "max_browsers": settings.worker_max_browsers},
        actor_type="system",
        actor_id=WORKER_ID,
    )

    pool = BrowserPool(max_browsers=settings.worker_max_browsers)
    await pool.init()

    shutdown_event = asyncio.Event()

    def handle_sigterm():
        structured_log("info", "shutdown_signal_received")
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, handle_sigterm)
    loop.add_signal_handler(signal.SIGINT, handle_sigterm)

    context_reaper_task = asyncio.create_task(
        pool.context_reaper_loop(
            shutdown_event,
            interval_seconds=int(os.getenv("WORKER_CONTEXT_REAPER_INTERVAL_SECONDS", "60")),
        )
    )
    heartbeat_task = asyncio.create_task(heartbeat_loop(shutdown_event))
    reload_signal_task = asyncio.create_task(reload_signal_loop(shutdown_event))
    task_outbox_task = asyncio.create_task(start_task_outbox_dispatcher(shutdown_event))
    login_task = asyncio.create_task(login_loop(pool, shutdown_event))
    calibration_task = asyncio.create_task(calibration_loop(pool, shutdown_event))
    probe_task = asyncio.create_task(probe_loop(pool, shutdown_event))
    worker_task = asyncio.create_task(task_loop(pool, shutdown_event))
    await shutdown_event.wait()
    structured_log("info", "shutdown_started")

    tasks = (worker_task, probe_task, calibration_task, login_task, task_outbox_task, context_reaper_task, reload_signal_task, heartbeat_task)
    for task in tasks:
        task.cancel()
    for task in tasks:
        try:
            await task
        except asyncio.CancelledError:
            pass

    for bid in list(pool._browsers.keys()):
        await pool.close_browser(bid)
    for account_id in list(pool._persistent_contexts.keys()):
        await pool.close_account_context(account_id, reason="worker_shutdown")
    await pool._playwright.stop()

    await record_event(
        aggregate="worker",
        aggregate_id=WORKER_ID,
        event_type="WorkerStopped",
        payload={"pid": os.getpid()},
        actor_type="system",
        actor_id=WORKER_ID,
    )
    await database.disconnect()
    await redis_client.close()
    structured_log("info", "shutdown_complete")


if __name__ == "__main__":
    asyncio.run(main())
