import unittest
from unittest.mock import AsyncMock, patch

from app.services import notification_dispatcher


class NotificationDispatcherRetentionTests(
    unittest.IsolatedAsyncioTestCase
):
    def test_consumer_identity_is_unique_and_managed(self):
        first = notification_dispatcher._new_notification_consumer_name()
        second = notification_dispatcher._new_notification_consumer_name()

        self.assertNotEqual(first, second)
        self.assertTrue(
            first.startswith(
                notification_dispatcher.NOTIFICATION_CONSUMER_PREFIX
            )
        )
        self.assertEqual(len(first.rsplit(":", 1)[-1]), 32)
        self.assertGreater(
            notification_dispatcher
            .NOTIFICATION_RECLAIM_IDLE_MILLISECONDS,
            (
                notification_dispatcher.NOTIFICATION_RECLAIM_COUNT
                * notification_dispatcher
                .NOTIFICATION_HANDLER_TIMEOUT_SECONDS
                * 1000
            ),
        )

    async def test_terminal_ack_retains_entry_while_peer_group_blocks(self):
        fake_redis = AsyncMock()
        fake_redis.eval = AsyncMock(return_value=[1, 0])
        with patch.object(
            notification_dispatcher,
            "redis",
            fake_redis,
        ):
            result = await (
                notification_dispatcher
                ._ack_terminal_notification("7-0")
            )

        self.assertEqual(result, {"acknowledged": 1, "deleted": 0})
        args = fake_redis.eval.await_args.args
        self.assertIn("XINFO", args[0])
        self.assertIn("XPENDING", args[0])
        self.assertIn("XDEL", args[0])
        self.assertEqual(
            args[1:],
            (
                1,
                notification_dispatcher.STREAM_KEY,
                notification_dispatcher.GROUP_NAME,
                "7-0",
            ),
        )
        fake_redis.xack.assert_not_awaited()
        fake_redis.xdel.assert_not_awaited()

    async def test_terminal_ack_accepts_delete_after_prior_ack(self):
        fake_redis = AsyncMock()
        fake_redis.eval = AsyncMock(return_value=[0, 1])
        with patch.object(
            notification_dispatcher,
            "redis",
            fake_redis,
        ):
            result = await (
                notification_dispatcher
                ._ack_terminal_notification("7-1")
            )

        self.assertEqual(result, {"acknowledged": 0, "deleted": 1})

    async def test_handler_failure_leaves_delivery_pending(self):
        fake_redis = AsyncMock()
        with (
            patch.object(
                notification_dispatcher,
                "redis",
                fake_redis,
            ),
            patch.object(
                notification_dispatcher,
                "handle_event",
                new=AsyncMock(side_effect=RuntimeError("sender failed")),
            ),
            self.assertRaisesRegex(RuntimeError, "sender failed"),
        ):
            await notification_dispatcher._process_notification_entries(
                [("8-0", {"title": "test"})]
            )

        fake_redis.eval.assert_not_awaited()

    async def test_stale_pending_delivery_is_bounded_and_reprocessed(self):
        fake_redis = AsyncMock()
        fake_redis.xpending_range = AsyncMock(
            return_value=[
                {
                    "message_id": "9-0",
                    "time_since_delivered": (
                        notification_dispatcher
                        .NOTIFICATION_RECLAIM_IDLE_MILLISECONDS
                    ),
                },
                {
                    "message_id": "9-1",
                    "time_since_delivered": 1,
                },
            ]
        )
        fake_redis.xclaim = AsyncMock(
            return_value=[
                ("9-0", {"title": "retry"}),
            ]
        )
        fake_redis.eval = AsyncMock(return_value=[1, 1])
        consumer = notification_dispatcher._new_notification_consumer_name()

        with (
            patch.object(
                notification_dispatcher,
                "redis",
                fake_redis,
            ),
            patch.object(
                notification_dispatcher,
                "handle_event",
                new=AsyncMock(),
            ) as handle,
        ):
            reclaimed = await (
                notification_dispatcher
                ._reclaim_stale_notifications(consumer)
            )

        self.assertEqual(reclaimed, 1)
        handle.assert_awaited_once_with({"title": "retry"})
        self.assertEqual(
            fake_redis.xpending_range.await_args.kwargs["count"],
            notification_dispatcher.NOTIFICATION_RECLAIM_COUNT,
        )
        fake_redis.xclaim.assert_awaited_once_with(
            notification_dispatcher.STREAM_KEY,
            notification_dispatcher.GROUP_NAME,
            consumer,
            min_idle_time=(
                notification_dispatcher
                .NOTIFICATION_RECLAIM_IDLE_MILLISECONDS
            ),
            message_ids=["9-0"],
        )
        fake_redis.eval.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
