"""Worker QR-login account/calibration/outbox atomicity tests."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from app import login_broker


class RecordingTransaction:
    def __init__(self, database):
        self.database = database

    async def __aenter__(self):
        self.database.depth += 1
        return self

    async def __aexit__(self, exc_type, *_args):
        self.database.depth -= 1
        if exc_type is not None:
            self.database.rolled_back = True
        return False


class RecordingDatabase:
    def __init__(self, *, fail_outbox: bool = False):
        self.depth = 0
        self.rolled_back = False
        self.fail_outbox = fail_outbox
        self.calls = []

    def transaction(self):
        return RecordingTransaction(self)

    async def execute(self, query, values=None):
        self.calls.append((query, values, self.depth))
        if "INSERT INTO outbox_events" in query and self.fail_outbox:
            raise RuntimeError("outbox unavailable")
        if "INSERT INTO accounts" in query:
            return 7
        return 1


class WorkerCalibrationProducerTests(unittest.IsolatedAsyncioTestCase):
    async def create(self, database):
        with (
            patch.object(login_broker, "database", database),
            patch(
                "app.services.account_calibration_outbox.database",
                database,
            ),
            patch.object(
                login_broker,
                "ensure_default_fingerprint",
                new=AsyncMock(return_value=3),
            ),
            patch.object(
                login_broker.cookie_vault,
                "encrypt",
                return_value=b"encrypted",
            ),
            patch.object(
                login_broker,
                "serialize_cookies",
                return_value="[]",
            ),
            patch.object(
                login_broker,
                "record_event",
                new=AsyncMock(),
            ),
        ):
            return await login_broker.create_account_from_cookies(
                "bilibili",
                [{"name": "SESSDATA", "value": "redacted"}],
            )

    async def test_account_calibration_and_outbox_share_outer_transaction(self):
        database = RecordingDatabase()
        created = await self.create(database)

        self.assertEqual(7, created["account_id"])
        relevant = [
            (query, depth)
            for query, _values, depth in database.calls
            if "INSERT INTO accounts" in query
            or "INSERT INTO account_calibrations" in query
            or "INSERT INTO outbox_events" in query
        ]
        self.assertEqual(3, len(relevant))
        self.assertTrue(all(depth > 0 for _query, depth in relevant))
        outbox = next(
            values
            for query, values, _depth in database.calls
            if "INSERT INTO outbox_events" in query
        )
        self.assertEqual(
            "account_calibration_requests:bilibili",
            outbox["stream_key"],
        )
        self.assertTrue(
            outbox["dedup_key"].startswith("account-calibration:")
        )

    async def test_outbox_insert_failure_rolls_back_outer_account_insert(self):
        database = RecordingDatabase(fail_outbox=True)
        with self.assertRaisesRegex(RuntimeError, "outbox unavailable"):
            await self.create(database)

        self.assertTrue(database.rolled_back)
        self.assertEqual(0, database.depth)


if __name__ == "__main__":
    unittest.main()
