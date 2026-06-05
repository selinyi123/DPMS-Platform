
import asyncio, time

from app.db import redis

from app.utils.log import structured_log



STREAM_KEY = "lottery_tasks"

GROUP_NAME = "workers"

RECOVERY_CONSUMER = "recovery-daemon"

MAX_RECOVERY_COUNT = 3



async def start_recovery_daemon():

    while True:

        try:

            now_ms = int(time.time() * 1000)

            pending = await redis.xpending_range(

                STREAM_KEY, GROUP_NAME, min="-", max="+", count=50

            )

            for msg in pending:

                idle_ms = now_ms - msg["idle"]

                if idle_ms < 120_000:

                    continue



                claimed = await redis.xclaim(

                    STREAM_KEY, GROUP_NAME, RECOVERY_CONSUMER,

                    min_idle_time=120_000,

                    message_ids=[msg["message_id"]]

                )

                if not claimed:

                    continue



                message_id, fields = claimed[0]

                task_id = fields.get("task_id", message_id)



                recovery_key = f"recovery_count:{task_id}"

                current_count = int(await redis.get(recovery_key) or 0)



                if current_count >= MAX_RECOVERY_COUNT:

                    structured_log("error", "task_permanent_failure",

                                   task_id=task_id, recovery_count=current_count)

                    await redis.xack(STREAM_KEY, GROUP_NAME, msg["message_id"])

                    await redis.delete(recovery_key)

                    continue



                new_count = await redis.incr(recovery_key)

                await redis.expire(recovery_key, 86400)



                structured_log("warning", "recovered_pending_task",

                               task_id=task_id, recovery_count=new_count)



                retry_msg_id = await redis.xadd(STREAM_KEY, {

                    "task_id": task_id,

                    "resume_from_phase": "latest",

                    "recovery_generation": str(new_count)

                })



                if retry_msg_id:

                    await redis.xack(STREAM_KEY, GROUP_NAME, msg["message_id"])

                else:

                    structured_log("error", "recovery_enqueue_failed",

                                   task_id=task_id)



        except Exception as e:

            structured_log("error", "recovery_daemon_error", exception=e)

        await asyncio.sleep(60)
