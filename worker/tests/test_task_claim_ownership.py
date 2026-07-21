import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from bilibili_dry_run_harness import (  # noqa: E402
    FakeDatabase,
    build_task_message,
    stub_httpx,
    stub_playwright,
    stub_worker_runtime_dependencies,
)

stub_playwright()
stub_httpx()
stub_worker_runtime_dependencies()

from app import task_runner  # noqa: E402


class FakeRedis:
    async def xadd(self, *args, **kwargs):
        return "1-0"


class TaskClaimOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_database = task_runner.database
        self.original_redis = task_runner.redis
        self.original_worker_id = task_runner.WORKER_ID
        self.original_record_event = task_runner.record_event

        self.db = FakeDatabase(task_mode="real_run")
        self.db.task_id = "task-1"
        self.db.lottery_execution_lock = "task-1"
        task_runner.database = self.db
        task_runner.redis = FakeRedis()
        task_runner.WORKER_ID = "worker-a"

        async def record_event(*args, **kwargs):
            return "event-1"

        task_runner.record_event = record_event

    def confirmed_intents(self):
        return [
            {
                "intent_id": f"intent-{action}",
                "action": action,
                "status": "succeeded",
                "effect_certainty": "confirmed_effect",
            }
            for action in ("follow", "like", "comment", "repost")
        ]

    async def asyncTearDown(self):
        task_runner.database = self.original_database
        task_runner.redis = self.original_redis
        task_runner.WORKER_ID = self.original_worker_id
        task_runner.record_event = self.original_record_event

    async def test_only_one_worker_can_claim_a_queued_task(self):
        binding = await task_runner.mark_task_started(
            "task-1", 9001, 7001, "real_run", "1-0"
        )
        self.assertEqual(binding.account_id, 9001)
        self.assertEqual(self.db.task_status, "running")
        self.assertEqual(self.db.task_worker_id, "worker-a")
        self.assertEqual(self.db.lottery_status, "running")
        self.assertEqual(self.db.account_status, "executing")

        task_runner.WORKER_ID = "worker-b"
        with self.assertRaises(task_runner.TaskAlreadyClaimed):
            await task_runner.mark_task_started(
                "task-1", 9001, 7001, "real_run", "2-0"
            )

    async def test_non_owner_cannot_finish_running_task(self):
        await task_runner.mark_task_started(
            "task-1", 9001, 7001, "real_run", "1-0"
        )
        task_runner.WORKER_ID = "worker-b"
        with self.assertRaises(task_runner.TaskOwnershipLost):
            await task_runner.mark_task_finished("task-1", False, "lost ownership")
        self.assertEqual(self.db.task_status, "running")

    async def test_unknown_real_action_outcome_keeps_account_quarantined(self):
        await task_runner.mark_task_started(
            "task-1", 9001, 7001, "real_run", "1-0"
        )

        await task_runner.mark_task_finished(
            "task-1",
            False,
            "external_action_outcome_unknown",
            quarantine_account=True,
        )

        self.assertEqual(self.db.task_status, "failed")
        self.assertEqual(self.db.lottery_status, "running")
        self.assertEqual(self.db.lottery_execution_lock, "task-1")
        self.assertEqual(self.db.account_status, "cooling")
        self.assertIsNone(self.db.account_lease_released_at)

    async def test_known_failure_after_any_succeeded_intent_requires_reconciliation(self):
        await task_runner.mark_task_started(
            "task-1", 9001, 7001, "real_run", "1-0"
        )
        self.db.external_action_intents = [
            {
                "intent_id": "intent-follow",
                "status": "succeeded",
                "effect_certainty": "confirmed_effect",
            },
            {
                "intent_id": "intent-comment",
                "status": "failed",
                "effect_certainty": "confirmed_no_effect",
            },
        ]

        await task_runner.mark_task_finished(
            "task-1", False, "comment rejected with known response"
        )

        self.assertEqual(self.db.task_status, "failed")
        self.assertEqual(self.db.task_reconciliation_required, 1)
        self.assertEqual(self.db.lottery_status, "running")
        self.assertEqual(self.db.lottery_execution_lock, "task-1")
        self.assertEqual(self.db.account_status, "cooling")
        self.assertIsNone(self.db.account_lease_released_at)

    async def test_known_failure_before_any_success_can_release_for_safe_retry(self):
        await task_runner.mark_task_started(
            "task-1", 9001, 7001, "real_run", "1-0"
        )
        self.db.external_action_intents = [
            {
                "intent_id": "intent-follow",
                "status": "failed",
                "effect_certainty": "confirmed_no_effect",
            },
        ]

        await task_runner.mark_task_finished("task-1", False, "follow rejected")

        self.assertEqual(self.db.task_status, "failed")
        self.assertEqual(self.db.task_reconciliation_required, 0)
        self.assertEqual(self.db.lottery_status, "pending")
        self.assertIsNone(self.db.lottery_execution_lock)
        self.assertEqual(self.db.account_status, "ready")
        self.assertIsNotNone(self.db.account_lease_released_at)

    async def test_failed_status_with_unknown_effect_certainty_stays_quarantined(self):
        await task_runner.mark_task_started(
            "task-1", 9001, 7001, "real_run", "1-0"
        )
        self.db.external_action_intents = [
            {
                "intent_id": "intent-corrupt",
                "status": "failed",
                "effect_certainty": "unknown",
            }
        ]

        await task_runner.mark_task_finished("task-1", False, "ambiguous failure")

        self.assertEqual(self.db.task_reconciliation_required, 1)
        self.assertEqual(self.db.lottery_status, "running")
        self.assertIsNone(self.db.account_lease_released_at)

    async def test_uncommitted_account_risk_state_keeps_task_and_account_claimed(self):
        message = build_task_message(
            task_id="task-1",
            account_id=9001,
            lottery_id=7001,
            platform="bilibili",
            raw_url=self.db.lottery_raw_url,
            canonical_url=self.db.lottery_canonical_url,
            task_mode="real_run",
            dry_run=False,
            platform_selectors=self.db.selector_config,
            action_plan=self.db.lottery_action_plan,
        )
        originals = {
            "gate": task_runner.enforce_task_real_run_gate,
            "account": task_runner.ensure_account_can_run,
            "execute": task_runner.execute_bilibili_api_real_task,
        }

        async def no_op(*_args, **_kwargs):
            return None

        async def fail_account_status(_task):
            raise task_runner.AccountStatusPersistenceFailed(
                9001,
                "cooling",
                "bilibili_comment_captcha",
                RuntimeError("risk audit unavailable"),
            )

        task_runner.enforce_task_real_run_gate = no_op
        task_runner.ensure_account_can_run = no_op
        task_runner.execute_bilibili_api_real_task = fail_account_status
        try:
            with self.assertRaises(task_runner.TaskSettlementUnconfirmed):
                await task_runner.execute_task_with_phases(message, adapter=None, pool=None)
        finally:
            task_runner.enforce_task_real_run_gate = originals["gate"]
            task_runner.ensure_account_can_run = originals["account"]
            task_runner.execute_bilibili_api_real_task = originals["execute"]

        self.assertEqual("running", self.db.task_status)
        self.assertEqual("running", self.db.lottery_status)
        self.assertEqual("task-1", self.db.lottery_execution_lock)
        self.assertEqual("executing", self.db.account_status)

    async def test_notification_failure_does_not_rollback_terminal_settlement(self):
        await task_runner.mark_task_started(
            "task-1", 9001, 7001, "real_run", "1-0"
        )
        self.db.external_action_intents = self.confirmed_intents()
        original_execute = self.db.execute

        async def fail_notification(query, values=None):
            if "INSERT INTO notify_logs" in str(query):
                raise RuntimeError("notification table unavailable")
            return await original_execute(query, values)

        self.db.execute = fail_notification
        settled = await task_runner.mark_task_finished("task-1", True)

        self.assertTrue(settled)
        self.assertEqual(self.db.task_status, "succeeded")
        self.assertEqual(self.db.task_worker_id, "worker-a")
        self.assertEqual(self.db.lottery_status, "participated")
        self.assertIsNone(self.db.lottery_execution_lock)
        self.assertEqual(self.db.account_status, "ready")

    async def test_real_success_without_any_intent_is_failed_and_retains_fence(self):
        await task_runner.mark_task_started(
            "task-1", 9001, 7001, "real_run", "1-0"
        )

        await task_runner.mark_task_finished("task-1", True)

        self.assertEqual(self.db.task_status, "failed")
        self.assertEqual(self.db.task_reconciliation_required, 1)
        self.assertEqual(self.db.lottery_status, "running")
        self.assertEqual(self.db.lottery_execution_lock, "task-1")
        self.assertEqual(self.db.account_status, "cooling")
        self.assertIsNone(self.db.account_lease_released_at)

    async def test_real_success_with_missing_intent_is_failed_and_retains_fence(self):
        await task_runner.mark_task_started(
            "task-1", 9001, 7001, "real_run", "1-0"
        )
        self.db.external_action_intents = self.confirmed_intents()[:-1]

        await task_runner.mark_task_finished("task-1", True)

        self.assertEqual(self.db.task_status, "failed")
        self.assertEqual(self.db.task_reconciliation_required, 1)
        self.assertIsNone(self.db.account_lease_released_at)

    async def test_real_success_with_extra_intent_is_failed_and_retains_fence(self):
        await task_runner.mark_task_started(
            "task-1", 9001, 7001, "real_run", "1-0"
        )
        self.db.external_action_intents = self.confirmed_intents() + [
            {
                "intent_id": "intent-reserve",
                "action": "reserve",
                "status": "succeeded",
                "effect_certainty": "confirmed_effect",
            }
        ]

        await task_runner.mark_task_finished("task-1", True)

        self.assertEqual(self.db.task_status, "failed")
        self.assertEqual(self.db.task_reconciliation_required, 1)
        self.assertIsNone(self.db.account_lease_released_at)

    async def test_real_success_with_duplicate_action_is_failed_and_retains_fence(self):
        await task_runner.mark_task_started(
            "task-1", 9001, 7001, "real_run", "1-0"
        )
        duplicate = dict(self.confirmed_intents()[0], intent_id="intent-follow-duplicate")
        self.db.external_action_intents = self.confirmed_intents() + [duplicate]

        await task_runner.mark_task_finished("task-1", True)

        self.assertEqual(self.db.task_status, "failed")
        self.assertEqual(self.db.task_reconciliation_required, 1)
        self.assertIsNone(self.db.account_lease_released_at)

    async def test_real_success_with_inconsistent_intent_state_is_failed_and_retains_fence(self):
        await task_runner.mark_task_started(
            "task-1", 9001, 7001, "real_run", "1-0"
        )
        intents = self.confirmed_intents()
        intents[-1].update(status="failed", effect_certainty="confirmed_no_effect")
        self.db.external_action_intents = intents

        await task_runner.mark_task_finished("task-1", True)

        self.assertEqual(self.db.task_status, "failed")
        self.assertEqual(self.db.task_reconciliation_required, 1)
        self.assertIsNone(self.db.account_lease_released_at)

    async def test_phase_event_failure_is_not_treated_as_durable_settlement(self):
        await task_runner.mark_task_started(
            "task-1", 9001, 7001, "real_run", "1-0"
        )

        async def missing_event(*_args, **_kwargs):
            return None

        task_runner.record_event = missing_event
        with self.assertRaisesRegex(RuntimeError, "task_phase_event_persistence_failed"):
            await task_runner.save_phase("task-1", 9001, 7001, "liked")

    async def test_shadow_finish_does_not_release_an_account_owned_by_another_task(self):
        self.db.task_mode = "shadow_run"
        self.db.task_status = "running"
        self.db.task_worker_id = "worker-a"
        self.db.lottery_status = "running"
        self.db.account_status = "executing"

        await task_runner.mark_task_finished("task-1", True)

        self.assertEqual(self.db.task_status, "succeeded")
        self.assertEqual(self.db.account_status, "executing")
        self.assertIsNotNone(self.db.account_lease_released_at)

    async def test_unclaimed_consumer_cannot_fail_queued_task(self):
        with self.assertRaises(task_runner.TaskOwnershipLost):
            await task_runner.mark_task_finished("task-1", False, "gate blocked")
        self.assertEqual(self.db.task_status, "queued")
        self.assertEqual(self.db.lottery_status, "claimed")
        self.assertEqual(self.db.lottery_execution_lock, "task-1")

    async def test_shadow_claim_binds_authoritative_target_and_action_plan(self):
        self.db.task_mode = "shadow_run"
        self.db.lottery_platform = "weibo"
        self.db.lottery_raw_url = "https://weibo.com/123456/AbCdEf1"
        self.db.lottery_canonical_url = self.db.lottery_raw_url
        message = {
            "platform": self.db.lottery_platform,
            "raw_url": self.db.lottery_raw_url,
            "canonical_url": self.db.lottery_canonical_url,
            "action_plan": self.db.lottery_action_plan,
            "selector_config": self.db.selector_config,
        }

        binding = await task_runner.mark_task_started(
            "task-1", 9001, 7001, "shadow_run", "1-0", task_message=message
        )

        self.assertEqual(binding.task_mode, "shadow_run")
        self.assertEqual(self.db.task_status, "running")

    async def test_tampered_shadow_target_is_rejected_before_claim(self):
        self.db.task_mode = "shadow_run"
        self.db.lottery_platform = "weibo"
        self.db.lottery_raw_url = "https://weibo.com/123456/AbCdEf1"
        self.db.lottery_canonical_url = self.db.lottery_raw_url
        message = {
            "platform": self.db.lottery_platform,
            "raw_url": "https://weibo.com/123456/Tampered",
            "canonical_url": self.db.lottery_canonical_url,
            "action_plan": self.db.lottery_action_plan,
            "selector_config": self.db.selector_config,
        }

        with self.assertRaisesRegex(task_runner.TaskClaimConflict, "shadow_task_raw_url_mismatch"):
            await task_runner.mark_task_started(
                "task-1", 9001, 7001, "shadow_run", "1-0", task_message=message
            )

        self.assertEqual(self.db.task_status, "queued")
        self.assertEqual(self.db.lottery_status, "claimed")

    async def test_tampered_shadow_selectors_are_rejected_before_claim(self):
        self.db.task_mode = "shadow_run"
        self.db.lottery_platform = "weibo"
        self.db.lottery_raw_url = "https://weibo.com/123456/AbCdEf1"
        self.db.lottery_canonical_url = self.db.lottery_raw_url
        self.db.selector_config = {"followed": ["button.follow"]}
        message = {
            "platform": self.db.lottery_platform,
            "raw_url": self.db.lottery_raw_url,
            "canonical_url": self.db.lottery_canonical_url,
            "action_plan": self.db.lottery_action_plan,
            "selector_config": {"followed": ["body"]},
        }

        with self.assertRaisesRegex(task_runner.TaskClaimConflict, "task_selector_config_mismatch"):
            await task_runner.mark_task_started(
                "task-1", 9001, 7001, "shadow_run", "1-0", task_message=message
            )

        self.assertEqual(self.db.task_status, "queued")

    async def test_tampered_selector_real_run_is_rejected_before_claim(self):
        self.db.lottery_platform = "weibo"
        self.db.lottery_raw_url = "https://weibo.com/123456/AbCdEf1"
        self.db.lottery_canonical_url = self.db.lottery_raw_url
        self.db.selector_config = {"followed": ["button.follow"]}
        message = {
            "platform": "weibo",
            "raw_url": self.db.lottery_raw_url,
            "canonical_url": self.db.lottery_canonical_url,
            "action_plan": self.db.lottery_action_plan,
            "selector_config": {"followed": ["body"]},
        }

        with self.assertRaisesRegex(task_runner.TaskClaimConflict, "task_selector_config_mismatch"):
            await task_runner.mark_task_started(
                "task-1", 9001, 7001, "real_run", "1-0", task_message=message
            )

        self.assertEqual(self.db.task_status, "queued")


if __name__ == "__main__":
    unittest.main(verbosity=2)
