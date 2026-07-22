import asyncio
import base64
import os
import sys
import unittest
from pathlib import Path


os.environ.setdefault("ENCRYPTION_KEY", base64.b64encode(b"0" * 32).decode())
os.environ.setdefault("DATABASE_URL", "mysql+aiomysql://u:p@localhost:3306/lottery")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import safety  # noqa: E402


class FakeDatabase:
    def __init__(
        self,
        *,
        fail_update: bool = False,
        fail_audit: bool = False,
        fail_commit: bool = False,
    ):
        self.fail_update = fail_update
        self.fail_audit = fail_audit
        self.fail_commit = fail_commit
        self.executions = []
        self.transaction_entered = False
        self.transaction_rolled_back = False
        self.account_status = "ready"
        self.risk_events = []
        self._pending_status = None
        self._pending_risk_events = []

    def transaction(self):
        database = self

        class Transaction:
            async def __aenter__(self):
                database.transaction_entered = True
                database._pending_status = None
                database._pending_risk_events = []
                return database

            async def __aexit__(self, exc_type, _exc, _tb):
                if exc_type is not None:
                    database.transaction_rolled_back = True
                    database._pending_status = None
                    database._pending_risk_events = []
                    return False
                if database.fail_commit:
                    database.transaction_rolled_back = True
                    database._pending_status = None
                    database._pending_risk_events = []
                    raise RuntimeError("account status commit unavailable")
                database.transaction_rolled_back = False
                if database._pending_status is not None:
                    database.account_status = database._pending_status
                database.risk_events.extend(database._pending_risk_events)
                return False

        return Transaction()

    async def execute(self, query, values=None):
        self.executions.append((query, dict(values or {})))
        if "UPDATE accounts" in query:
            if self.fail_update:
                raise RuntimeError("account status update unavailable")
            self._pending_status = values["status"]
        if self.fail_audit and "INSERT INTO risk_events" in query:
            raise RuntimeError("risk audit unavailable")
        if "INSERT INTO risk_events" in query:
            self._pending_risk_events.append(dict(values or {}))
        return 1


class FakeRedis:
    def __init__(self, *, fail: bool = False, result="1-0", cancel: bool = False):
        self.fail = fail
        self.result = result
        self.cancel = cancel
        self.messages = []

    async def xadd(self, stream, message):
        self.messages.append((stream, dict(message)))
        if self.cancel:
            raise asyncio.CancelledError()
        if self.fail:
            raise RuntimeError("notification stream unavailable")
        return self.result


class CancelledAuditDatabase(FakeDatabase):
    async def execute(self, query, values=None):
        if "INSERT INTO risk_events" in query:
            self.executions.append((query, dict(values or {})))
            raise asyncio.CancelledError()
        return await super().execute(query, values)


class AccountStatusPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_database = safety.database
        self.original_redis = safety.redis
        self.original_structured_log = safety.structured_log
        self.logs = []
        safety.structured_log = lambda level, event, **fields: self.logs.append(
            (level, event, fields)
        )

    async def asyncTearDown(self):
        safety.database = self.original_database
        safety.redis = self.original_redis
        safety.structured_log = self.original_structured_log

    async def test_notification_failure_does_not_undo_committed_safety_state(self):
        database = FakeDatabase()
        redis = FakeRedis(fail=True)
        safety.database = database
        safety.redis = redis

        await safety.set_account_status(42, "cooling", "bilibili_comment_captcha")

        self.assertTrue(database.transaction_entered)
        self.assertFalse(database.transaction_rolled_back)
        self.assertEqual(2, len(database.executions))
        self.assertIn("UPDATE accounts", database.executions[0][0])
        self.assertIn("INSERT INTO risk_events", database.executions[1][0])
        self.assertEqual("cooling", database.account_status)
        self.assertEqual(1, len(database.risk_events))
        self.assertEqual(1, len(redis.messages))
        self.assertIn(
            "account_status_notification_failed",
            [event for _level, event, _fields in self.logs],
        )

    async def test_falsey_notification_result_is_logged_without_failing_status(self):
        database = FakeDatabase()
        safety.database = database
        safety.redis = FakeRedis(result=None)

        await safety.set_account_status(42, "cooling", "bilibili_comment_captcha")

        self.assertEqual("cooling", database.account_status)
        failures = [
            fields
            for _level, event, fields in self.logs
            if event == "account_status_notification_failed"
        ]
        self.assertEqual("enqueue_unconfirmed", failures[0]["failure"])

    async def test_audit_failure_aborts_the_canonical_status_settlement(self):
        database = FakeDatabase(fail_audit=True)
        redis = FakeRedis()
        safety.database = database
        safety.redis = redis

        with self.assertRaises(safety.AccountStatusPersistenceFailed) as caught:
            await safety.set_account_status(42, "cooling", "bilibili_comment_captcha")

        self.assertTrue(database.transaction_entered)
        self.assertTrue(database.transaction_rolled_back)
        self.assertEqual(42, caught.exception.account_id)
        self.assertEqual("cooling", caught.exception.status)
        self.assertIsInstance(caught.exception.__cause__, RuntimeError)
        self.assertEqual("ready", database.account_status)
        self.assertEqual([], database.risk_events)
        self.assertEqual([], redis.messages)

    async def test_cancellation_during_audit_propagates_and_rolls_back(self):
        database = CancelledAuditDatabase()
        redis = FakeRedis()
        safety.database = database
        safety.redis = redis

        with self.assertRaises(asyncio.CancelledError):
            await safety.set_account_status(42, "cooling", "bilibili_comment_captcha")

        self.assertTrue(database.transaction_rolled_back)
        self.assertEqual("ready", database.account_status)
        self.assertEqual([], redis.messages)

    async def test_update_failure_is_unconfirmed_and_does_not_notify(self):
        database = FakeDatabase(fail_update=True)
        redis = FakeRedis()
        safety.database = database
        safety.redis = redis

        with self.assertRaises(safety.AccountStatusPersistenceFailed):
            await safety.set_account_status(42, "cooling", "bilibili_comment_captcha")

        self.assertTrue(database.transaction_rolled_back)
        self.assertEqual("ready", database.account_status)
        self.assertEqual([], database.risk_events)
        self.assertEqual([], redis.messages)

    async def test_commit_failure_is_unconfirmed_and_does_not_notify(self):
        database = FakeDatabase(fail_commit=True)
        redis = FakeRedis()
        safety.database = database
        safety.redis = redis

        with self.assertRaises(safety.AccountStatusPersistenceFailed):
            await safety.set_account_status(42, "cooling", "bilibili_comment_captcha")

        self.assertTrue(database.transaction_rolled_back)
        self.assertEqual("ready", database.account_status)
        self.assertEqual([], database.risk_events)
        self.assertEqual([], redis.messages)

    async def test_notification_cancellation_preserves_commit_and_propagates(self):
        database = FakeDatabase()
        safety.database = database
        safety.redis = FakeRedis(cancel=True)

        with self.assertRaises(asyncio.CancelledError):
            await safety.set_account_status(42, "cooling", "bilibili_comment_captcha")

        self.assertEqual("cooling", database.account_status)
        self.assertEqual(1, len(database.risk_events))


if __name__ == "__main__":
    unittest.main()
