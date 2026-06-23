"""Offline tests for the Bilibili API execution channel (Phase 2a).

Covers the DB-free decision logic (plan_from_result), the gate flag, and the
target-id resolver. The thin DB wiring in run_bilibili_api_task is exercised by
the worker dry/real smoke suites; here we pin the mapping/abort/fail semantics
that the worker relies on.

Run:  python worker/tests/test_bilibili_api_channel.py
"""

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # worker/ -> import app.*

# crypto.cookie_vault validates ENCRYPTION_KEY at import; the channel imports it.
import base64

os.environ.setdefault("ENCRYPTION_KEY", base64.b64encode(b"0" * 32).decode())
os.environ.setdefault("DATABASE_URL", "mysql+aiomysql://u:p@localhost:3306/lottery")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from app.adapters.bilibili_api_channel import api_mode_enabled, plan_from_result
from app.bilibili.executor import ExecutionResult
from app.bilibili.targets import extract_dynamic_id
from app.bilibili.errors import CodeResult, Outcome, classify


class TargetResolverTests(unittest.TestCase):
    def test_extracts_dynamic_id(self):
        self.assertEqual(extract_dynamic_id("https://t.bilibili.com/123456789012"), "123456789012")
        self.assertEqual(extract_dynamic_id("https://www.bilibili.com/opus/987654321098"), "987654321098")
        self.assertEqual(extract_dynamic_id("123456789012345"), "123456789012345")

    def test_rejects_unresolvable(self):
        self.assertIsNone(extract_dynamic_id("https://b23.tv/abcXYZ"))
        self.assertIsNone(extract_dynamic_id(""))
        self.assertIsNone(extract_dynamic_id(None))


class GateTests(unittest.TestCase):
    def test_flag_gates_api_mode(self):
        os.environ.pop("DPMS_BILIBILI_API_MODE", None)
        self.assertFalse(api_mode_enabled("bilibili"))
        os.environ["DPMS_BILIBILI_API_MODE"] = "true"
        try:
            self.assertTrue(api_mode_enabled("bilibili"))
            self.assertFalse(api_mode_enabled("douyin"))  # only bilibili
        finally:
            os.environ.pop("DPMS_BILIBILI_API_MODE", None)


def _result(**kw):
    r = ExecutionResult(dynamic_id="d", required=kw.pop("required", []))
    for k, v in kw.items():
        setattr(r, k, v)
    return r


class PlanFromResultTests(unittest.TestCase):
    def test_success_saves_all_phases_plus_completed(self):
        r = _result(success=True, required=["follow", "repost"])
        r.actions = {"follow": classify("follow", 0), "repost": classify("repost", 0)}
        plan = plan_from_result(r, ["follow", "repost"])
        self.assertEqual(plan.phases_to_save, ["followed", "reposted", "completed"])
        self.assertIsNone(plan.failure_reason)
        self.assertIsNone(plan.account_status)

    def test_risk_abort_cools_account_and_fails(self):
        r = _result(aborted=True, abort_outcome=Outcome.RISK, abort_reason="follow: 账号异常")
        r.actions = {"follow": classify("follow", 22015)}
        plan = plan_from_result(r, ["follow", "repost"])
        self.assertEqual(plan.account_status, ("cooling", "api_risk_signal"))
        self.assertIsNotNone(plan.failure_reason)
        self.assertNotIn("completed", plan.phases_to_save)

    def test_auth_abort_flags_login_required(self):
        r = _result(aborted=True, abort_outcome=Outcome.AUTH, abort_reason="comment: 未登录")
        plan = plan_from_result(r, ["comment"])
        self.assertEqual(plan.account_status, ("login_required", "api_auth_failed"))
        self.assertIsNotNone(plan.failure_reason)

    def test_partial_success_records_done_phases_but_fails(self):
        # follow ok, repost permanently skipped -> save 'followed', no 'completed', fail.
        r = _result(success=False, stopped_reason="", required=["follow", "repost"])
        r.actions = {"follow": classify("follow", 0), "repost": CodeResult(1101004, Outcome.SKIP, "不能转发")}
        plan = plan_from_result(r, ["follow", "repost"])
        self.assertEqual(plan.phases_to_save, ["followed"])
        self.assertIsNotNone(plan.failure_reason)
        self.assertIsNone(plan.account_status)  # SKIP is not an account-risk


if __name__ == "__main__":
    unittest.main(verbosity=2)
