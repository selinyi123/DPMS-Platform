import base64
import json
import os
import unittest
from unittest.mock import AsyncMock, patch

# Provide a valid key/secret before importing the API module, so importing
# notify.py (which pulls in app.config / app.security) never fails on env.
os.environ.setdefault("ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode())
os.environ.setdefault("UPDATE_SECRET", "test-secret")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")

import httpx  # noqa: E402
from fastapi import BackgroundTasks, HTTPException  # noqa: E402

from app.api import notify  # noqa: E402
from app.models.schemas import NotifyRequest  # noqa: E402


def _response(status_code: int, payload=None) -> httpx.Response:
    kwargs = {"json": payload} if payload is not None else {}
    return httpx.Response(
        status_code,
        request=httpx.Request("POST", "https://example.com/hook"),
        **kwargs,
    )


class _FakeAsyncClient:
    """Minimal async-context-manager client returning a fixed response."""

    def __init__(self, response: httpx.Response):
        self._response = response

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def post(self, *args, **kwargs):
        return self._response


class NotificationSenderStatusTests(unittest.IsolatedAsyncioTestCase):
    """P1-6: senders must surface non-2xx responses as failures.

    Previously the dispatcher only treated network-level exceptions as
    failures; a webhook returning 4xx/5xx with a 200-shaped body was
    silently marked as ``success``. ``raise_for_status`` makes such
    responses bubble up so ``dispatch_notification`` records the failure.
    """

    async def test_send_webhook_raises_on_http_error(self):
        with patch.object(notify, "secret_value", return_value="https://example.com/hook"):
            with patch.object(notify.httpx, "AsyncClient", _FakeAsyncClient(_response(500))):
                with self.assertRaises(httpx.HTTPStatusError):
                    await notify.send_webhook("title", "content")

    async def test_send_webhook_succeeds_on_2xx(self):
        with patch.object(notify, "secret_value", return_value="https://example.com/hook"):
            with patch.object(notify.httpx, "AsyncClient", _FakeAsyncClient(_response(200))):
                await notify.send_webhook("title", "content")  # should not raise

    async def test_webhook_payload_reaches_in_process_mock_transport(self):
        requests = []

        async def handler(request: httpx.Request):
            requests.append(request)
            return httpx.Response(204, request=request)

        transport = httpx.MockTransport(handler)
        real_async_client = httpx.AsyncClient

        def client_factory(*args, **kwargs):
            return real_async_client(transport=transport)

        with (
            patch.object(
                notify,
                "secret_value",
                AsyncMock(return_value="https://notify.invalid/dpms"),
            ),
            patch.object(notify, "validate_secret_value"),
            patch.object(notify.httpx, "AsyncClient", client_factory),
        ):
            await notify.send_webhook("mock title", "mock content")

        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].method, "POST")
        self.assertEqual(
            json.loads(requests[0].content),
            {"title": "mock title", "content": "mock content"},
        )

    async def test_send_feishu_raises_on_http_error(self):
        with (
            patch.object(
                notify,
                "secret_value",
                return_value="https://open.feishu.cn/hook",
            ),
            patch.object(notify, "validate_secret_value"),
            patch.object(
                notify.httpx,
                "AsyncClient",
                _FakeAsyncClient(_response(403)),
            ),
        ):
            with self.assertRaises(httpx.HTTPStatusError):
                await notify.send_feishu("title", "content")

    def test_feishu_webhook_rejects_private_destination(self):
        with self.assertRaises(HTTPException):
            notify.validate_secret_value(
                "FEISHU_WEBHOOK",
                "https://127.0.0.1/internal-hook",
            )

    async def test_send_telegram_raises_on_http_error(self):
        async def fake_secret(key_name):
            return "token" if key_name == "TELEGRAM_BOT_TOKEN" else "chat-id"

        with patch.object(notify, "secret_value", side_effect=fake_secret):
            with patch.object(notify.httpx, "AsyncClient", _FakeAsyncClient(_response(401))):
                with self.assertRaises(httpx.HTTPStatusError):
                    await notify.send_telegram("title", "content")

    async def test_send_serverchan_raises_on_http_error(self):
        with patch.object(notify, "secret_value", return_value="serverchan-key"):
            with patch.object(notify.httpx, "AsyncClient", _FakeAsyncClient(_response(500))):
                with self.assertRaises(httpx.HTTPStatusError):
                    await notify.send_serverchan("title", "content")

    async def test_serverchan_2xx_business_failure_is_rejected(self):
        with (
            patch.object(notify, "secret_value", return_value="test-key"),
            patch.object(
                notify.httpx,
                "AsyncClient",
                _FakeAsyncClient(_response(200, {"code": 1})),
            ),
        ):
            with self.assertRaisesRegex(
                notify.NotificationDeliveryContractError,
                "notification_serverchan_business_rejected",
            ):
                await notify.send_serverchan("title", "content")

    async def test_feishu_and_telegram_require_business_success(self):
        with (
            patch.object(
                notify,
                "secret_value",
                return_value="https://open.feishu.cn/hook",
            ),
            patch.object(notify, "validate_secret_value"),
            patch.object(
                notify.httpx,
                "AsyncClient",
                _FakeAsyncClient(_response(200, {"code": 9499})),
            ),
        ):
            with self.assertRaisesRegex(
                notify.NotificationDeliveryContractError,
                "notification_feishu_business_rejected",
            ):
                await notify.send_feishu("title", "content")

        async def telegram_secret(key_name):
            return "token" if key_name == "TELEGRAM_BOT_TOKEN" else "chat"

        with (
            patch.object(
                notify,
                "secret_value",
                side_effect=telegram_secret,
            ),
            patch.object(
                notify.httpx,
                "AsyncClient",
                _FakeAsyncClient(_response(200, {"ok": False})),
            ),
        ):
            with self.assertRaisesRegex(
                notify.NotificationDeliveryContractError,
                "notification_telegram_business_rejected",
            ):
                await notify.send_telegram("title", "content")

    async def test_unconfigured_channel_is_rejected_before_queueing(self):
        payload = NotifyRequest(
            channel="serverchan",
            title="test",
            content="test",
        )
        with (
            patch.object(notify, "require_min_role", return_value={"actor_id": "tester"}),
            patch.object(
                notify,
                "notification_config_revision",
                AsyncMock(return_value=None),
            ),
            patch.object(notify.database, "execute", AsyncMock()) as execute,
        ):
            with self.assertRaises(HTTPException) as raised:
                await notify.send_notification(
                    payload,
                    BackgroundTasks(),
                    object(),
                )

        self.assertEqual(raised.exception.status_code, 409)
        execute.assert_not_awaited()

    async def test_dispatch_skip_log_has_non_failure_status(self):
        rows = [
            {
                "id": 7,
                "channel": "dispatch",
                "title": "event",
                "content": "EVENT: test\nSKIPPED: no notification channels configured",
                "success": 0,
                "config_revision": "2:" + "b" * 64,
            }
        ]
        with patch.object(
            notify.database,
            "fetch_all",
            AsyncMock(return_value=rows),
        ):
            result = await notify.list_notify_logs()

        self.assertEqual(result[0]["delivery_status"], "skipped")
        self.assertNotIn("config_revision", result[0])

    async def test_channels_do_not_expose_internal_revision(self):
        with patch.object(
            notify,
            "notification_config_revision",
            AsyncMock(return_value="2:" + "b" * 64),
        ):
            channels = await notify.list_channels()

        self.assertTrue(all(item["configured"] for item in channels))
        self.assertTrue(
            all("config_revision" not in item for item in channels)
        )

    async def test_status_does_not_expose_matching_internal_revision(self):
        revision = "2:" + "b" * 64
        channels = [
            {"id": "webhook", "label": "Webhook", "configured": True},
        ]
        latest = {
            "id": 8,
            "title": "test",
            "content": "delivered",
            "success": 1,
            "config_revision": revision,
            "created_at": "2026-08-01T00:00:00",
        }
        with (
            patch.object(
                notify,
                "list_channels",
                AsyncMock(return_value=channels),
            ),
            patch.object(
                notify,
                "notification_config_revision",
                AsyncMock(return_value=revision),
            ),
            patch.object(
                notify.database,
                "fetch_one",
                AsyncMock(side_effect=[latest, None]),
            ),
        ):
            status = await notify.notification_status()

        self.assertTrue(status["channels"][0]["healthy"])
        self.assertNotIn(
            "config_revision",
            status["channels"][0]["last_log"],
        )

    async def test_configured_but_untested_channel_is_not_healthy(self):
        channels = [
            {
                "id": "webhook",
                "label": "Webhook",
                "configured": True,
            }
        ]
        with (
            patch.object(
                notify,
                "list_channels",
                AsyncMock(return_value=channels),
            ),
            patch.object(
                notify,
                "notification_config_revision",
                AsyncMock(return_value=None),
            ),
            patch.object(
                notify.database,
                "fetch_one",
                AsyncMock(side_effect=[None, None]),
            ),
        ):
            status = await notify.notification_status()

        self.assertEqual(status["configured_count"], 1)
        self.assertFalse(status["channels"][0]["healthy"])
        self.assertTrue(
            status["channels"][0]["verification_required"]
        )

    async def test_save_secret_persists_only_encrypted_value(self):
        execute = AsyncMock()
        with (
            patch.object(
                notify.cookie_vault,
                "encrypt",
                return_value=b"sealed-notification-secret",
            ) as encrypt,
            patch.object(
                notify.database,
                "fetch_one",
                AsyncMock(return_value=None),
            ),
            patch.object(notify.database, "execute", execute),
        ):
            await notify.save_secret(
                "GENERIC_WEBHOOK_URL",
                "https://notify.invalid/test-only",
            )

        encrypt.assert_called_once_with(
            "https://notify.invalid/test-only",
            aad=notify.notification_secret_aad(
                "GENERIC_WEBHOOK_URL"
            ),
        )
        persisted = execute.await_args.args[1]
        self.assertEqual(
            persisted["encrypted_value"],
            b"sealed-notification-secret",
        )
        self.assertNotIn("notify.invalid", repr(persisted))

    async def test_dispatch_persists_safe_http_error_without_secret_url(self):
        secret_marker = "do-not-persist-this-token"
        request = httpx.Request(
            "POST",
            f"https://api.telegram.org/bot{secret_marker}/sendMessage",
        )
        response = httpx.Response(502, request=request)
        sender_error = httpx.HTTPStatusError(
            "upstream rejected secret-bearing URL",
            request=request,
            response=response,
        )
        execute = AsyncMock()
        record = AsyncMock()

        with (
            patch.dict(
                notify.SENDERS,
                {"telegram": AsyncMock(side_effect=sender_error)},
            ),
            patch.object(notify.database, "execute", execute),
            patch.object(notify, "record_event", record),
            patch.object(
                notify,
                "notification_config_revision",
                AsyncMock(return_value="3:" + "a" * 64),
            ),
        ):
            await notify.dispatch_notification(
                9,
                "telegram",
                "title",
                "content",
                "3:" + "a" * 64,
            )

        persisted = execute.await_args.args[1]
        self.assertEqual(
            persisted["err"],
            "notification_http_status:502",
        )
        event_payload = record.await_args.kwargs["payload"]
        self.assertEqual(
            event_payload["error_code"],
            "notification_http_status:502",
        )
        self.assertNotIn(secret_marker, repr(execute.await_args))
        self.assertNotIn(secret_marker, repr(record.await_args))

    def test_effective_env_secret_change_changes_non_secret_revision(self):
        with patch.object(
            notify.settings,
            "encryption_key",
            base64.b64encode(b"r" * 32).decode(),
        ):
            first = notify._notification_revision_token(
                "serverchan",
                4,
                [("SERVERCHAN_KEY", "test-value-a")],
            )
            second = notify._notification_revision_token(
                "serverchan",
                4,
                [("SERVERCHAN_KEY", "test-value-b")],
            )
        self.assertNotEqual(first, second)
        self.assertEqual(notify.notification_config_epoch(first), 4)
        self.assertNotIn("test-value", first)

    async def test_config_guide_requires_one_channel_not_every_secret(self):
        channels = [
            {"id": "serverchan", "label": "ServerChan", "configured": False},
            {"id": "feishu", "label": "Feishu", "configured": False},
            {"id": "webhook", "label": "Webhook", "configured": False},
            {"id": "telegram", "label": "Telegram", "configured": False},
        ]
        with (
            patch.object(notify, "list_channels", AsyncMock(return_value=channels)),
            patch.object(
                notify,
                "secret_configured",
                AsyncMock(return_value=False),
            ),
        ):
            guide = await notify.notification_config_guide()

        self.assertFalse(guide["production_ready"])
        self.assertEqual(guide["required_channel_count"], 1)
        self.assertEqual(guide["missing_required"], ["SERVERCHAN_KEY"])
        self.assertEqual(
            guide["missing_by_channel"]["telegram"],
            ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"],
        )

    async def test_config_guide_has_no_minimum_bundle_when_ready(self):
        channels = [
            {"id": "serverchan", "label": "ServerChan", "configured": True},
            {"id": "feishu", "label": "Feishu", "configured": False},
            {"id": "webhook", "label": "Webhook", "configured": False},
            {"id": "telegram", "label": "Telegram", "configured": False},
        ]

        async def configured(key_name):
            return key_name == "SERVERCHAN_KEY"

        with (
            patch.object(notify, "list_channels", AsyncMock(return_value=channels)),
            patch.object(
                notify,
                "secret_configured",
                AsyncMock(side_effect=configured),
            ),
        ):
            guide = await notify.notification_config_guide()

        self.assertTrue(guide["production_ready"])
        self.assertEqual(guide["missing_required"], [])
        self.assertEqual(guide["minimum_env_bundle"], "")


if __name__ == "__main__":
    unittest.main()
