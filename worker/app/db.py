
import databases

import redis.asyncio as aioredis

from app.config import settings
from app.utils.log import structured_log


TERMINAL_TASK_STATUSES = {"succeeded", "failed"}


class WorkerDatabase:

    def __init__(self, url: str):

        self._db = databases.Database(url)


    async def execute(self, query, values=None, *args, **kwargs):

        await self._guard_terminal_task_reopen(query, values)

        return await self._db.execute(query, values, *args, **kwargs)


    async def _guard_terminal_task_reopen(self, query, values):

        text = str(query or "").strip().lower()

        if not text.startswith("update task_runs set status = 'running'"):

            return

        task_id = (values or {}).get("task_id")

        if not task_id:

            return

        row = await self._db.fetch_one("SELECT status FROM task_runs WHERE task_id = :task_id", {"task_id": task_id})

        if row and str(row["status"] or "") in TERMINAL_TASK_STATUSES:

            raise RuntimeError(f"Refusing to reopen terminal task {task_id} from {row['status']}")


    def __getattr__(self, name):

        return getattr(self._db, name)



database = WorkerDatabase(settings.database_url)



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
