
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

            max_connections=10

        )



    async def close(self):

        if self._conn:

            await self._conn.close()



    async def xadd(self, name, fields, *args, **kwargs):

        if self._conn is None:

            raise RuntimeError("Redis client is not initialized")

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



redis = RedisClient()
