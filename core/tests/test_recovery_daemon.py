import base64
import json
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
    TaskRecoveryBlocked,
    _mark_recovery_blocked,
    _mark_recovery_exhausted,
    _prepare_task_for_recovery,
    _rebuild_task_payload,
    pending_idle_ms,
)


CONFIG_HASH_REVISION_7 = "7ea85ddf973664b5825f8a065c5866706176969209e4800060f2f390d5e67fc0"


def _task_row(*, mode="dry_run", canonical_url="https://example.test/lottery", action_plan="{}"):
    return {
        "id": 7,
        "account_id": 11,
        "lottery_id": 7,
        "task_mode": mode,
        "dry_run": 0 if mode == "real_run" else 1,
        "platform": "bilibili",
        "raw_url": "https://example.test/lottery",
        "canonical_url": canonical_url,
        "action_plan": action_plan,
        "task_rule_snapshot_id": 91,
        "task_rule_hash": "r" * 64,
        "task_action_plan_hash": "p" * 64,
        "execution_evidence_id": "evidence-1" if mode == "real_run" else None,
        "execution_path_id": "bilibili_api_v2",
        "target_hash": "t" * 64,
        "config_hash": CONFIG_HASH_REVISION_7,
        "account_lease_id": "lease-1",
        "account_lease_generation": 3,
        "current_execution_revision": 7,
        "reconciliation_required": 0,
        "authoritative_rule_snapshot_id": 91,
        "rule_hash": "r" * 64,
        "action_plan_hash": "p" * 64,
    }


def _task_payload(task_id, *, mode="dry_run", canonical_url="https://example.test/lottery", action_plan="{}"):
    return {
        "task_id": task_id,
        "account_id": "11",
        "lottery_id": "7",
        "platform": "bilibili",
        "raw_url": "https://example.test/lottery",
        "canonical_url": canonical_url,
        "dry_run": "0" if mode == "real_run" else "1",
        "mode": mode,
        "selector_config": "{}",
        "action_plan": action_plan,
        "rule_snapshot_id": "91",
        "rule_hash": "r" * 64,
        "action_plan_hash": "p" * 64,
        "execution_evidence_id": "evidence-1" if mode == "real_run" else "",
        "execution_path_id": "bilibili_api_v2",
        "target_hash": "t" * 64,
        "config_hash": CONFIG_HASH_REVISION_7,
        "execution_revision": "7",
        "account_lease_id": "lease-1",
        "account_lease_generation": "3",
    }


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
    async def test_bilibili_api_recovery_rejects_account_revision_drift(self):
        row = _task_row()
        row["current_execution_revision"] = 8
        with patch(
            "app.services.recovery_daemon.database.fetch_one",
            new=AsyncMock(return_value=row),
        ):
            with self.assertRaisesRegex(
                TaskRecoveryBlocked, "account_execution_revision_changed"
            ):
                await _rebuild_task_payload("task-dry")

    async def test_real_run_recovery_rechecks_gate_and_fails_closed(self):
        row = _task_row(mode="real_run")
        with patch("app.services.recovery_daemon.database.fetch_one", new=AsyncMock(return_value=row)), \
             patch("app.services.recovery_daemon.evaluate_real_run_decision", new=AsyncMock(return_value={
                 "allowed": False,
                 "failed_gates": ["global_real_run_enabled"],
                 "blockers": [],
             })):
            with self.assertRaises(RealRunRecoveryBlocked):
                await _rebuild_task_payload("task-real")

    async def test_real_run_recovery_rejects_inconsistent_allow_with_raw_blockers(self):
        row = _task_row(mode="real_run")
        fetch = AsyncMock(return_value=row)
        inconsistent_decision = {
            "allowed": True,
            "failed_gates": [],
            "blockers": ["lottery_action_plan_stale"],
        }
        with patch("app.services.recovery_daemon.database.fetch_one", new=fetch), patch(
            "app.services.recovery_daemon.evaluate_real_run_decision",
            new=AsyncMock(return_value=inconsistent_decision),
        ):
            with self.assertRaisesRegex(
                RealRunRecoveryBlocked,
                "lottery_action_plan_stale",
            ):
                await _rebuild_task_payload("task-real")

        # The immutable outbox row must not be read once the current gate has
        # exposed any blocker, so no payload can reach the re-enqueue path.
        self.assertEqual(fetch.await_count, 1)

    async def test_dry_run_recovery_does_not_call_real_run_gate(self):
        row = _task_row()
        expected = _task_payload("task-dry")
        outbox = {"stream_key": "lottery_tasks", "payload": json.dumps(expected)}
        fetch = AsyncMock(side_effect=[row, outbox])
        with patch("app.services.recovery_daemon.database.fetch_one", new=fetch), \
             patch("app.services.recovery_daemon.evaluate_real_run_decision", new=AsyncMock()) as gate:
            payload = await _rebuild_task_payload("task-dry")

        self.assertEqual(payload, expected)
        gate.assert_not_called()

    async def test_repair_recovery_preserves_immutable_missing_action_subset(self):
        row = _task_row(
            mode="real_run",
            canonical_url="canonical://bilibili/dynamic/7",
            action_plan=json.dumps(
                {"required_actions": ["followed", "liked", "commented", "reposted"]}
            ),
        )
        repair_plan = {
            "version": 1,
            "source": "missing_action_repair",
            "required_actions": ["commented"],
            "full_required_actions": ["followed", "liked", "commented", "reposted"],
            "completed_actions": ["followed", "liked", "reposted"],
            "review_required": False,
        }
        original = _task_payload(
            "task-repair",
            mode="real_run",
            canonical_url="canonical://bilibili/dynamic/7",
            action_plan=json.dumps(repair_plan),
        )
        fetch = AsyncMock(
            side_effect=[row, {"stream_key": "lottery_tasks", "payload": json.dumps(original)}]
        )
        gate = AsyncMock(return_value={"allowed": True, "failed_gates": [], "blockers": []})

        with patch("app.services.recovery_daemon.database.fetch_one", new=fetch), \
             patch("app.services.recovery_daemon.evaluate_real_run_decision", new=gate):
            recovered = await _rebuild_task_payload("task-repair")

        self.assertEqual(json.loads(recovered["action_plan"]), repair_plan)
        self.assertEqual(json.loads(recovered["action_plan"])["required_actions"], ["commented"])

    async def test_missing_immutable_outbox_payload_fails_closed(self):
        row = _task_row()
        fetch = AsyncMock(side_effect=[row, None])
        with patch("app.services.recovery_daemon.database.fetch_one", new=fetch):
            with self.assertRaisesRegex(TaskRecoveryBlocked, "immutable_task_payload_missing"):
                await _rebuild_task_payload("task-dry")


class FakeRecoveryDatabase:
    def __init__(self, *, lease_active, task_mode="dry_run", status="running", breaker_status="open"):
        self.task = {
            "task_id": "task-1",
            "account_id": 11,
            "lottery_id": 7,
            "account_lease_id": "lease-task-1",
            "account_lease_generation": 5,
            "status": status,
            "worker_id": "worker-old",
            "task_mode": task_mode,
            "reconciliation_required": 0,
            "lease_active": 1 if lease_active else 0,
        }
        self.executions = []
        self.breaker_status = breaker_status

    def transaction(self):
        class Transaction:
            async def __aenter__(self_inner):
                return self

            async def __aexit__(self_inner, *exc):
                return False

        return Transaction()

    async def fetch_one(self, query, values=None):
        if "FROM task_runs" in query:
            return dict(self.task)
        if "FROM lotteries" in query:
            return {"id": 7, "platform": "bilibili"}
        if "FROM accounts" in query:
            return {"id": 11}
        if "FROM circuit_breakers" in query:
            return {"status": self.breaker_status}
        raise AssertionError(f"unexpected query: {query}")

    async def execute(self, query, values=None):
        self.executions.append((query, dict(values or {})))
        if "UPDATE task_runs" in query:
            next_status = "failed" if "SET status = 'failed'" in query else "queued"
            self.task.update({"status": next_status, "worker_id": None, "lease_active": 0})
            if "reconciliation_required = 1" in query:
                self.task["reconciliation_required"] = 1
        return 1


class PrepareTaskForRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_expired_running_owner_is_revoked_before_reenqueue(self):
        fake = FakeRecoveryDatabase(lease_active=False, task_mode="shadow_run")
        with patch("app.services.recovery_daemon.database", fake):
            result = await _prepare_task_for_recovery("task-1")

        self.assertEqual(result, "recover")
        self.assertEqual(fake.task["status"], "queued")
        self.assertIsNone(fake.task["worker_id"])
        self.assertTrue(any("SET status = 'claimed'" in query for query, _ in fake.executions))
        self.assertFalse(any("SET status = 'ready'" in query for query, _ in fake.executions))

    async def test_expired_real_run_is_quarantined_not_reenqueued(self):
        fake = FakeRecoveryDatabase(lease_active=False, task_mode="real_run")
        with patch("app.services.recovery_daemon.database", fake):
            result = await _prepare_task_for_recovery("task-1")

        self.assertEqual(result, "real_run_reconciliation_required")
        self.assertEqual(fake.task["status"], "failed")
        self.assertEqual(fake.task["reconciliation_required"], 1)
        self.assertTrue(any("external outcome requires reconciliation" in query for query, _ in fake.executions))
        terminal_query = next(
            query for query, _ in fake.executions if "external outcome requires reconciliation" in query
        )
        self.assertNotIn("worker_id = NULL", terminal_query)
        self.assertNotIn("stream_message_id = NULL", terminal_query)
        self.assertTrue(any("SET status = 'cooling'" in query for query, _ in fake.executions))
        self.assertFalse(any("execution_lock = NULL" in query for query, _ in fake.executions))
        unknown_intent_query = next(
            query for query, _ in fake.executions
            if "UPDATE external_action_intents" in query
        )
        self.assertIn("effect_certainty = 'unknown'", unknown_intent_query)
        self.assertIn("completed_at = COALESCE(completed_at, NOW())", unknown_intent_query)
        breaker_writes = [(query, values) for query, values in fake.executions if "circuit_breakers" in query]
        self.assertEqual(len(breaker_writes), 1)
        self.assertEqual(breaker_writes[0][1]["scope"], "platform:bilibili")

    async def test_expired_real_run_is_not_settled_without_confirmed_breaker(self):
        fake = FakeRecoveryDatabase(
            lease_active=False,
            task_mode="real_run",
            breaker_status="closed",
        )
        with patch("app.services.recovery_daemon.database", fake):
            with self.assertRaisesRegex(RuntimeError, "breaker_not_persisted"):
                await _prepare_task_for_recovery("task-1")

    async def test_active_lease_is_not_revoked_after_xclaim_race(self):
        fake = FakeRecoveryDatabase(lease_active=True)
        with patch("app.services.recovery_daemon.database", fake):
            result = await _prepare_task_for_recovery("task-1")

        self.assertEqual(result, "skip_owned_running_task")
        self.assertEqual(fake.task["status"], "running")
        self.assertEqual(fake.executions, [])


class RecoveryTerminalSettlementTests(unittest.IsolatedAsyncioTestCase):
    async def test_gate_block_cleanup_does_not_touch_reclaimed_running_task(self):
        fake = FakeRecoveryDatabase(lease_active=True, task_mode="shadow_run", status="running")
        with patch("app.services.recovery_daemon.database", fake):
            settled = await _mark_recovery_blocked("task-1", "current gate failed")

        self.assertFalse(settled)
        self.assertEqual(fake.task["status"], "running")
        self.assertEqual(fake.executions, [])

    async def test_gate_block_cleanup_settles_still_queued_task(self):
        fake = FakeRecoveryDatabase(lease_active=False, task_mode="shadow_run", status="queued")
        with patch("app.services.recovery_daemon.database", fake):
            settled = await _mark_recovery_blocked("task-1", "current gate failed")

        self.assertTrue(settled)
        self.assertEqual(fake.task["status"], "failed")
        self.assertTrue(any("UPDATE lotteries" in query for query, _ in fake.executions))
        lease_query, lease_values = next(
            (query, values)
            for query, values in fake.executions
            if "UPDATE account_operation_leases" in query
        )
        self.assertIn("generation = :lease_generation", lease_query)
        self.assertIn("owner_id = :task_id", lease_query)
        self.assertEqual(lease_values["lease_id"], "lease-task-1")
        self.assertEqual(lease_values["lease_generation"], 5)
        self.assertEqual(lease_values["task_id"], "task-1")

    async def test_exhausted_cleanup_skips_task_that_is_no_longer_queued(self):
        fake = FakeRecoveryDatabase(lease_active=True, task_mode="dry_run", status="running")
        with patch("app.services.recovery_daemon.database", fake):
            settled = await _mark_recovery_exhausted("task-1")

        self.assertFalse(settled)
        self.assertEqual(fake.executions, [])


if __name__ == "__main__":
    unittest.main()
