import base64
import os
import unittest
from unittest.mock import AsyncMock, patch

# Valid env so importing app.db / app.config never fails during collection.
os.environ.setdefault("ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode())
os.environ.setdefault("UPDATE_SECRET", "test-secret")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")

from app.services.recovery_daemon import (  # noqa: E402
    IDLE_THRESHOLD_MS,
    RealRunRecoveryBlocked,
    _rebuild_task_payload,
    pending_idle_ms,
)


def _pending_entry(time_since_delivered):
    """A redis-py xpending_range entry as the parser actually shapes it."""
    return {
        "message_id": "1700000000000-0",
        "consumer": "worker-1",
        "time_since_delivered": time_since_delivered,
        "times_delivered": 1,
    }


class PendingIdleMsTests(unittest.TestCase):
    def test_reads_time_since_delivered(self):
        """redis-py exposes idle time as ``time_since_delivered`` (ms), not ``idle``.

        Reading a non-existent ``idle`` key raised KeyError every cycle, which the
        daemon's broad except swallowed — so it never recovered a stuck task.
        """
        self.assertEqual(pending_idle_ms(_pending_entry(130_000)), 130_000)

    def test_no_idle_key_required(self):
        # The pre-fix code did `now_ms - msg["idle"]`; ensure we never touch it.
        entry = _pending_entry(5_000)
        self.assertNotIn("idle", entry)
        self.assertEqual(pending_idle_ms(entry), 5_000)

    def test_threshold_comparison(self):
        stale = pending_idle_ms(_pending_entry(IDLE_THRESHOLD_MS + 1))
        fresh = pending_idle_ms(_pending_entry(IDLE_THRESHOLD_MS - 1))
        self.assertGreaterEqual(stale, IDLE_THRESHOLD_MS)
        self.assertLess(fresh, IDLE_THRESHOLD_MS)

    def test_missing_value_is_zero_not_crash(self):
        self.assertEqual(pending_idle_ms({}), 0)
        self.assertEqual(pending_idle_ms({"time_since_delivered": None}), 0)


class RebuildTaskPayloadRealRunGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_run_recovery_rechecks_gate_and_fails_closed(self):
        row = {
            "id": 7,
            "account_id": 11,
            "lottery_id": 7,
            "task_mode": "real_run",
            "dry_run": 0,
            "platform": "bilibili",
            "raw_url": "https://example.test/lottery",
            "canonical_url": "https://example.test/lottery",
            "action_plan": "{}",
        }
        with patch("app.services.recovery_daemon.database.fetch_one", new=AsyncMock(return_value=row)), \
             patch("app.services.recovery_daemon.evaluate_real_run_decision", new=AsyncMock(return_value={
                 "allowed": False,
                 "failed_gates": ["global_real_run_enabled"],
                 "blockers": [],
             })), \
             patch("app.services.recovery_daemon.build_lottery_task_message") as build_message:
            with self.assertRaises(RealRunRecoveryBlocked):
                await _rebuild_task_payload("task-real")

        build_message.assert_not_called()

    async def test_dry_run_recovery_does_not_call_real_run_gate(self):
        row = {
            "id": 7,
            "account_id": 11,
            "lottery_id": 7,
            "task_mode": "dry_run",
            "dry_run": 1,
            "platform": "bilibili",
            "raw_url": "https://example.test/lottery",
            "canonical_url": "https://example.test/lottery",
            "action_plan": "{}",
        }
        expected = {"task_id": "task-dry", "mode": "dry_run"}
        with patch("app.services.recovery_daemon.database.fetch_one", new=AsyncMock(return_value=row)), \
             patch("app.services.recovery_daemon.load_runtime_selector_config", new=AsyncMock(return_value={})), \
             patch("app.services.recovery_daemon.evaluate_real_run_decision", new=AsyncMock()) as gate, \
             patch("app.services.recovery_daemon.build_lottery_task_message", return_value=expected):
            payload = await _rebuild_task_payload("task-dry")

        self.assertEqual(payload, expected)
        gate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
