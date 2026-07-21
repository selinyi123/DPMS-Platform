import base64
import json
import os
import unittest

# Valid env so importing app.db / app.config never fails during collection.
os.environ.setdefault("ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode())
os.environ.setdefault("UPDATE_SECRET", "test-secret")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")

from app.services.outbox import (  # noqa: E402
    LOTTERY_TASK_FIELDS,
    OUTBOX_MAX_ATTEMPTS,
    _deliver_claimed,
    _settle_terminal_delivery_failure,
    build_lottery_task_message,
    reconcile_orphaned_locks,
    should_retry,
    terminal_status,
)


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class _TerminalFailureDatabase:
    def __init__(
        self,
        task_status="queued",
        stream_key="lottery_tasks",
        *,
        outbox_status="sending",
        outbox_attempts=OUTBOX_MAX_ATTEMPTS,
        probe_status="queued",
    ):
        self.task_status = task_status
        self.stream_key = stream_key
        self.outbox_status = outbox_status
        self.outbox_attempts = outbox_attempts
        self.probe_status = probe_status
        self.executions = []

    def transaction(self):
        return _Transaction()

    async def fetch_one(self, query, values=None):
        if "FROM outbox_events" in query:
            if self.stream_key == "adapter_probe_requests":
                return {
                    "id": 1,
                    "stream_key": self.stream_key,
                    "dedup_key": "adapter-probe:probe-1",
                    "payload": json.dumps({"probe_id": "probe-1"}),
                    "status": self.outbox_status,
                    "attempts": self.outbox_attempts,
                }
            return {
                "id": 1,
                "stream_key": "lottery_tasks",
                "dedup_key": "task-1",
                "payload": json.dumps({"task_id": "task-1"}),
                "status": self.outbox_status,
                "attempts": self.outbox_attempts,
            }
        if "FROM task_runs" in query:
            return {
                "task_id": "task-1",
                "account_id": 9,
                "lottery_id": 42,
                "status": self.task_status,
                "task_mode": "dry_run",
                "account_lease_id": "lease-task",
                "account_lease_generation": 4,
            }
        if "FROM adapter_calibrations" in query:
            return {
                "account_id": 9,
                "status": self.probe_status,
                "account_lease_id": "lease-probe",
                "account_lease_generation": 3,
            }
        if "FROM accounts" in query:
            return {"id": 9}
        if "FROM lotteries" in query:
            return {"id": 42, "status": "claimed", "execution_lock": "task-1"}
        raise AssertionError(query)

    async def execute(self, query, values=None):
        self.executions.append((str(query), dict(values or {})))
        return 1


class BuildLotteryTaskMessageTests(unittest.TestCase):
    def _msg(self, **overrides):
        params = dict(
            task_id="t-1",
            account_id=7,
            lottery_id=42,
            platform="bilibili",
            raw_url="https://www.bilibili.com/x",
            canonical_url="https://www.bilibili.com/x?clean",
            task_mode="dry_run",
            dry_run=True,
            platform_selectors={"follow": ".btn"},
            action_plan={"required_actions": ["followed"]},
        )
        params.update(overrides)
        return build_lottery_task_message(**params)

    def test_field_set_is_exactly_the_contract(self):
        msg = self._msg()
        self.assertEqual(set(msg.keys()), set(LOTTERY_TASK_FIELDS))

    def test_all_values_are_strings(self):
        msg = self._msg()
        for key, value in msg.items():
            self.assertIsInstance(value, str, f"{key} must be a str for Redis xadd")

    def test_ids_are_stringified(self):
        msg = self._msg(account_id=7, lottery_id=42, execution_revision=9)
        self.assertEqual(msg["account_id"], "7")
        self.assertEqual(msg["lottery_id"], "42")
        self.assertEqual(msg["execution_revision"], "9")

    def test_dry_run_flag_matches_mode(self):
        self.assertEqual(self._msg(task_mode="dry_run", dry_run=True)["dry_run"], "1")
        self.assertEqual(self._msg(task_mode="shadow_run", dry_run=True)["dry_run"], "1")
        self.assertEqual(self._msg(task_mode="real_run", dry_run=False)["dry_run"], "0")

    def test_mode_is_preserved(self):
        self.assertEqual(self._msg(task_mode="real_run", dry_run=False)["mode"], "real_run")

    def test_selectors_and_plan_are_json_encoded(self):
        msg = self._msg(platform_selectors={"a": 1}, action_plan={"b": 2})
        self.assertEqual(json.loads(msg["selector_config"]), {"a": 1})
        self.assertEqual(json.loads(msg["action_plan"]), {"b": 2})

    def test_non_dict_selectors_and_plan_default_to_empty(self):
        msg = self._msg(platform_selectors=None, action_plan="not-a-dict")
        self.assertEqual(msg["selector_config"], "{}")
        self.assertEqual(msg["action_plan"], "{}")

    def test_none_urls_become_empty_strings(self):
        msg = self._msg(raw_url=None, canonical_url=None)
        self.assertEqual(msg["raw_url"], "")
        self.assertEqual(msg["canonical_url"], "")


class RetryPredicateTests(unittest.TestCase):
    def test_should_retry_below_cap(self):
        self.assertTrue(should_retry(0))
        self.assertTrue(should_retry(OUTBOX_MAX_ATTEMPTS - 1))

    def test_should_not_retry_at_or_above_cap(self):
        self.assertFalse(should_retry(OUTBOX_MAX_ATTEMPTS))
        self.assertFalse(should_retry(OUTBOX_MAX_ATTEMPTS + 3))

    def test_terminal_status_keeps_pending_until_cap(self):
        self.assertEqual(terminal_status(1), "pending")
        self.assertEqual(terminal_status(OUTBOX_MAX_ATTEMPTS - 1), "pending")

    def test_terminal_status_failed_at_cap(self):
        self.assertEqual(terminal_status(OUTBOX_MAX_ATTEMPTS), "failed")


class TerminalDeliverySettlementTests(unittest.IsolatedAsyncioTestCase):
    async def test_exhausted_lottery_task_is_failed_and_claim_is_released(self):
        from app.services import outbox

        fake = _TerminalFailureDatabase()
        original = outbox.database
        outbox.database = fake
        try:
            await _settle_terminal_delivery_failure(
                {
                    "id": 1,
                    "stream_key": "lottery_tasks",
                    "dedup_key": "task-1",
                },
                OUTBOX_MAX_ATTEMPTS,
                RuntimeError("redis unavailable"),
            )
        finally:
            outbox.database = original

        statements = "\n".join(query for query, _ in fake.executions)
        self.assertIn("SET status = 'failed'", statements)
        self.assertIn("UPDATE task_runs", statements)
        self.assertIn("UPDATE lotteries SET status = 'pending'", statements)

    async def test_running_task_is_not_reversed_after_ambiguous_delivery(self):
        from app.services import outbox

        fake = _TerminalFailureDatabase(task_status="running")
        original = outbox.database
        outbox.database = fake
        try:
            await _settle_terminal_delivery_failure(
                {
                    "id": 1,
                    "stream_key": "lottery_tasks",
                    "dedup_key": "task-1",
                },
                OUTBOX_MAX_ATTEMPTS,
                RuntimeError("redis response lost"),
            )
        finally:
            outbox.database = original

        statements = "\n".join(query for query, _ in fake.executions)
        self.assertIn("SET status = 'failed'", statements)
        self.assertNotIn("UPDATE task_runs", statements)
        self.assertNotIn("UPDATE lotteries", statements)

    async def test_exhausted_probe_delivery_marks_queued_calibration_failed(self):
        from app.services import outbox

        fake = _TerminalFailureDatabase(stream_key="adapter_probe_requests")
        original = outbox.database
        outbox.database = fake
        try:
            await _settle_terminal_delivery_failure(
                {
                    "id": 1,
                    "stream_key": "adapter_probe_requests",
                    "dedup_key": "adapter-probe:probe-1",
                },
                OUTBOX_MAX_ATTEMPTS,
                RuntimeError("redis unavailable"),
            )
        finally:
            outbox.database = original

        statements = "\n".join(query for query, _ in fake.executions)
        self.assertIn("SET status = 'failed'", statements)
        self.assertIn("UPDATE adapter_calibrations", statements)
        self.assertIn("operation_kind = 'adapter_probe'", statements)
        self.assertNotIn("UPDATE task_runs", statements)

    async def test_ambiguous_probe_delivery_does_not_release_running_probe(self):
        from app.services import outbox

        fake = _TerminalFailureDatabase(
            stream_key="adapter_probe_requests",
            probe_status="running",
        )
        original = outbox.database
        outbox.database = fake
        try:
            await _settle_terminal_delivery_failure(
                {
                    "id": 1,
                    "stream_key": "adapter_probe_requests",
                    "dedup_key": "adapter-probe:probe-1",
                },
                OUTBOX_MAX_ATTEMPTS,
                RuntimeError("redis response lost"),
            )
        finally:
            outbox.database = original

        statements = "\n".join(query for query, _ in fake.executions)
        self.assertNotIn("UPDATE adapter_calibrations", statements)
        self.assertNotIn("UPDATE account_operation_leases", statements)

    async def test_stale_terminal_failure_cannot_overwrite_newer_claim(self):
        from app.services import outbox

        fake = _TerminalFailureDatabase(outbox_attempts=OUTBOX_MAX_ATTEMPTS + 1)
        original = outbox.database
        outbox.database = fake
        try:
            await _settle_terminal_delivery_failure(
                {"id": 1, "stream_key": "lottery_tasks", "dedup_key": "task-1"},
                OUTBOX_MAX_ATTEMPTS,
                RuntimeError("late failure"),
            )
        finally:
            outbox.database = original

        self.assertEqual(fake.executions, [])


class _StaleReceiptDatabase:
    def __init__(self, affected=0):
        self.affected = affected
        self.executions = []

    def transaction(self):
        return _Transaction()

    async def execute(self, query, values=None):
        self.executions.append((str(query), dict(values or {})))
        return 0

    async def fetch_one(self, query, values=None):
        self.executions.append((str(query), dict(values or {})))
        if "ROW_COUNT()" in str(query):
            return {"affected": self.affected}
        raise AssertionError(query)


class _SuccessfulRedis:
    async def xadd(self, *_args, **_kwargs):
        return "1-0"


class DeliveryFencingTests(unittest.IsolatedAsyncioTestCase):
    async def test_stale_success_receipt_does_not_report_newer_claim_as_sent(self):
        from app.services import outbox

        fake_database = _StaleReceiptDatabase()
        original_database = outbox.database
        original_redis = outbox.redis
        outbox.database = fake_database
        outbox.redis = _SuccessfulRedis()
        try:
            delivered = await _deliver_claimed(
                {
                    "id": 1,
                    "stream_key": "lottery_tasks",
                    "payload": json.dumps({"task_id": "task-1"}),
                    "attempts": 2,
                }
            )
        finally:
            outbox.database = original_database
            outbox.redis = original_redis

        self.assertFalse(delivered)
        query, values = fake_database.executions[0]
        self.assertIn("status = 'sending'", query)
        self.assertIn("attempts = :attempts", query)
        self.assertEqual(values["attempts"], 2)

    async def test_success_receipt_uses_row_count_even_when_execute_returns_zero(self):
        from app.services import outbox

        fake_database = _StaleReceiptDatabase(affected=1)
        original_database = outbox.database
        original_redis = outbox.redis
        outbox.database = fake_database
        outbox.redis = _SuccessfulRedis()
        try:
            delivered = await _deliver_claimed(
                {
                    "id": 1,
                    "stream_key": "lottery_tasks",
                    "payload": json.dumps({"task_id": "task-1"}),
                    "attempts": 2,
                }
            )
        finally:
            outbox.database = original_database
            outbox.redis = original_redis

        self.assertTrue(delivered)
        self.assertTrue(any("ROW_COUNT()" in query for query, _ in fake_database.executions))


class _AffectedRowsDatabase:
    def __init__(self):
        self.query = None
        self.values = None

    def transaction(self):
        return _Transaction()

    async def execute(self, query, values=None):
        self.query = str(query)
        self.values = dict(values or {})
        return 3

    async def fetch_one(self, query, values=None):
        self.row_count_query = str(query)
        return {"affected": 3}


class OrphanLockSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_partial_commit_is_not_unlocked_for_replay(self):
        from app.services import outbox

        fake = _AffectedRowsDatabase()
        original = outbox.database
        outbox.database = fake
        try:
            affected = await reconcile_orphaned_locks(grace_minutes=17)
        finally:
            outbox.database = original

        self.assertEqual(affected, 3)
        self.assertEqual(fake.values["grace"], 17)
        compact = " ".join(fake.query.split())
        self.assertIn("tr.reconciliation_required = 0", compact)
        self.assertIn("eai.status IN ('started', 'unknown', 'succeeded')", compact)
        self.assertIn("eai.effect_certainty IN ('unknown', 'confirmed_effect')", compact)
        self.assertIn("eai.effect_certainty <> 'confirmed_no_effect'", compact)
        self.assertIn("THEN 'participated'", compact)


if __name__ == "__main__":
    unittest.main()
