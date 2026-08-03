import databases

import redis.asyncio as aioredis

from app.config import settings
from app.utils.log import structured_log


# Keep every pooled session on the same clock contract as Core. In particular,
# TIMESTAMP risk deadlines must not change meaning when a host/server default
# time zone differs between deployment units.
database = databases.Database(
    settings.database_url,
    init_command="SET time_zone = '+00:00'",
)


async def execute_affected_rows(query, values=None, *, db=None) -> int:
    """Execute MySQL DML and read the connection-local affected-row count."""

    target = db or database
    async with target.transaction():
        await target.execute(query, values)
        row = await target.fetch_one("SELECT ROW_COUNT() AS affected")
    if row is None:
        raise RuntimeError("database_affected_row_count_unavailable")
    affected = int(row["affected"])
    if affected < 0:
        raise RuntimeError("database_affected_row_count_invalid")
    return affected


class RedisClient:
    def __init__(self):
        self._conn = None

    async def initialize(self):
        auth_options = {}
        if settings.redis_username:
            auth_options["username"] = settings.redis_username
        if settings.redis_password:
            auth_options["password"] = settings.redis_password
        self._conn = aioredis.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
            max_connections=settings.redis_max_connections,
            socket_timeout=settings.redis_socket_timeout_seconds,
            socket_connect_timeout=settings.redis_connect_timeout_seconds,
            **auth_options,
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
