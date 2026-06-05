import json
import uuid
from typing import Any

from app.db import database
from app.utils.log import structured_log


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
) -> str | None:
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
    try:
        await database.execute(
            """INSERT INTO events
               (id, aggregate, aggregate_id, event_type, payload, correlation_id, causation_id, actor_type, actor_id, source_service)
               VALUES (:id, :aggregate, :aggregate_id, :event_type, :payload, :correlation_id, :causation_id, :actor_type, :actor_id, :source_service)""",
            values,
        )
        return event_id
    except Exception as exc:
        structured_log(
            "warning",
            "event_store_write_failed",
            aggregate=aggregate,
            aggregate_id=aggregate_id,
            event_type=event_type,
            exception=exc,
        )
        return None


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
