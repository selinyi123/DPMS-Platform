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
from app.event_store.service import ensure_event_schema, record_event
from app.login_broker import login_loop
from app.task_runner import task_loop
from app.utils.log import structured_log


HEALTH_FILE = Path("/tmp/worker-health")
WORKER_ID = os.getenv("HOSTNAME") or f"worker-{os.getpid()}"


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
    await database.connect()
    await redis_client.initialize()
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

    heartbeat_task = asyncio.create_task(heartbeat_loop(shutdown_event))
    reload_signal_task = asyncio.create_task(reload_signal_loop(shutdown_event))
    login_task = asyncio.create_task(login_loop(pool, shutdown_event))
    calibration_task = asyncio.create_task(calibration_loop(pool, shutdown_event))
    probe_task = asyncio.create_task(probe_loop(pool, shutdown_event))
    worker_task = asyncio.create_task(task_loop(pool, shutdown_event))
    await shutdown_event.wait()
    structured_log("info", "shutdown_started")

    worker_task.cancel()
    probe_task.cancel()
    calibration_task.cancel()
    login_task.cancel()
    reload_signal_task.cancel()
    heartbeat_task.cancel()
    for task in (worker_task, probe_task, calibration_task, login_task, reload_signal_task, heartbeat_task):
        try:
            await task
        except asyncio.CancelledError:
            pass

    for bid in list(pool._browsers.keys()):
        await pool.close_browser(bid)
    for account_id in list(pool._persistent_contexts.keys()):
        await pool.close_account_context(account_id)
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
