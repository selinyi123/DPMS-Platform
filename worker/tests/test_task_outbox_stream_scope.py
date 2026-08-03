import unittest
from unittest.mock import AsyncMock, patch

from app.services import task_outbox


class TaskOutboxStreamScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_enqueue_rejects_non_notification_stream(self):
        with patch.object(
            task_outbox,
            "_enqueue",
            new=AsyncMock(),
        ) as enqueue:
            with self.assertRaisesRegex(
                ValueError,
                "task_outbox_notify_stream_invalid",
            ):
                await task_outbox.enqueue_notify_outbox(
                    stream_key="lottery_tasks:bilibili",
                    message={"task_id": "task-1"},
                    dedup_key="notice:task-1",
                )
        enqueue.assert_not_awaited()

    async def test_relay_rejects_persisted_non_notification_stream(self):
        with patch.object(
            task_outbox,
            "redis",
            new=AsyncMock(),
        ) as redis:
            with self.assertRaisesRegex(
                RuntimeError,
                "task_outbox_notify_stream_invalid",
            ):
                await task_outbox._deliver_notify(
                    "adapter_probe_requests:bilibili",
                    {"task_id": "task-1"},
                )
        redis.xadd.assert_not_awaited()

    async def test_relay_uses_fixed_notification_stream(self):
        redis = AsyncMock()
        redis.xadd.return_value = "1-0"
        with patch.object(task_outbox, "redis", redis):
            await task_outbox._deliver_notify(
                "notify_events",
                {"task_id": "task-1"},
            )
        redis.xadd.assert_awaited_once_with(
            "notify_events",
            {"task_id": "task-1"},
            _from_task_outbox=True,
        )


if __name__ == "__main__":
    unittest.main()
