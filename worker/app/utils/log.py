
import json
from itertools import islice

from datetime import datetime, timezone


# Mirrors core/app/utils/log.py SENSITIVE_LOG_PARTS so worker logs redact the
# same sensitive kwarg names (Phase 4).
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
    exception = kwargs.pop("exception", None)
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "level": _bounded_text(level),
        "event": _bounded_text(event),
        **{
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
