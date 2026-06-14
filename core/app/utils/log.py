
import json, asyncio

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


def _redact_extra(key, value):
    lowered = str(key).lower()
    if any(part in lowered for part in SENSITIVE_LOG_PARTS):
        return "<redacted>"
    return str(value)



def structured_log(level: str, event: str, **kwargs):

    record = {

        "ts": datetime.now(timezone.utc).isoformat(),

        "level": level,

        "event": event,

        "trace_id": kwargs.pop("trace_id", None),

        "worker_id": kwargs.pop("worker_id", None),

        "account_id": kwargs.pop("account_id", None),

        "task_id": kwargs.pop("task_id", None),

        "proxy_id": kwargs.pop("proxy_id", None),

        "browser_id": kwargs.pop("browser_id", None),

        "phase": kwargs.pop("phase", None),

        "latency_ms": kwargs.pop("latency_ms", None),

        "extra": {key: _redact_extra(key, value) for key, value in kwargs.items()}

    }

    if "exception" in kwargs:

        record["exception"] = str(kwargs["exception"])

    print(json.dumps(record, ensure_ascii=False, default=str))

    try:

        from app.db import redis

        # fire-and-forget 发布到 Redis，但不阻塞调用者

        asyncio.ensure_future(safe_publish(redis, "structured_logs", json.dumps(record, ensure_ascii=False, default=str)))

    except Exception:

        pass



async def safe_publish(redis_client, channel, message):

    try:

        await redis_client.publish(channel, message)

    except Exception:

        pass
