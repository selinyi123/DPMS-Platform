import base64
import os
import unittest

# Valid env so importing app.db / app.config never fails during collection.
os.environ.setdefault("ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode())
os.environ.setdefault("UPDATE_SECRET", "test-secret")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")

from app.services.recovery_daemon import (  # noqa: E402
    IDLE_THRESHOLD_MS,
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


if __name__ == "__main__":
    unittest.main()
