import asyncio
import json
import uuid
from typing import Any

from app.db import database, redis
from app.utils.log import structured_log


TASK_OUTBOX_MAX_ATTEMPTS = 5
TASK_OUTBOX_BATCH = 50
TASK_OUTBOX_POLL_SECONDS = 5
TASK_OUTBOX_SENDING_RECLAIM_SECONDS = 60


def _json(value: dict[str, Any]) -> str:
    return json.dumps(value or {}, ensure_ascii=False, default=str)


async def enqueue_event_outbox(
    *,
    aggregate: str,
    aggregate_id: Any,
    event_type: str,
    payload: dict[str, Any] | None = None,
    correlation_id: Any | None = None,
    causation_id: Any | None = None,
    actor_type: str = "system",
    actor_id: Any | None = None,
    source_service: str = "worker",
    dedup_key: str | None = None,
) -> None:
    """Persist an event-store write request inside the caller's DB transaction."""
    event_id = str(uuid.uuid4())
    outbox_payload = {
        "id": event_id,
        "aggregate": aggregate,
        "aggregate_id": str(aggregate_id),
        "event_type": event_type,
        "payload": payload or {},
        "correlation_id": str(correlation_id) if correlation_id is not None else None,
        "causation_id": str(causation_id) if causation_id is not None else None,
        "actor_type": actor_type,
        "actor_id": str(actor_id) if actor_id is not None else None,
        "source_service": source_service,
    }
    await _enqueue(
        event_kind="event_store",
        aggregate=aggregate,
        aggregate_id=str(aggregate_id),
        event_type=event_type,
        stream_key=None,
        payload=outbox_payload,
        dedup_key=dedup_key or f"event:{aggregate}:{aggregate_id}:{event_type}:{correlation_id}:{event_id}",
    )


async def enqueue_notify_outbox(*, stream_key: str, message: dict[str, Any], dedup_key: str) -> None:
    """Persist a Redis stream notification write request inside the caller's transaction."""
    await _enqueue(
        event_kind="notify_stream",
        aggregate="notification",
        aggregate_id=str(message.get("task_id") or dedup_key),
        event_type="NotifyEventRequested",
        stream_key=stream_key,
        payload=message,
        dedup_key=dedup_key,
    )


async def _enqueue(
    *,
    event_kind: str,
    aggregate: str | None,
    aggregate_id: str | None,
    event_type: str | None,
    stream_key: str | None,
    payload: dict[str, Any],
    dedup_key: str,
) -> None:
    await database.execute(
        """INSERT INTO task_outbox_events
           (event_kind, aggregate, aggregate_id, event_type, stream_key, payload, dedup_key, status)
           VALUES (:event_kind, :aggregate, :aggregate_id, :event_type, :stream_key, :payload, :dedup_key, 'pending')
           ON DUPLICATE KEY UPDATE id = id""",
        {
            "event_kind": event_kind,
            "aggregate": aggregate,
            "aggregate_id": aggregate_id,
            "event_type": event_type,
            "stream_key": stream_key,
            "payload": _json(payload),
            "dedup_key": dedup_key[:191],
        },
    )


def _retry_status(attempts: int) -> str:
    return "pending" if attempts < TASK_OUTBOX_MAX_ATTEMPTS else "failed"


async def reclaim_stale_task_outbox() -> int:
    result = await database.execute(
        """UPDATE task_outbox_events SET status = 'pending'
           WHERE status = 'sending' AND updated_at < (NOW() - INTERVAL 60 SECOND)"""
    )
    return int(result or 0)


async def flush_pending_task_outbox(limit: int = TASK_OUTBOX_BATCH) -> dict[str, int]:
    rows = await database.fetch_all(
        "SELECT id FROM task_outbox_events WHERE status = 'pending' ORDER BY id LIMIT :limit",
        {"limit": limit},
    )
    sent = 0
    for row in rows:
        if await _relay_by_id(row["id"]):
            sent += 1
    return {"scanned": len(rows), "sent": sent}


async def _claim_row(row_id) -> dict | None:
    async with database.transaction():
        row = await database.fetch_one(
            """SELECT id, event_kind, stream_key, payload, attempts
               FROM task_outbox_events
               WHERE id = :id AND status = 'pending'
               FOR UPDATE""",
            {"id": row_id},
        )
        if not row:
            return None
        await database.execute(
            "UPDATE task_outbox_events SET status = 'sending' WHERE id = :id",
            {"id": row_id},
        )
        return dict(row)


async def _relay_by_id(row_id) -> bool:
    row = await _claim_row(row_id)
    if row is None:
        return False
    return await _deliver_claimed(row)


async def _deliver_claimed(row: dict) -> bool:
    attempts = int(row["attempts"] or 0) + 1
    try:
        payload = json.loads(row["payload"]) if isinstance(row["payload"], str) else dict(row["payload"])
        if row["event_kind"] == "event_store":
            await _deliver_event(payload)
        elif row["event_kind"] == "notify_stream":
            await _deliver_notify(row["stream_key"], payload)
        else:
            raise RuntimeError(f"Unknown task outbox event kind: {row['event_kind']}")
        await database.execute(
            "UPDATE task_outbox_events SET status = 'sent', sent_at = NOW(), attempts = :attempts WHERE id = :id",
            {"id": row["id"], "attempts": attempts},
        )
        return True
    except Exception as exc:
        await database.execute(
            """UPDATE task_outbox_events
               SET status = :status, attempts = :attempts, last_error = :error
               WHERE id = :id""",
            {"id": row["id"], "status": _retry_status(attempts), "attempts": attempts, "error": str(exc)[:512]},
        )
        structured_log(
            "error" if attempts >= TASK_OUTBOX_MAX_ATTEMPTS else "warning",
            "task_outbox_relay_failed",
            outbox_id=row["id"],
            event_kind=row["event_kind"],
            attempts=attempts,
            error=str(exc),
        )
        return False


async def _deliver_event(payload: dict[str, Any]) -> None:
    await database.execute(
        """INSERT INTO events
           (id, aggregate, aggregate_id, event_type, payload, correlation_id, causation_id, actor_type, actor_id, source_service)
           VALUES (:id, :aggregate, :aggregate_id, :event_type, :payload, :correlation_id, :causation_id, :actor_type, :actor_id, :source_service)
           ON DUPLICATE KEY UPDATE id = id""",
        {
            "id": payload["id"],
            "aggregate": payload["aggregate"],
            "aggregate_id": payload["aggregate_id"],
            "event_type": payload["event_type"],
            "payload": _json(payload.get("payload") or {}),
            "correlation_id": payload.get("correlation_id"),
            "causation_id": payload.get("causation_id"),
            "actor_type": payload.get("actor_type") or "system",
            "actor_id": payload.get("actor_id"),
            "source_service": payload.get("source_service") or "worker",
        },
    )


async def _deliver_notify(stream_key: str | None, payload: dict[str, Any]) -> None:
    if not stream_key:
        raise RuntimeError("notify stream key is required")
    message = {k: "" if v is None else str(v) for k, v in payload.items()}
    msg_id = await redis.xadd(stream_key, message)
    if not msg_id:
        raise RuntimeError("notify xadd returned no id")


async def start_task_outbox_dispatcher(shutdown_event: asyncio.Event | None = None):
    while shutdown_event is None or not shutdown_event.is_set():
        try:
            await reclaim_stale_task_outbox()
            await flush_pending_task_outbox()
        except Exception as exc:
            structured_log("error", "task_outbox_dispatcher_error", exception=exc)
        await asyncio.sleep(TASK_OUTBOX_POLL_SECONDS)
