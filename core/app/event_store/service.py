import asyncio
import json
import uuid
from typing import Any

from app.db import database
from app.utils.log import structured_log


EVENT_WRITE_ATTEMPTS = 3


def _safe_exception_code(exc: BaseException | None) -> str | None:
    if exc is None:
        return None
    candidate = str(getattr(exc, "code", "") or "").strip()
    if (
        candidate
        and len(candidate) <= 128
        and candidate.replace("_", "").replace("-", "").isalnum()
    ):
        return candidate
    return f"event_store_{type(exc).__name__}"[:128]


async def record_event(
    *,
    aggregate: str,
    aggregate_id: Any,
    event_type: str,
    payload: dict[str, Any] | None = None,
    correlation_id: Any | None = None,
    causation_id: Any | None = None,
    actor_type: str = "system",
    actor_id: Any | None = None,
    source_service: str = "core-api",
    critical: bool = False,
) -> str | None:
    """Append an event to the store.

    P2-3: a write failure is no longer silently swallowed. The insert is retried
    a few times to ride out a transient blip, and for ``critical=True`` events
    (real-run dispatch, policy activation, …) a persistent failure is logged
    without payload contents and durably captured in access-controlled
    ``failed_events`` for later replay, so the audit trail is not lost or copied
    into stdout/pubsub logs.
    """
    event_id = str(uuid.uuid4())
    values = {
        "id": event_id,
        "aggregate": aggregate,
        "aggregate_id": str(aggregate_id),
        "event_type": event_type,
        "payload": json.dumps(payload or {}, ensure_ascii=False, default=str),
        "correlation_id": str(correlation_id) if correlation_id is not None else None,
        "causation_id": str(causation_id) if causation_id is not None else None,
        "actor_type": actor_type,
        "actor_id": str(actor_id) if actor_id is not None else None,
        "source_service": source_service,
    }

    last_exc: Exception | None = None
    for attempt in range(EVENT_WRITE_ATTEMPTS):
        try:
            await database.execute(
                """INSERT INTO events
                   (id, aggregate, aggregate_id, event_type, payload, correlation_id, causation_id, actor_type, actor_id, source_service)
                   VALUES (:id, :aggregate, :aggregate_id, :event_type, :payload, :correlation_id, :causation_id, :actor_type, :actor_id, :source_service)""",
                values,
            )
            return event_id
        except Exception as exc:
            last_exc = exc
            if attempt < EVENT_WRITE_ATTEMPTS - 1:
                await asyncio.sleep(0.1 * (attempt + 1))

    if critical:
        structured_log(
            "error",
            "event_store_write_failed_critical",
            aggregate=aggregate,
            aggregate_id=aggregate_id,
            event_type=event_type,
            payload_bytes=len(values["payload"].encode("utf-8")),
            exception=last_exc,
        )
        await _dead_letter(values, last_exc)
    else:
        structured_log(
            "warning",
            "event_store_write_failed",
            aggregate=aggregate,
            aggregate_id=aggregate_id,
            event_type=event_type,
            exception=last_exc,
        )
    return None


async def _dead_letter(values: dict[str, Any], exc: Exception | None) -> None:
    """Durably capture a critical event that could not be written to ``events``.

    Uses a loose schema (TEXT payload) so an event that failed the strict
    ``events`` table can still be preserved for replay. Best-effort: if even this
    fails (e.g. the database is fully down) the ERROR log above is the record.
    """
    try:
        await database.execute(
            """INSERT INTO failed_events
               (id, aggregate, aggregate_id, event_type, payload, actor_type, actor_id, source_service, error)
               VALUES (:id, :aggregate, :aggregate_id, :event_type, :payload, :actor_type, :actor_id, :source_service, :error)""",
            {
                "id": values["id"],
                "aggregate": values["aggregate"],
                "aggregate_id": values["aggregate_id"],
                "event_type": values["event_type"],
                "payload": values["payload"],
                "actor_type": values["actor_type"],
                "actor_id": values["actor_id"],
                "source_service": values["source_service"],
                "error": _safe_exception_code(exc),
            },
        )
    except Exception as inner:
        structured_log("error", "event_dead_letter_failed", event_id=values["id"], exception=inner)


def parse_payload(value: Any) -> Any:
    if value is None or isinstance(value, (dict, list)):
        return value
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    try:
        return json.loads(value)
    except Exception:
        return value


def normalize_event_row(row: Any) -> dict[str, Any]:
    item = dict(row)
    item["payload"] = parse_payload(item.get("payload"))
    return item
