"""Bounded retention cleanup for stale nonce-based Worker heartbeats."""

from __future__ import annotations

import asyncio

from app.db import database, execute_affected_rows
from app.redis_consumer_retention import (
    REDIS_CONSUMER_RETIRE_IDLE_SECONDS,
    retire_stale_redis_consumers_once,
)
from app.utils.log import structured_log
from app.worker_identity import WORKER_ID


# Keep this bounded pass frequent enough that a consumer which crosses Core's
# stale threshold disappears from health/readiness within one dashboard
# refresh window plus at most one minute. Every Redis deletion is still
# guarded by the atomic pending/idle recheck in redis_consumer_retention.
HEARTBEAT_RETENTION_INTERVAL_SECONDS = 60
HEARTBEAT_RETIREMENT_MAX_PER_PASS = 100


async def retire_stale_worker_heartbeats_once() -> dict[str, int]:
    retired_heartbeats = await execute_affected_rows(
        """DELETE FROM worker_heartbeats
           WHERE service_name = 'worker'
             AND last_seen_at < (
             NOW() - INTERVAL 7 DAY
           )
             AND worker_id <> :worker_id
           ORDER BY last_seen_at ASC, worker_id ASC
           LIMIT :limit""",
        {
            "worker_id": WORKER_ID,
            "limit": HEARTBEAT_RETIREMENT_MAX_PER_PASS,
        },
        db=database,
    )
    return {
        "heartbeats": int(retired_heartbeats or 0),
    }


async def heartbeat_retention_loop(
    shutdown_event: asyncio.Event,
) -> None:
    while not shutdown_event.is_set():
        try:
            retired = await retire_stale_worker_heartbeats_once()
            consumer_retention = (
                await retire_stale_redis_consumers_once()
            )
            if (
                retired["heartbeats"]
                or consumer_retention["retired"]
                or consumer_retention["oversized_groups"]
                or consumer_retention["unavailable_groups"]
            ):
                structured_log(
                    "info",
                    "worker_stale_heartbeats_retired",
                    **retired,
                    redis_consumers=consumer_retention["retired"],
                    redis_consumer_candidates=consumer_retention[
                        "candidates"
                    ],
                    redis_consumer_oversized_groups=consumer_retention[
                        "oversized_groups"
                    ],
                    redis_consumer_unavailable_groups=consumer_retention[
                        "unavailable_groups"
                    ],
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            structured_log(
                "error",
                "worker_heartbeat_retention_failed",
                exception=exc,
            )
        try:
            await asyncio.wait_for(
                shutdown_event.wait(),
                timeout=HEARTBEAT_RETENTION_INTERVAL_SECONDS,
            )
        except asyncio.TimeoutError:
            pass


__all__ = (
    "heartbeat_retention_loop",
    "retire_stale_worker_heartbeats_once",
)
