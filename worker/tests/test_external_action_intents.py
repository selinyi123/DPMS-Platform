import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.external_action_intents import (  # noqa: E402
    ExternalActionIntentBlocked,
    mark_action_intent_unknown,
    prepare_and_start_action_intent,
    renew_account_operation_lease,
    settle_action_intent,
)


class AsyncTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeIntentDatabase:
    def __init__(self):
        self.task = {
            "task_id": "task-1",
            "account_id": 41,
            "lottery_id": 73,
            "task_status": "running",
            "worker_id": "worker-a",
            "account_lease_id": "lease-1",
            "account_lease_generation": 7,
            "reconciliation_required": 0,
            "execution_intent_kind": "full",
            "lease_id": "lease-1",
            "lease_generation": 7,
            "operation_kind": "real_run",
            "owner_id": "task-1",
            "lease_task_id": "task-1",
            "lease_active": 1,
            "lease_unreleased": 1,
            "lease_latest_generation": 1,
            "active_account_lease_count": 1,
        }
        self.intent = None
        self.lease_updates = []

    def transaction(self):
        return AsyncTransaction()

    async def fetch_one(self, query, values=None):
        values = values or {}
        if "FROM task_runs tr" in query:
            return dict(self.task)
        if "FROM external_action_intents" in query:
            if self.intent is None:
                return None
            if values.get("intent_id") and values["intent_id"] != self.intent["intent_id"]:
                return None
            return dict(self.intent)
        if "FROM account_operation_leases" in query:
            return {
                "lease_id": self.task["lease_id"],
                "generation": self.task["lease_generation"],
                "lease_active": self.task["lease_active"],
                "lease_unreleased": self.task["lease_unreleased"],
                "lease_latest_generation": self.task["lease_latest_generation"],
                "active_account_lease_count": self.task["active_account_lease_count"],
            }
        raise AssertionError(f"unexpected fetch_one query: {query}")

    async def execute(self, query, values=None):
        values = dict(values or {})
        if "INSERT INTO external_action_intents" in query:
            self.intent = {
                **values,
                "status": "prepared",
                "effect_certainty": "not_started",
            }
            return
        if "SET status = 'started'" in query:
            if self.intent and self.intent["status"] == "prepared":
                self.intent.update(status="started", effect_certainty="unknown")
            return
        if "SET status = 'prepared'" in query:
            if self.intent and self.intent["status"] == "failed":
                self.intent.update(
                    status="prepared",
                    effect_certainty="not_started",
                    attempt_no=values["attempt_no"],
                )
            return
        if "SET status = 'unknown'" in query:
            if self.intent and self.intent["status"] in {"started", "unknown"}:
                self.intent.update(
                    status="unknown",
                    effect_certainty="unknown",
                    outcome="unknown",
                    started_at=self.intent.get("started_at") or "now",
                    completed_at=self.intent.get("completed_at") or "now",
                    reconciliation_note=self.intent.get("reconciliation_note")
                    or values.get("note"),
                )
            return
        if "SET status = :status" in query and "external_action_intents" in query:
            if self.intent and self.intent["status"] == "started":
                self.intent.update(
                    status=values["status"],
                    effect_certainty=values["effect_certainty"],
                    outcome=values["outcome"],
                )
            return
        if "UPDATE task_runs" in query:
            self.task["reconciliation_required"] = 1
            return
        if "UPDATE account_operation_leases" in query:
            self.lease_updates.append(values)
            return
        raise AssertionError(f"unexpected execute query: {query}")


class ExternalActionIntentTests(unittest.IsolatedAsyncioTestCase):
    async def start(
        self,
        db,
        payload=None,
        *,
        execution_intent_kind="full",
    ):
        return await prepare_and_start_action_intent(
            db=db,
            task_id="task-1",
            account_id=41,
            lottery_id=73,
            worker_id="worker-a",
            execution_intent_kind=execution_intent_kind,
            action="comment",
            payload=payload or {"text": "#话题# 精确评论"},
        )

    async def test_prepared_started_and_confirmed_success_are_durable(self):
        db = FakeIntentDatabase()
        intent = await self.start(db)
        self.assertEqual(db.intent["status"], "started")
        self.assertEqual(intent.attempt_no, 1)

        await settle_action_intent(
            db=db,
            intent=intent,
            succeeded=True,
            outcome="ok",
        )
        self.assertEqual(db.intent["status"], "succeeded")
        self.assertEqual(db.intent["effect_certainty"], "confirmed_effect")
        self.assertEqual(db.intent["outcome"], "ok")

        with self.assertRaises(ExternalActionIntentBlocked) as caught:
            await self.start(db)
        self.assertEqual(caught.exception.code, "intent_succeeded_requires_reconciliation")
        self.assertEqual(db.task["reconciliation_required"], 1)

    async def test_explicit_failure_can_use_a_new_fenced_attempt(self):
        db = FakeIntentDatabase()
        first = await self.start(db)
        await settle_action_intent(
            db=db,
            intent=first,
            succeeded=False,
            outcome="retry",
            error_message="explicit retry response",
        )
        second = await self.start(db)
        self.assertEqual(second.intent_id, first.intent_id)
        self.assertEqual(second.attempt_no, 2)
        self.assertEqual(db.intent["status"], "started")
        self.assertEqual(db.intent["effect_certainty"], "unknown")

    async def test_only_explicit_known_failure_can_claim_no_effect(self):
        db = FakeIntentDatabase()
        intent = await self.start(db)
        with self.assertRaises(ExternalActionIntentBlocked) as caught:
            await settle_action_intent(
                db=db,
                intent=intent,
                succeeded=False,
                outcome="unknown",
            )
        self.assertEqual(caught.exception.code, "intent_outcome_invalid")
        self.assertEqual(db.intent["status"], "started")
        self.assertEqual(db.intent["effect_certainty"], "unknown")

        await settle_action_intent(
            db=db,
            intent=intent,
            succeeded=False,
            outcome="auth",
            error_message="explicit API response",
        )
        self.assertEqual(db.intent["status"], "failed")
        self.assertEqual(db.intent["effect_certainty"], "confirmed_no_effect")

    async def test_unclassified_failures_and_non_ok_success_never_claim_certainty(self):
        for succeeded, outcome in (
            (False, "timeout"),
            (False, "fatal"),
            (False, "transport"),
            (False, "unrecognized"),
            (True, "skip"),
            (True, "fatal"),
        ):
            with self.subTest(succeeded=succeeded, outcome=outcome):
                db = FakeIntentDatabase()
                intent = await self.start(db)
                with self.assertRaises(ExternalActionIntentBlocked) as caught:
                    await settle_action_intent(
                        db=db,
                        intent=intent,
                        succeeded=succeeded,
                        outcome=outcome,
                    )
                self.assertEqual(caught.exception.code, "intent_outcome_invalid")
                self.assertEqual(db.intent["status"], "started")
                self.assertEqual(db.intent["effect_certainty"], "unknown")

    async def test_unsettled_started_intent_is_never_automatically_replayed(self):
        db = FakeIntentDatabase()
        await self.start(db)
        with self.assertRaises(ExternalActionIntentBlocked) as caught:
            await self.start(db)
        self.assertEqual(
            caught.exception.code, "intent_started_requires_reconciliation"
        )
        self.assertEqual(db.intent["status"], "unknown")
        self.assertIsNotNone(db.intent["started_at"])
        self.assertIsNotNone(db.intent["completed_at"])
        self.assertEqual(db.intent["outcome"], "unknown")
        self.assertTrue(db.intent["reconciliation_note"])
        self.assertEqual(db.task["reconciliation_required"], 1)

    async def test_unknown_transition_marks_task_for_reconciliation(self):
        db = FakeIntentDatabase()
        intent = await self.start(db)
        await mark_action_intent_unknown(
            db=db,
            intent=intent,
            reason="timeout after request write",
        )
        self.assertEqual(db.intent["status"], "unknown")
        self.assertIsNotNone(db.intent["started_at"])
        self.assertIsNotNone(db.intent["completed_at"])
        self.assertEqual(db.intent["outcome"], "unknown")
        self.assertTrue(db.intent["reconciliation_note"])
        self.assertEqual(db.task["reconciliation_required"], 1)

        with self.assertRaises(ExternalActionIntentBlocked) as caught:
            await self.start(db)
        self.assertEqual(caught.exception.code, "task_reconciliation_required")

    async def test_payload_change_cannot_reuse_an_existing_intent(self):
        db = FakeIntentDatabase()
        await self.start(db)
        with self.assertRaises(ExternalActionIntentBlocked) as caught:
            await self.start(db, {"text": "different unreviewed text"})
        self.assertEqual(caught.exception.code, "intent_binding_invalid")

    async def test_expired_or_reassigned_lease_blocks_before_intent(self):
        for field, value in (
            ("lease_active", 0),
            ("lease_generation", 8),
            ("owner_id", "other-task"),
            ("worker_id", "worker-b"),
        ):
            with self.subTest(field=field):
                db = FakeIntentDatabase()
                db.task[field] = value
                with self.assertRaises(ExternalActionIntentBlocked) as caught:
                    await self.start(db)
                self.assertEqual(caught.exception.code, "task_lease_binding_invalid")
                self.assertIsNone(db.intent)

    async def test_exact_lease_can_be_renewed_but_stale_generation_cannot(self):
        db = FakeIntentDatabase()
        lease = await renew_account_operation_lease(
            db=db,
            task_id="task-1",
            account_id=41,
            lottery_id=73,
            worker_id="worker-a",
            execution_intent_kind="full",
        )
        self.assertEqual(lease, ("lease-1", 7))

        db.task["account_lease_generation"] = 6
        with self.assertRaises(ExternalActionIntentBlocked):
            await renew_account_operation_lease(
                db=db,
                task_id="task-1",
                account_id=41,
                lottery_id=73,
                worker_id="worker-a",
                execution_intent_kind="full",
            )

    async def test_repair_intent_requires_and_renews_repair_lease(self):
        db = FakeIntentDatabase()
        db.task["execution_intent_kind"] = "repair"
        db.task["operation_kind"] = "repair_run"

        intent = await self.start(
            db,
            execution_intent_kind="repair",
        )
        lease = await renew_account_operation_lease(
            db=db,
            task_id="task-1",
            account_id=41,
            lottery_id=73,
            worker_id="worker-a",
            execution_intent_kind="repair",
        )

        self.assertEqual(intent.task_id, "task-1")
        self.assertEqual(lease, ("lease-1", 7))
        self.assertEqual(
            db.lease_updates[-1]["operation_kind"],
            "repair_run",
        )

        db.task["operation_kind"] = "real_run"
        with self.assertRaises(ExternalActionIntentBlocked) as caught:
            await self.start(
                db,
                execution_intent_kind="repair",
            )
        self.assertEqual(
            caught.exception.code,
            "task_lease_binding_invalid",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
