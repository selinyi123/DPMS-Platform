import json
import uuid
from typing import Any

from app.db import database
from app.utils.log import structured_log


async def ensure_event_schema() -> None:
    await database.execute(
        """CREATE TABLE IF NOT EXISTS events (
          id CHAR(36) PRIMARY KEY,
          aggregate VARCHAR(64) NOT NULL,
          aggregate_id VARCHAR(128) NOT NULL,
          event_type VARCHAR(128) NOT NULL,
          payload JSON NULL,
          correlation_id VARCHAR(128) NULL,
          causation_id VARCHAR(128) NULL,
          actor_type VARCHAR(32) NOT NULL DEFAULT 'system',
          actor_id VARCHAR(128) NULL,
          source_service VARCHAR(64) NOT NULL DEFAULT 'worker',
          occurred_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
          INDEX idx_events_aggregate (aggregate, aggregate_id, occurred_at),
          INDEX idx_events_type_created (event_type, occurred_at),
          INDEX idx_events_correlation (correlation_id, occurred_at)
        ) ENGINE=InnoDB"""
    )


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
    source_service: str = "worker",
) -> str | None:
    event_id = str(uuid.uuid4())
    try:
        await database.execute(
            """INSERT INTO events
               (id, aggregate, aggregate_id, event_type, payload, correlation_id, causation_id, actor_type, actor_id, source_service)
               VALUES (:id, :aggregate, :aggregate_id, :event_type, :payload, :correlation_id, :causation_id, :actor_type, :actor_id, :source_service)""",
            {
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
            },
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
