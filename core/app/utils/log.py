
import asyncio
from itertools import islice
import json

from datetime import datetime, timezone


# Mirrors app.security.SENSITIVE_KEY_PARTS so structured logs and audit details
# redact consistently (Phase 4). A kwarg whose name contains any of these is
# masked before it reaches stdout or the structured_logs stream.
SENSITIVE_LOG_PARTS = (
    "cookie",
    "credential",
    "password",
    "secret",
    "token",
    "signature",
    "webhook",
    "key",
    "authorization",
)
ERROR_LOG_KEYS = frozenset({"error", "exception", "exc"})
MAX_LOG_FIELD_CHARS = 512
MAX_LOG_COLLECTION_ITEMS = 32
MAX_PENDING_LOG_PUBLISHES = 128
_pending_log_publishes: set[asyncio.Task] = set()


def _bounded_text(value) -> str:
    rendered = str(value)
    if len(rendered) <= MAX_LOG_FIELD_CHARS:
        return rendered
    return rendered[:MAX_LOG_FIELD_CHARS] + "<truncated>"


def _sanitized_value(value, *, depth: int = 0):
    if depth >= 4:
        return "<max-depth>"
    if value is None:
        return None
    if isinstance(value, BaseException):
        return f"<{type(value).__name__}>"
    if isinstance(value, dict):
        sanitized = {}
        for index, (key, nested) in enumerate(value.items()):
            if index >= MAX_LOG_COLLECTION_ITEMS:
                sanitized["<truncated>"] = True
                break
            sanitized[str(key)[:128]] = _redact_extra(
                key,
                nested,
                depth=depth + 1,
            )
        return sanitized
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(islice(iter(value), MAX_LOG_COLLECTION_ITEMS + 1))
        sanitized = [
            _sanitized_value(item, depth=depth + 1)
            for item in items[:MAX_LOG_COLLECTION_ITEMS]
        ]
        if len(items) > MAX_LOG_COLLECTION_ITEMS:
            sanitized.append("<truncated>")
        return sanitized
    return _bounded_text(value)


def _redact_extra(key, value, *, depth: int = 0):
    lowered = str(key).lower()
    if (
        lowered in ERROR_LOG_KEYS
        or any(part in lowered for part in SENSITIVE_LOG_PARTS)
    ):
        return "<redacted>"
    return _sanitized_value(value, depth=depth)



def structured_log(level: str, event: str, **kwargs):

    # Pop the exception before building ``extra`` so it is reported once (as
    # ``record["exception"]``) instead of also leaking into ``extra``.
    exception = kwargs.pop("exception", None)

    record = {

        "ts": datetime.now(timezone.utc).isoformat(),

        "level": _bounded_text(level),

        "event": _bounded_text(event),

        "trace_id": _redact_extra(
            "trace_id",
            kwargs.pop("trace_id", None),
        ),

        "worker_id": _redact_extra(
            "worker_id",
            kwargs.pop("worker_id", None),
        ),

        "account_id": _redact_extra(
            "account_id",
            kwargs.pop("account_id", None),
        ),

        "task_id": _redact_extra(
            "task_id",
            kwargs.pop("task_id", None),
        ),

        "proxy_id": _redact_extra(
            "proxy_id",
            kwargs.pop("proxy_id", None),
        ),

        "browser_id": _redact_extra(
            "browser_id",
            kwargs.pop("browser_id", None),
        ),

        "phase": _redact_extra(
            "phase",
            kwargs.pop("phase", None),
        ),

        "latency_ms": _redact_extra(
            "latency_ms",
            kwargs.pop("latency_ms", None),
        ),

        "extra": {
            key: _redact_extra(key, value)
            for key, value in list(kwargs.items())[
                :MAX_LOG_COLLECTION_ITEMS
            ]
        },

    }

    if exception is not None:
        record["exception_type"] = type(exception).__name__
        record["exception"] = "<redacted>"

    print(json.dumps(record, ensure_ascii=False, default=str))

    try:

        from app.db import redis

        loop = asyncio.get_running_loop()
        if len(_pending_log_publishes) >= MAX_PENDING_LOG_PUBLISHES:
            return
        task = loop.create_task(
            safe_publish(
                redis,
                "structured_logs",
                json.dumps(record, ensure_ascii=False, default=str),
            )
        )
        _pending_log_publishes.add(task)
        task.add_done_callback(_pending_log_publishes.discard)

    except Exception:

        pass



async def safe_publish(redis_client, channel, message):

    try:

        await redis_client.publish(channel, message)

    except Exception:

        pass
