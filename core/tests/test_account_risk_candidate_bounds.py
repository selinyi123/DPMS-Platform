import asyncio
import base64
import json
import os
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

os.environ.setdefault(
    "ENCRYPTION_KEY",
    base64.b64encode(os.urandom(32)).decode(),
)
os.environ.setdefault("UPDATE_SECRET", "test-secret")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")

from app.services import real_run_readiness  # noqa: E402


class DenseRiskDatabase:
    def __init__(self, *, now, account_ids, history):
        self.now = now
        self.account_ids = tuple(account_ids)
        self.history = list(history)
        self.risk_query = ""
        self.risk_values = {}
        self.risk_rows_returned = 0

    async def fetch_one(self, query, values=None):
        if "SELECT UTC_TIMESTAMP() AS db_now" in query:
            return {"db_now": self.now}
        raise AssertionError(f"Unexpected query: {query}")

    async def fetch_all(self, query, values=None):
        if "FROM accounts a" in query:
            return [
                {
                    "id": account_id,
                    "platform": "douyin",
                    "status": "ready",
                    "execution_revision": 1,
                    "credential_size": 64,
                }
                for account_id in self.account_ids
            ]
        if "FROM account_active_risk_states active_risk" in query:
            self.risk_query = query
            self.risk_values = dict(values or {})
            # Return the dense source history if the query stops using the
            # one-row-per-account materialized state contract.
            bounded_contract = all(
                token in query
                for token in (
                    "JOIN risk_events re",
                    "active_risk.risk_event_id",
                    "active_risk.active_until > :readiness_risk_now",
                )
            )
            if not bounded_contract:
                return list(self.history)

            active_by_account = {}
            for row in self.history:
                account_id = int(row["account_id"])
                if account_id not in self.account_ids:
                    continue
                if not real_run_readiness.account_risk_is_active(
                    row,
                    self.now,
                ):
                    continue
                current = active_by_account.get(account_id)
                if current is None or (
                    real_run_readiness.account_risk_cooldown_until(row),
                    row["created_at"],
                    row["id"],
                ) > (
                    real_run_readiness.account_risk_cooldown_until(current),
                    current["created_at"],
                    current["id"],
                ):
                    active_by_account[account_id] = row
            result = [
                active_by_account[account_id]
                for account_id in self.account_ids
                if account_id in active_by_account
            ]
            self.risk_rows_returned = len(result)
            return result
        raise AssertionError(f"Unexpected query: {query}")


def lottery_row():
    return {
        "id": 1,
        "platform": "douyin",
        "authoritative_rule_snapshot_id": None,
    }


class AccountRiskCandidateBoundTests(unittest.IsolatedAsyncioTestCase):
    async def test_dense_expired_short_history_cannot_hide_active_24h_risk(
        self,
    ):
        now = datetime(2026, 7, 24, 12, 0, 0)
        history = [
            {
                "id": 10_000 + index,
                "account_id": 7,
                "event_type": "cooling",
                "detail": json.dumps({"reason": "action_window"}),
                "created_at": now - timedelta(hours=5, seconds=index),
            }
            for index in range(10_000)
        ]
        history.append(
            {
                "id": 1,
                "account_id": 7,
                "event_type": "login_required",
                "detail": json.dumps({"reason": "redirected_to_login"}),
                "created_at": now - timedelta(hours=20),
            }
        )
        fake = DenseRiskDatabase(
            now=now,
            account_ids=[7],
            history=history,
        )

        with patch.object(real_run_readiness, "database", fake):
            batch = await real_run_readiness.load_account_scoped_real_run_readiness_batch(
                [lottery_row()],
                account_ids=[7],
            )

        self.assertEqual(fake.risk_rows_returned, 1)
        self.assertEqual(
            batch.account_risks[7]["latest_event"]["id"],
            1,
        )
        self.assertIn(
            "FROM account_active_risk_states active_risk",
            fake.risk_query,
        )
        self.assertNotIn("ROW_NUMBER() OVER", fake.risk_query)
        self.assertNotIn("JSON_EXTRACT", fake.risk_query)

    async def test_maximum_account_batch_returns_at_most_one_row_per_account(
        self,
    ):
        now = datetime(2026, 7, 24, 12, 0, 0)
        account_ids = list(
            range(
                1,
                (
                    real_run_readiness
                    .ACCOUNT_SCOPED_READINESS_ACCOUNT_BATCH_SIZE
                    + 1
                ),
            )
        )
        history = []
        for account_id in account_ids:
            history.extend(
                {
                    "id": (account_id * 10_000) + index,
                    "account_id": account_id,
                    "event_type": "cooling",
                    "detail": json.dumps(
                        {
                            "reason": (
                                "action_window"
                                if index
                                else "page_risk_signal"
                            )
                        }
                    ),
                    "created_at": now - timedelta(minutes=index + 1),
                }
                for index in range(500)
            )
        fake = DenseRiskDatabase(
            now=now,
            account_ids=account_ids,
            history=history,
        )

        with patch.object(real_run_readiness, "database", fake):
            batch = await real_run_readiness.load_account_scoped_real_run_readiness_batch(
                [lottery_row()],
                account_ids=account_ids,
            )

        self.assertEqual(fake.risk_rows_returned, len(account_ids))
        self.assertEqual(len(batch.account_risks), len(account_ids))
        self.assertEqual(
            len(
                [
                    key
                    for key in fake.risk_values
                    if key.startswith("readiness_account_")
                ]
            ),
            len(account_ids),
        )

    async def test_sql_uses_materialized_state_without_history_reason_scan(
        self,
    ):
        now = datetime(2026, 7, 24, 12, 0, 0)
        fake = DenseRiskDatabase(now=now, account_ids=[3], history=[])

        with patch.object(real_run_readiness, "database", fake):
            await real_run_readiness.load_account_scoped_real_run_readiness_batch(
                [lottery_row()],
                account_ids=[3],
            )

        self.assertIn("readiness_risk_now", fake.risk_values)
        self.assertFalse(
            any(
                key.startswith("readiness_risk_reason_")
                for key in fake.risk_values
            )
        )
        self.assertNotIn("TIMESTAMPADD(", fake.risk_query)

    async def test_excess_or_duplicate_risk_rows_fail_closed(self):
        now = datetime(2026, 7, 24, 12, 0, 0)
        duplicate = {
            "id": 1,
            "account_id": 9,
            "event_type": "cooling",
            "detail": json.dumps({"reason": "page_risk_signal"}),
            "created_at": now - timedelta(hours=1),
        }
        fake = DenseRiskDatabase(
            now=now,
            account_ids=[9],
            history=[duplicate],
        )

        async def duplicate_rows(query, values=None):
            if "FROM account_active_risk_states active_risk" in query:
                return [duplicate, {**duplicate, "id": 2}]
            return await DenseRiskDatabase.fetch_all(fake, query, values)

        fake.fetch_all = duplicate_rows
        with patch.object(real_run_readiness, "database", fake):
            with self.assertRaisesRegex(
                RuntimeError,
                "exceeded one row per readiness account",
            ):
                await real_run_readiness.load_account_scoped_real_run_readiness_batch(
                    [lottery_row()],
                    account_ids=[9],
                )

    async def test_risk_query_timeout_is_explicit_and_fail_closed(self):
        now = datetime(2026, 7, 24, 12, 0, 0)
        fake = DenseRiskDatabase(now=now, account_ids=[5], history=[])
        original_fetch_all = fake.fetch_all

        async def slow_risk_query(query, values=None):
            if "FROM account_active_risk_states active_risk" in query:
                await asyncio.sleep(0.05)
            return await original_fetch_all(query, values)

        fake.fetch_all = slow_risk_query
        with patch.object(
            real_run_readiness,
            "database",
            fake,
        ), patch.object(
            real_run_readiness,
            "ACCOUNT_SCOPED_READINESS_RISK_DB_TIMEOUT_SECONDS",
            0.001,
        ):
            with self.assertRaises(
                real_run_readiness.AccountScopedReadinessRiskQueryTimeout
            ):
                await real_run_readiness.load_account_scoped_real_run_readiness_batch(
                    [lottery_row()],
                    account_ids=[5],
                )

    async def test_strategy_maps_risk_timeout_to_platform_local_blocker(self):
        prefilter = (
            real_run_readiness.AccountScopedReadinessCandidatePrefilter(
                account_ids_by_lottery={1: frozenset({5})},
            )
        )
        with patch.object(
            real_run_readiness,
            "load_account_scoped_real_run_readiness_batch",
            new=AsyncMock(
                side_effect=(
                    real_run_readiness
                    .AccountScopedReadinessRiskQueryTimeout()
                )
            ),
        ):
            result = (
                await real_run_readiness
                .evaluate_account_scoped_real_run_readiness_batch(
                    [lottery_row()],
                    account_candidates={
                        "douyin": [
                            {
                                "account_id": 5,
                                "platform": "douyin",
                            }
                        ]
                    },
                    candidate_prefilter=prefilter,
                )
            )

        self.assertEqual(
            result[1]["readiness"]["blockers"],
            [
                real_run_readiness
                .ACCOUNT_SCOPED_READINESS_RISK_QUERY_TIMEOUT_BLOCKER
            ],
        )
        self.assertFalse(result[1]["readiness"]["allowed"])


if __name__ == "__main__":
    unittest.main()
