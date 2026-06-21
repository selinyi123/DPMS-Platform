import contextlib
import contextvars
import re

import databases

import redis.asyncio as aioredis

from app.config import settings
from app.utils.log import structured_log


_schema_write_allowed = contextvars.ContextVar("schema_write_allowed", default=False)


DDL_RE = re.compile(r"^\s*(ALTER|CREATE|DROP|RENAME|TRUNCATE)\s+", re.IGNORECASE)
RUNTIME_SEED_RE = re.compile(
    r"^\s*INSERT\s+INTO\s+(runtime_settings|circuit_breakers|policy_versions)\b",
    re.IGNORECASE,
)


def production_mode() -> bool:
    return str(settings.deployment_mode or "").strip().lower() == "production"


@contextlib.contextmanager
def allow_schema_writes():
    token = _schema_write_allowed.set(True)
    try:
        yield
    finally:
        _schema_write_allowed.reset(token)


def _query_text(query) -> str:
    return str(query or "")


def _is_runtime_schema_write(query) -> bool:
    text = _query_text(query)
    return bool(DDL_RE.search(text) or RUNTIME_SEED_RE.search(text))


class GuardedDatabase:
    def __init__(self, url: str):
        self._inner = databases.Database(url)

    async def execute(self, query, values=None, *args, **kwargs):
        if production_mode() and not _schema_write_allowed.get() and _is_runtime_schema_write(query):
            text = _query_text(query).strip().splitlines()[0][:160]
            structured_log(
                "warning",
                "runtime_schema_write_skipped_in_production",
                statement=text,
            )
            return None
        return await self._inner.execute(query, values=values, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


database = GuardedDatabase(settings.database_url)


class RedisClient:

    def __init__(self):

        self._conn = None



    async def initialize(self):

        self._conn = aioredis.from_url(

            settings.redis_url,

            encoding="utf-8",

            decode_responses=True,

            max_connections=10

        )

        try:

            await self._conn.xgroup_create("lottery_tasks", "workers", id="0", mkstream=True)

        except aioredis.ResponseError as e:

            if "BUSYGROUP" not in str(e):

                raise



    async def close(self):

        if self._conn:

            await self._conn.close()



    def __getattr__(self, name):

        if self._conn is None:

            raise RuntimeError("Redis client is not initialized")

        return getattr(self._conn, name)



redis = RedisClient()
