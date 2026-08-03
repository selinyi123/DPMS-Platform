import base64
import os
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("ENCRYPTION_KEY", base64.b64encode(b"n" * 32).decode())
os.environ.setdefault("UPDATE_SECRET", "test-update-secret")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")

from app.api import notify


class NotificationIdempotencyTests(unittest.IsolatedAsyncioTestCase):
    def test_delivery_key_is_stable_and_bounded(self):
        first = notify.notification_delivery_key(
            "webhook",
            stream_message_id="12-3",
        )
        second = notify.notification_delivery_key(
            "webhook",
            stream_message_id="12-3",
        )
        self.assertEqual(first, second)
        self.assertLessEqual(len(first), 191)
        self.assertNotEqual(
            first,
            notify.notification_delivery_key(
                "webhook",
                stream_message_id="12-4",
            ),
        )

    async def test_sent_claim_does_not_call_provider_again(self):
        sender = AsyncMock()
        with (
            patch.object(
                notify,
                "_claim_notification_delivery",
                AsyncMock(return_value={"status": "sent"}),
            ),
            patch.dict(notify.SENDERS, {"webhook": sender}),
            patch.object(
                notify,
                "notification_config_revision",
                AsyncMock(return_value="1:" + "a" * 64),
            ),
        ):
            await notify.dispatch_notification(
                1,
                "webhook",
                "title",
                "content",
                "1:" + "a" * 64,
                stream_message_id="10-0",
            )
        sender.assert_not_awaited()

    async def test_uncertain_claim_is_terminal_for_automatic_retry(self):
        sender = AsyncMock()
        with (
            patch.object(
                notify,
                "_claim_notification_delivery",
                AsyncMock(return_value={"status": "uncertain"}),
            ),
            patch.dict(notify.SENDERS, {"webhook": sender}),
            patch.object(
                notify,
                "notification_config_revision",
                AsyncMock(return_value="1:" + "a" * 64),
            ),
        ):
            await notify.dispatch_notification(
                1,
                "webhook",
                "title",
                "content",
                "1:" + "a" * 64,
                stream_message_id="10-0",
            )
        sender.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
