
import databases

import redis.asyncio as aioredis

from app.config import settings



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



    def __getattr__(self, name):

        if self._conn is None:

            raise RuntimeError("Redis client is not initialized")

        return getattr(self._conn, name)



redis = RedisClient()
