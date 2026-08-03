import contextlib
import contextvars
import re

import databases

import redis.asyncio as aioredis

from app.config import settings
from shared.platform_scope import normalize_platform_scope
from shared.redis_consumer_groups import (
    verify_redis_consumer_group_topology,
)
from app.utils.log import structured_log


_schema_write_allowed = contextvars.ContextVar("schema_write_allowed", default=False)


DDL_RE = re.compile(r"^\s*(ALTER|CREATE|DROP|RENAME|TRUNCATE)\s+", re.IGNORECASE)


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


def _schema_guard_sql_body(query) -> str:
    """Return the first executable SQL body after harmless leading comments.

    MySQL executes ``/*! ... */`` comments, so their body must be inspected
    rather than discarded. This helper is only a defence-in-depth classifier;
    production database privileges remain the authoritative schema boundary.
    """

    text = _query_text(query)
    while True:
        text = text.lstrip()
        if text.startswith("--") or text.startswith("#"):
            newline = text.find("\n")
            return "" if newline < 0 else _schema_guard_sql_body(text[newline + 1 :])
        if not text.startswith("/*"):
            return text
        end = text.find("*/", 2)
        if end < 0:
            return text
        comment_body = text[2:end].lstrip()
        if comment_body.startswith("!"):
            executable = re.sub(r"^!\d*\s*", "", comment_body)
            # MySQL may split a statement across the executable comment and
            # ordinary SQL, e.g. ``/*!50000 CREATE*/ TABLE x``.
            return f"{executable} {text[end + 2 :]}".strip()
        text = text[end + 2 :]


def _is_schema_write(query) -> bool:
    text = _schema_guard_sql_body(query)
    # Runtime settings, circuit breakers, and policy versions are ordinary
    # application data. Blocking their INSERT/UPSERT statements in production
    # silently disables safety controls. Only actual DDL belongs behind the
    # migration-only schema-write context.
    return bool(DDL_RE.search(text))


class GuardedDatabase:
    def __init__(self, url: str, **options):
        self._inner = databases.Database(url, **options)

    async def execute(self, query, values=None, *args, **kwargs):
        if production_mode() and not _schema_write_allowed.get() and _is_schema_write(query):
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


# All persisted timestamps and evidence cut-offs are interpreted as UTC.
# Configure every pooled MySQL session rather than relying on the server's
# mutable global/default time zone.
database = GuardedDatabase(
    settings.database_url,
    init_command="SET time_zone = '+00:00'",
)


async def execute_affected_rows(query, values=None, *, db=None) -> int:
    """Execute one MySQL DML statement and return its real affected-row count.

    ``databases`` returns the driver's ``lastrowid`` from ``execute()``.  That
    is useful for INSERTs, but it is not an affected-row count for conditional
    UPDATE/DELETE statements.  ``ROW_COUNT()`` is connection-local, so both
    statements must run in the same transaction-bound connection.
    """

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



    async def initialize(
        self,
        *,
        platforms=None,
        include_shared: bool = True,
    ):

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

        if platforms is None:
            selected_platforms = frozenset(
                normalize_platform_scope("all")
            )
        else:
            platform_values = (
                (platforms,)
                if isinstance(platforms, str)
                else tuple(platforms)
            )
            selected_platforms = (
                frozenset(normalize_platform_scope(platform_values))
                if platform_values
                else frozenset()
            )
        await verify_redis_consumer_group_topology(
            self._conn,
            role="core",
            platforms=selected_platforms,
            include_shared=include_shared,
        )



    async def close(self):

        if self._conn:

            await self._conn.close()



    def __getattr__(self, name):

        if self._conn is None:

            raise RuntimeError("Redis client is not initialized")

        return getattr(self._conn, name)



redis = RedisClient()
