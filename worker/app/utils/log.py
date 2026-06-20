
import json

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


def _redact_extra(key, value):
    lowered = str(key).lower()
    if any(part in lowered for part in SENSITIVE_LOG_PARTS):
        return "<redacted>"
    return str(value)


def structured_log(level: str, event: str, **kwargs):

    record = {"ts": datetime.now(timezone.utc).isoformat(), "level": level, "event": event, **{key: _redact_extra(key, value) for key, value in kwargs.items()}}

    print(json.dumps(record, ensure_ascii=False, default=str))
