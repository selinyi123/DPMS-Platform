
import json

from datetime import datetime, timezone



def structured_log(level: str, event: str, **kwargs):

    record = {"ts": datetime.now(timezone.utc).isoformat(), "level": level, "event": event, **{key: str(value) for key, value in kwargs.items()}}

    print(json.dumps(record, ensure_ascii=False, default=str))
