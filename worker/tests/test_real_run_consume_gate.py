"""Offline tests for the worker-side real-run consume/action gate."""

import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.real_run_gate import RealRunGateBlocked, enforce_real_run_gate  # noqa: E402


def _task():
    return {
        "task_id": "task-1",
        "account_id": "41",
        "lottery_id": "73",
        "platform": "bilibili",
        "mode": "real_run",
    }


def _decision_row():
    return {
        "task_id": "task-1",
        "account_id": 41,
        "lottery_id": 73,
        "task_mode": "real_run",
        "decision_id": "decision-1",
        "task_policy_version": 3,
        "bound_account_id": 41,
        "account_platform": "bilibili",
        "bound_lottery_id": 73,
        "lottery_platform": "bilibili",
        "policy_decision_id": "decision-1",
        "decision_policy_key": "real_run_gate",
        "decision_policy_version": 3,
        "decision_subject_type": "lottery",
        "decision_subject_id": "73",
        "decision_outcome": "allow",
        "policy_active": 1,
    }


class FakeGateDatabase:
    def __init__(self):
        self.setting = {"setting_value": "true"}
        self.decision = _decision_row()
        self.breakers = [{"scope": "global", "status": "closed", "reason": None}]
        self.raise_on = None

    async def fetch_one(self, query, values=None):
        if self.raise_on == "fetch_one":
            raise RuntimeError("database unavailable")
        if "runtime_settings" in query:
            return self.setting
        if "FROM task_runs" in query:
            return self.decision
        raise AssertionError(f"unexpected fetch_one query: {query}")

    async def fetch_all(self, query, values=None):
        if self.raise_on == "fetch_all":
            raise RuntimeError("database unavailable")
        if "circuit_breakers" in query:
            return self.breakers
        raise AssertionError(f"unexpected fetch_all query: {query}")


class RealRunConsumeGateTests(unittest.IsolatedAsyncioTestCase):
    async def assert_blocked(self, db, code, task=None):
        with self.assertRaises(RealRunGateBlocked) as caught:
            await enforce_real_run_gate(task or _task(), db=db)
        self.assertEqual(caught.exception.code, code)

    async def test_all_authorities_allow(self):
        db = FakeGateDatabase()
        snapshot = await enforce_real_run_gate(_task(), db=db)
        self.assertEqual(snapshot.task_id, "task-1")
        self.assertEqual(snapshot.account_id, 41)
        self.assertEqual(snapshot.lottery_id, 73)
        self.assertEqual(snapshot.decision_id, "decision-1")
        self.assertEqual(snapshot.policy_version, 3)

    async def test_runtime_switch_is_fail_closed(self):
        db = FakeGateDatabase()
        db.setting = {"setting_value": "false"}
        await self.assert_blocked(db, "real_run_disabled")

        db.setting = None
        await self.assert_blocked(db, "runtime_setting_missing")

    async def test_global_and_platform_breakers_block(self):
        for scope in ("global", "platform:bilibili"):
            with self.subTest(scope=scope):
                db = FakeGateDatabase()
                db.breakers = [
                    {"scope": "global", "status": "closed", "reason": None},
                    {"scope": scope, "status": "open", "reason": "operator stop"},
                ]
                await self.assert_blocked(db, "circuit_breaker_blocked")

    async def test_missing_global_breaker_blocks(self):
        db = FakeGateDatabase()
        db.breakers = []
        await self.assert_blocked(db, "global_breaker_missing")

    async def test_policy_decision_must_exist_and_allow(self):
        db = FakeGateDatabase()
        db.decision["policy_decision_id"] = None
        await self.assert_blocked(db, "policy_decision_missing")

        db = FakeGateDatabase()
        db.decision["decision_outcome"] = "deny"
        await self.assert_blocked(db, "policy_decision_denied")

        db = FakeGateDatabase()
        db.decision["policy_active"] = 0
        await self.assert_blocked(db, "policy_inactive")

    async def test_task_account_lottery_and_platform_must_match(self):
        mismatches = (
            ("account_id", 99),
            ("bound_lottery_id", 99),
            ("lottery_platform", "weibo"),
        )
        for field, value in mismatches:
            with self.subTest(field=field):
                db = FakeGateDatabase()
                db.decision[field] = value
                await self.assert_blocked(db, "task_binding_mismatch")

    async def test_policy_subject_and_version_must_match(self):
        db = FakeGateDatabase()
        db.decision["decision_subject_id"] = "999"
        await self.assert_blocked(db, "policy_subject_mismatch")

        db = FakeGateDatabase()
        db.decision["decision_policy_version"] = 4
        await self.assert_blocked(db, "policy_version_mismatch")

    async def test_database_errors_block_without_leaking_error_text(self):
        for operation in ("fetch_one", "fetch_all"):
            with self.subTest(operation=operation):
                db = FakeGateDatabase()
                db.raise_on = operation
                await self.assert_blocked(db, "gate_database_error")


if __name__ == "__main__":
    unittest.main(verbosity=2)
