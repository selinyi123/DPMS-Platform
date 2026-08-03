"""Durability and safety contracts for account calibration delivery."""

from __future__ import annotations

import json
import unittest
from unittest.mock import AsyncMock, patch

from app.account_calibration_streams import (
    account_calibration_stream_binding_for_platform,
    validate_account_calibration_stream_message,
)
from app.services.account_calibration_outbox import (
    ACCOUNT_CALIBRATION_OUTBOX_DEDUP_PREFIX,
    build_account_calibration_message,
    settle_terminal_account_calibration_delivery_failure,
)


CALIBRATION_ID = "00000000-0000-0000-0000-000000000001"


def message(*, platform: str = "bilibili", fallback: str = "login_required"):
    return build_account_calibration_message(
        calibration_id=CALIBRATION_ID,
        account_id=7,
        platform=platform,
        check_url="https://account.bilibili.com/account/home",
        calibration_kind="browser_session",
        fallback_account_status=fallback,
    )


def outbox_current(payload: dict):
    return {
        "id": 9,
        "stream_key": "account_calibration_requests:bilibili",
        "dedup_key": (
            f"{ACCOUNT_CALIBRATION_OUTBOX_DEDUP_PREFIX}{CALIBRATION_ID}"
        ),
        "payload": json.dumps(payload),
        "status": "sending",
        "attempts": 5,
    }


class AccountCalibrationStreamContractTests(unittest.TestCase):
    def test_wrong_platform_lane_is_rejected(self):
        weibo = account_calibration_stream_binding_for_platform("weibo")
        with self.assertRaisesRegex(
            ValueError,
            "account_calibration_stream_platform_mismatch",
        ):
            validate_account_calibration_stream_message(
                weibo,
                message(platform="bilibili"),
            )

    def test_secret_bearing_envelope_is_rejected(self):
        binding = account_calibration_stream_binding_for_platform("bilibili")
        candidate = {**message(), "access_token": "must-not-enter-redis"}
        with self.assertRaisesRegex(
            ValueError,
            "account_calibration_stream_secret_forbidden",
        ):
            validate_account_calibration_stream_message(binding, candidate)


class TerminalCalibrationDeliverySettlementTests(
    unittest.IsolatedAsyncioTestCase
):
    async def test_exhausted_forced_banned_calibration_restores_banned(self):
        payload = message(fallback="banned")
        rows = [
            {
                "id": 12,
                "calibration_id": CALIBRATION_ID,
                "account_id": 7,
                "platform": "bilibili",
                "check_url": payload["check_url"],
                "status": "queued",
            },
            {"id": 7, "status": "warming", "deleted_at": None},
            {"calibration_id": CALIBRATION_ID},
        ]
        database = AsyncMock()
        database.fetch_one = AsyncMock(side_effect=rows)
        database.execute = AsyncMock()
        with patch(
            "app.services.account_calibration_outbox.database",
            database,
        ), patch(
            "app.services.account_calibration_outbox.execute_affected_rows",
            new=AsyncMock(return_value=1),
        ):
            settled = (
                await settle_terminal_account_calibration_delivery_failure(
                    outbox_current(payload),
                    5,
                    "redis unavailable",
                )
            )

        self.assertTrue(settled)
        account_update = database.execute.await_args
        self.assertIn("UPDATE accounts", account_update.args[0])
        self.assertEqual(
            account_update.args[1]["fallback_status"],
            "banned",
        )

    async def test_newer_calibration_prevents_account_status_rollback(self):
        payload = message(fallback="banned")
        database = AsyncMock()
        database.fetch_one = AsyncMock(
            side_effect=[
                {
                    "id": 12,
                    "calibration_id": CALIBRATION_ID,
                    "account_id": 7,
                    "platform": "bilibili",
                    "check_url": payload["check_url"],
                    "status": "queued",
                },
                {"id": 7, "status": "warming", "deleted_at": None},
                {
                    "calibration_id": (
                        "00000000-0000-0000-0000-000000000002"
                    )
                },
            ]
        )
        database.execute = AsyncMock()
        with patch(
            "app.services.account_calibration_outbox.database",
            database,
        ), patch(
            "app.services.account_calibration_outbox.execute_affected_rows",
            new=AsyncMock(return_value=1),
        ):
            settled = (
                await settle_terminal_account_calibration_delivery_failure(
                    outbox_current(payload),
                    5,
                    "redis unavailable",
                )
            )

        self.assertTrue(settled)
        database.execute.assert_not_awaited()

    async def test_ambiguous_delivery_does_not_fail_running_calibration(self):
        payload = message()
        database = AsyncMock()
        database.fetch_one = AsyncMock(
            return_value={
                "id": 12,
                "calibration_id": CALIBRATION_ID,
                "account_id": 7,
                "platform": "bilibili",
                "check_url": payload["check_url"],
                "status": "running",
            }
        )
        with patch(
            "app.services.account_calibration_outbox.database",
            database,
        ), patch(
            "app.services.account_calibration_outbox.execute_affected_rows",
            new=AsyncMock(),
        ) as affected:
            settled = (
                await settle_terminal_account_calibration_delivery_failure(
                    outbox_current(payload),
                    5,
                    "ambiguous timeout",
                )
            )

        self.assertFalse(settled)
        affected.assert_not_awaited()
        database.execute.assert_not_awaited()

    async def test_corrupt_payload_fails_exact_dedup_row_and_quarantines(self):
        current = outbox_current(message())
        current["payload"] = "{not-json"
        database = AsyncMock()
        database.fetch_one = AsyncMock(
            side_effect=[
                {
                    "id": 12,
                    "calibration_id": CALIBRATION_ID,
                    "account_id": 7,
                    "platform": "bilibili",
                    "check_url": "https://account.bilibili.com/account/home",
                    "status": "queued",
                },
                {"id": 7, "status": "warming", "deleted_at": None},
                {"calibration_id": CALIBRATION_ID},
            ]
        )
        database.execute = AsyncMock()
        with patch(
            "app.services.account_calibration_outbox.database",
            database,
        ), patch(
            "app.services.account_calibration_outbox.execute_affected_rows",
            new=AsyncMock(return_value=1),
        ):
            settled = (
                await settle_terminal_account_calibration_delivery_failure(
                    current,
                    5,
                    "invalid relay payload",
                )
            )

        self.assertTrue(settled)
        self.assertEqual(
            database.execute.await_args.args[1]["fallback_status"],
            "frozen",
        )


if __name__ == "__main__":
    unittest.main()
