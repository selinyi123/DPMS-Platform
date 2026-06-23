"""Offline tests for the autonomous dispatch eligibility logic (Phase 2b).

Pins the pure gates (daily cap / spacing / retry cap) and the flag short-circuit,
which is where the real decision complexity lives. The DB wiring is verified in
the Docker integration environment.

Run:  PYTHONPATH=core python core/tests/test_auto_dispatch.py
"""

import base64
import os
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # core/ -> import app.*

os.environ.setdefault("ENCRYPTION_KEY", base64.b64encode(b"0" * 32).decode())
os.environ.setdefault("DATABASE_URL", "mysql+aiomysql://u:p@localhost:3306/lottery")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from app.services.auto_dispatch import (
    AccountStats,
    account_eligible,
    api_mode_enabled,
    auto_dispatch_enabled,
    lottery_eligible,
    run_auto_dispatch_cycle,
)

LIMITS = {"max_daily_per_account": 8, "min_spacing_min": 45}
NOW = datetime(2026, 6, 23, 12, 0, 0)


class AccountEligibleTests(unittest.TestCase):
    def test_daily_cap_blocks(self):
        ok, reason = account_eligible(AccountStats(1, 8, None), LIMITS, NOW)
        self.assertFalse(ok)
        self.assertIn("daily cap", reason)

    def test_spacing_blocks(self):
        stats = AccountStats(1, 2, NOW - timedelta(minutes=30))  # 30m < 45m
        ok, reason = account_eligible(stats, LIMITS, NOW)
        self.assertFalse(ok)
        self.assertIn("spacing", reason)

    def test_spacing_met_passes(self):
        stats = AccountStats(1, 2, NOW - timedelta(minutes=60))  # 60m >= 45m
        self.assertTrue(account_eligible(stats, LIMITS, NOW)[0])

    def test_no_prior_dispatch_passes(self):
        self.assertTrue(account_eligible(AccountStats(1, 0, None), LIMITS, NOW)[0])


class LotteryEligibleTests(unittest.TestCase):
    def test_retry_cap(self):
        self.assertFalse(lottery_eligible(3, 2)[0])
        self.assertTrue(lottery_eligible(2, 2)[0])
        self.assertTrue(lottery_eligible(0, 2)[0])


class GateTests(unittest.TestCase):
    def test_flags_default_off(self):
        os.environ.pop("DPMS_AUTO_DISPATCH_ENABLED", None)
        os.environ.pop("DPMS_BILIBILI_API_MODE", None)
        self.assertFalse(auto_dispatch_enabled())
        self.assertFalse(api_mode_enabled())


class CycleShortCircuitTests(unittest.IsolatedAsyncioTestCase):
    async def test_cycle_skips_when_flags_off(self):
        # No DB touched: the flag gate returns before any query.
        os.environ.pop("DPMS_AUTO_DISPATCH_ENABLED", None)
        os.environ.pop("DPMS_BILIBILI_API_MODE", None)
        result = await run_auto_dispatch_cycle(now=NOW)
        self.assertEqual(result, {"skipped": "flag_off"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
