"""Transactional and at-least-once contracts for browser-login ingress."""

from __future__ import annotations

import base64
import json
import os
import unittest
import uuid
from unittest.mock import AsyncMock, patch


os.environ.setdefault(
    "ENCRYPTION_KEY",
    base64.b64encode(b"login-outbox-test-key-material!!").decode(),
)
os.environ.setdefault("UPDATE_SECRET", "test-secret")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")

from app.services import login_request_outbox, outbox  # noqa: E402
from shared.login_streams import LOGIN_REQUEST_STREAM_KEY  # noqa: E402


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _TerminalDatabase:
    def __init__(self, message):
        self.message = message
        self.executions = []

    def transaction(self):
        return _Transaction()

    async def fetch_one(self, query, values=None):
        if "FROM login_sessions" in query:
            return {
                "session_id": self.message["session_id"],
                "platform": self.message["platform"],
                "login_url": self.message["login_url"],
                "status": "queued",
            }
        raise AssertionError(query)

    async def execute(self, query, values=None):
        self.executions.append((query, dict(values or {})))
        return 1


class _ReconcileDatabase:
    def __init__(self):
        self.executions = []

    def transaction(self):
        return _Transaction()

    async def execute(self, query, values=None):
        self.executions.append((query, dict(values or {})))
        return 1

    async def fetch_one(self, query, values=None):
        if "ROW_COUNT()" in query:
            return {"affected": 1}
        raise AssertionError(query)


class LoginRequestOutboxTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.message = login_request_outbox.build_login_request_message(
            session_id=str(uuid.uuid4()),
            platform="douyin",
            login_url="https://example.invalid/login",
        )

    def test_message_rejects_credential_bearing_url(self):
        with self.assertRaisesRegex(
            ValueError,
            "login_request_url_invalid",
        ):
            login_request_outbox.build_login_request_message(
                session_id=str(uuid.uuid4()),
                platform="douyin",
                login_url="https://user:password@example.invalid/login",
            )

    async def test_enqueue_uses_session_scoped_dedup_key(self):
        enqueue = AsyncMock()
        with patch("app.services.outbox.enqueue_outbox", new=enqueue):
            await login_request_outbox.enqueue_login_request_outbox(
                self.message
            )

        enqueue.assert_awaited_once_with(
            self.message,
            LOGIN_REQUEST_STREAM_KEY,
            dedup_key=f"login-request:{self.message['session_id']}",
        )

    async def test_terminal_relay_failure_settles_exact_queued_session(self):
        database = _TerminalDatabase(self.message)
        event = AsyncMock()
        current = {
            "stream_key": LOGIN_REQUEST_STREAM_KEY,
            "dedup_key": f"login-request:{self.message['session_id']}",
            "payload": json.dumps(self.message),
        }
        with patch.object(
            login_request_outbox,
            "record_event",
            new=event,
        ):
            handled = await (
                login_request_outbox
                .settle_terminal_login_request_delivery_failure(
                    current,
                    8,
                    "sensitive transport detail",
                    db=database,
                )
            )

        self.assertTrue(handled)
        self.assertEqual(len(database.executions), 2)
        query, values = database.executions[0]
        self.assertIn("status = 'failed'", query)
        self.assertEqual(
            values["error"],
            "Login request delivery exhausted",
        )
        self.assertNotIn("sensitive", values["error"])
        cleanup_query, cleanup_values = database.executions[1]
        self.assertIn(
            "login_profile_cleanup_intents",
            cleanup_query,
        )
        self.assertEqual(
            cleanup_values["session_id"],
            self.message["session_id"],
        )
        event.assert_awaited_once()

    async def test_epoch_replay_is_scoped_to_queued_login_session(self):
        database = _ReconcileDatabase()
        epoch = AsyncMock(return_value="epoch-login")
        with (
            patch.object(outbox, "database", database),
            patch.object(
                outbox,
                "read_redis_task_stream_epoch",
                new=epoch,
            ),
            patch.object(outbox, "structured_log"),
        ):
            affected = (
                await outbox.reconcile_redis_login_request_stream_epoch()
            )

        self.assertEqual(affected, 1)
        query, values = database.executions[0]
        self.assertIn("session_row.status = 'queued'", query)
        self.assertIn("outbox_row.status = 'sent'", query)
        self.assertEqual(values["stream_key"], LOGIN_REQUEST_STREAM_KEY)
        self.assertEqual(values["observed"], "epoch-login")


if __name__ == "__main__":
    unittest.main()
