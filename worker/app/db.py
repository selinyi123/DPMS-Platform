import databases

import redis.asyncio as aioredis

from app.config import settings
from app.utils.log import structured_log


database = databases.Database(settings.database_url)


class RedisClient:
    def __init__(self):
        self._conn = None

    async def initialize(self):
        self._conn = aioredis.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
            max_connections=10,
        )

    async def close(self):
        if self._conn:
            await self._conn.close()

    async def xadd(self, name, fields, *args, **kwargs):
        from_task_outbox = bool(kwargs.pop("_from_task_outbox", False))
        if self._conn is None:
            raise RuntimeError("Redis client is not initialized")
        if name == "notify_events" and _terminal_task_notice(fields) and not from_task_outbox:
            structured_log(
                "info",
                "terminal_notice_covered_by_outbox",
                stream=name,
                task_id=fields.get("task_id") if isinstance(fields, dict) else None,
                status=fields.get("status") if isinstance(fields, dict) else None,
            )
            return "covered-by-task-outbox"
        try:
            return await self._conn.xadd(name, fields, *args, **kwargs)
        except Exception as exc:
            if name == "notify_events":
                structured_log("warning", "notify_enqueue_failed", stream=name, error=str(exc))
                return None
            raise

    def __getattr__(self, name):
        if self._conn is None:
            raise RuntimeError("Redis client is not initialized")
        return getattr(self._conn, name)


def _terminal_task_notice(fields) -> bool:
    if not isinstance(fields, dict):
        return False
    status = str(fields.get("status") or "").lower()
    return bool(fields.get("task_id")) and status in {"succeeded", "failed"}


redis = RedisClient()
