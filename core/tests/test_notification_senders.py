import base64
import os
import unittest
from unittest.mock import patch

# Provide a valid key/secret before importing the API module, so importing
# notify.py (which pulls in app.config / app.security) never fails on env.
os.environ.setdefault("ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode())
os.environ.setdefault("UPDATE_SECRET", "test-secret")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")

import httpx  # noqa: E402

from app.api import notify  # noqa: E402


def _response(status_code: int) -> httpx.Response:
    return httpx.Response(status_code, request=httpx.Request("POST", "https://example.com/hook"))


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

    async def test_send_feishu_raises_on_http_error(self):
        with patch.object(notify, "secret_value", return_value="https://open.feishu.cn/hook"):
            with patch.object(notify.httpx, "AsyncClient", _FakeAsyncClient(_response(403))):
                with self.assertRaises(httpx.HTTPStatusError):
                    await notify.send_feishu("title", "content")

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


if __name__ == "__main__":
    unittest.main()
