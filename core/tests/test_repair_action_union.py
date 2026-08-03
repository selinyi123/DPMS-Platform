import unittest
from unittest.mock import patch

from fastapi import HTTPException

from app.action_plan import (
    compute_action_plan_hash,
    compute_target_hash,
    weibo_runtime_capability_requirements,
)
from app.api import lotteries
from app.platform_modules import (
    PlatformModuleUnavailableError,
    get_platform_module,
)
from app.services.execution_intents import (
    ExecutionIntentLoadFailure,
    build_frozen_execution_intent,
)


def repair_fixture(*, status="pending", execution_lock=None):
    plan = {
        "version": 2,
        "platform": "bilibili",
        "is_lottery": True,
        "required_actions": ["liked", "commented"],
        "action_payloads": {
            "liked": {},
            "commented": {"text": "参与抽奖"},
        },
        "content_requirements": {
            "follow_targets": [],
            "commented": {"topic_tags": [], "mentions": []},
            "reposted": {"topic_tags": [], "mentions": []},
        },
        "source_content_requirements": {
            "follow_targets": [],
            "commented": {"topic_tags": [], "mentions": []},
            "reposted": {"topic_tags": [], "mentions": []},
        },
        "friend_mention_requirements": {},
        "execution_path_id": "bilibili_api_v2",
        "rule_snapshot_id": 101,
        "rule_hash": "a" * 64,
        "review_required": False,
        "executable": True,
        "confidence": 1.0,
        "source": "operator_complete_attestation",
        "reviewed_by": "operator-1",
        "rule_complete_confirmed": True,
        "unsupported_actions": [],
        "represented_requirements": [],
        "unresolved_requirements": [],
        "ambiguity_patterns": [],
        "payload_validation_errors": [],
        "capability_blockers": [],
    }
    plan["plan_hash"] = compute_action_plan_hash(plan)
    canonical_url = "https://t.bilibili.com/73"
    lottery = {
        "id": 73,
        "platform": "bilibili",
        "status": status,
        "execution_lock": execution_lock,
        "raw_url": canonical_url,
        "canonical_url": canonical_url,
        "action_plan": plan,
        "authoritative_rule_snapshot_id": 101,
        "rule_hash": "a" * 64,
        "action_plan_hash": plan["plan_hash"],
    }
    plan_binding = {
        "rule_snapshot_id": 101,
        "rule_hash": "a" * 64,
        "action_plan_hash": plan["plan_hash"],
        "execution_path_id": "bilibili_api_v2",
        "target_hash": compute_target_hash(canonical_url),
        "config_hash": "b" * 64,
        "execution_revision": 1,
        "required_actions": ("liked", "commented"),
        "action_plan": plan,
    }
    intent = build_frozen_execution_intent(
        lottery,
        source_task_id="00000000-0000-4000-8000-000000000001",
        source_account_id=9,
        plan_binding=plan_binding,
        intent_id="00000000-0000-4000-8000-000000000002",
    )
    return lottery, intent


def weibo_repair_fixture():
    actions = ["liked", "commented"]
    plan = {
        "version": 2,
        "platform": "weibo",
        "is_lottery": True,
        "required_actions": actions,
        "action_payloads": {
            "liked": {},
            "commented": {
                "text": "join lottery",
                "topic_tags": [],
                "mentions": [],
            },
        },
        "content_requirements": {
            "follow_targets": [],
            "commented": {"topic_tags": [], "mentions": []},
            "reposted": {"topic_tags": [], "mentions": []},
        },
        "source_content_requirements": {
            "follow_targets": [],
            "commented": {"topic_tags": [], "mentions": []},
            "reposted": {"topic_tags": [], "mentions": []},
        },
        "friend_mention_requirements": {},
        "runtime_capability_requirements": (
            weibo_runtime_capability_requirements(actions)
        ),
        "execution_path_id": "weibo_oauth_v1",
        "rule_snapshot_id": 201,
        "rule_hash": "c" * 64,
        "review_required": False,
        "executable": True,
        "confidence": 1.0,
        "source": "operator_complete_attestation",
        "reviewed_by": "operator-1",
        "rule_complete_confirmed": True,
        "unsupported_actions": [],
        "represented_requirements": [],
        "unresolved_requirements": [],
        "ambiguity_patterns": [],
        "payload_validation_errors": [],
        "capability_blockers": [],
    }
    plan["plan_hash"] = compute_action_plan_hash(plan)
    canonical_url = "https://weibo.com/1234567890/AbCdEfGhI"
    lottery = {
        "id": 74,
        "platform": "weibo",
        "status": "pending",
        "execution_lock": None,
        "raw_url": canonical_url,
        "canonical_url": canonical_url,
        "action_plan": plan,
        "authoritative_rule_snapshot_id": 201,
        "rule_hash": "c" * 64,
        "action_plan_hash": plan["plan_hash"],
    }
    plan_binding = {
        "rule_snapshot_id": 201,
        "rule_hash": "c" * 64,
        "action_plan_hash": plan["plan_hash"],
        "execution_path_id": "weibo_oauth_v1",
        "target_hash": compute_target_hash(canonical_url),
        "config_hash": "d" * 64,
        "execution_revision": 3,
        "required_actions": tuple(actions),
        "action_plan": plan,
    }
    intent = build_frozen_execution_intent(
        lottery,
        source_task_id="00000000-0000-4000-8000-000000000011",
        source_account_id=10,
        plan_binding=plan_binding,
        intent_id="00000000-0000-4000-8000-000000000012",
    )
    return lottery, intent


class FakeRepairDatabase:
    async def fetch_all(self, query, values=None):
        if "FROM bilibili_action_ledger" in query:
            return [{"phase": "liked"}]
        if "FROM external_action_intents" in query:
            return []
        if "FROM events" in query:
            return [{"phase": "followed"}, {"phase": "commented"}]
        if "FROM task_phases" in query:
            # The real schema keeps only the latest phase per task. This is a
            # legacy fallback, not a source of complete per-action history.
            return [{"phase": "reposted"}]
        raise AssertionError(f"Unexpected query: {query}")


class PartialRepairDatabase:
    async def fetch_all(self, query, values=None):
        if "FROM bilibili_action_ledger" in query:
            return [{"phase": "liked"}]
        if (
            "FROM external_action_intents" in query
            or "FROM events" in query
            or "FROM task_phases" in query
        ):
            return []
        raise AssertionError(f"Unexpected query: {query}")


class StrictIntentScopeDatabase:
    def __init__(self):
        self.queries = []

    async def fetch_all(self, query, values=None):
        normalized = " ".join(query.split())
        self.queries.append((normalized, dict(values or {})))
        self.assert_exact_scope(normalized, values or {})
        if "FROM bilibili_action_ledger" in normalized:
            return [{"phase": "liked"}]
        if "FROM external_action_intents" in normalized:
            return []
        if "FROM events" in normalized:
            return []
        if "FROM task_phases" in normalized:
            return []
        raise AssertionError(f"Unexpected query: {query}")

    @staticmethod
    def assert_exact_scope(query, values):
        assert "JOIN task_execution_intent_bindings execution_binding" in query
        assert "execution_binding.intent_id = :scope_intent_id" in query
        assert "execution_binding.account_id = :scope_account_id" in query
        assert values["lottery_id"] == 73
        assert (
            values["scope_intent_id"]
            == "00000000-0000-4000-8000-000000000002"
        )
        assert values["scope_account_id"] == 9


class WeiboPartialSuccessDatabase:
    def __init__(self, *, include_unknown: bool = True):
        self.external_action_query = ""
        self.include_unknown = include_unknown

    async def fetch_all(self, query, values=None):
        if "FROM bilibili_action_ledger" in query:
            return []
        if "FROM external_action_intents" in query:
            normalized = " ".join(query.split())
            self.external_action_query = normalized
            for predicate in ("tr.task_mode = 'real_run'",):
                if predicate not in normalized:
                    raise AssertionError(f"Missing authoritative predicate: {predicate}")
            rows = [
                {
                    "intent_id": "00000000-0000-4000-8000-000000000021",
                    "phase": "liked",
                    "status": "succeeded",
                    "effect_certainty": "confirmed_effect",
                    "outcome": "ok",
                },
                {
                    "intent_id": "00000000-0000-4000-8000-000000000023",
                    "phase": "commented",
                    "status": "failed",
                    "effect_certainty": "confirmed_no_effect",
                    "outcome": "retry",
                },
            ]
            if self.include_unknown:
                rows.insert(
                    1,
                    {
                        "intent_id": (
                            "00000000-0000-4000-8000-000000000022"
                        ),
                        "phase": "commented",
                        "status": "unknown",
                        "effect_certainty": "unknown",
                        "outcome": "unknown",
                    },
                )
            return rows
        if "FROM events" in query or "FROM task_phases" in query:
            return []
        raise AssertionError(f"Unexpected query: {query}")


class BatchAuthorityDatabase:
    async def fetch_all(self, query, values=None):
        if "FROM bilibili_action_ledger" in query:
            return []
        if "FROM external_action_intents" in query:
            return [
                {
                    "lottery_id": 73,
                    "intent_id": "00000000-0000-4000-8000-000000000041",
                    "phase": "like",
                    "status": "succeeded",
                    "effect_certainty": "confirmed_effect",
                    "outcome": "ok",
                },
                {
                    "lottery_id": 74,
                    "intent_id": "00000000-0000-4000-8000-000000000042",
                    "phase": "liked",
                    "status": "succeeded",
                    "effect_certainty": "confirmed_effect",
                    "outcome": "ok",
                },
            ]
        if "FROM events" in query or "FROM task_phases" in query:
            return []
        raise AssertionError(f"Unexpected query: {query}")


class RepairActionUnionTests(unittest.IsolatedAsyncioTestCase):
    def test_bilibili_historical_api_action_is_canonical_completion(self):
        authority = lotteries._build_real_run_completion_authority(
            "bilibili",
            legacy_completed_actions=[],
            external_action_rows=[
                {
                    "intent_id": "00000000-0000-4000-8000-000000000031",
                    "phase": "like",
                    "status": "succeeded",
                    "effect_certainty": "confirmed_effect",
                    "outcome": "ok",
                }
            ],
        )

        self.assertEqual(authority.completed_actions, ("liked",))
        self.assertEqual(authority.blockers, ())

    def test_bilibili_canonical_action_remains_unchanged(self):
        authority = lotteries._build_real_run_completion_authority(
            "bilibili",
            legacy_completed_actions=[],
            external_action_rows=[
                {
                    "intent_id": "00000000-0000-4000-8000-000000000032",
                    "phase": "liked",
                    "status": "failed",
                    "effect_certainty": "confirmed_no_effect",
                    "outcome": "skip",
                }
            ],
        )

        self.assertEqual(authority.completed_actions, ())
        self.assertEqual(authority.blockers, ())

    def test_weibo_rejected_action_is_settled_confirmed_no_effect(self):
        authority = lotteries._build_real_run_completion_authority(
            "weibo",
            legacy_completed_actions=[],
            external_action_rows=[
                {
                    "intent_id": "00000000-0000-4000-8000-000000000036",
                    "phase": "liked",
                    "status": "failed",
                    "effect_certainty": "confirmed_no_effect",
                    "outcome": "rejected",
                }
            ],
        )

        self.assertEqual(authority.completed_actions, ())
        self.assertEqual(authority.blockers, ())

    def test_unknown_external_action_fails_closed(self):
        authority = lotteries._build_real_run_completion_authority(
            "bilibili",
            legacy_completed_actions=[],
            external_action_rows=[
                {
                    "intent_id": "00000000-0000-4000-8000-000000000033",
                    "phase": "reserve",
                    "status": "succeeded",
                    "effect_certainty": "confirmed_effect",
                    "outcome": "ok",
                }
            ],
        )

        self.assertEqual(authority.completed_actions, ())
        self.assertEqual(len(authority.blockers), 1)
        self.assertEqual(
            authority.blockers[0].reason,
            "external_action_intent_lifecycle_invalid",
        )
        self.assertEqual(authority.blockers[0].action, "reserve")

    def test_weibo_canonical_action_does_not_accept_bilibili_alias(self):
        authority = lotteries._build_real_run_completion_authority(
            "weibo",
            legacy_completed_actions=[],
            external_action_rows=[
                {
                    "intent_id": "00000000-0000-4000-8000-000000000034",
                    "phase": "liked",
                    "status": "succeeded",
                    "effect_certainty": "confirmed_effect",
                    "outcome": "ok",
                }
            ],
        )
        invalid_alias = lotteries._build_real_run_completion_authority(
            "weibo",
            legacy_completed_actions=[],
            external_action_rows=[
                {
                    "intent_id": "00000000-0000-4000-8000-000000000035",
                    "phase": "like",
                    "status": "succeeded",
                    "effect_certainty": "confirmed_effect",
                    "outcome": "ok",
                }
            ],
        )

        self.assertEqual(authority.completed_actions, ("liked",))
        self.assertEqual(authority.blockers, ())
        self.assertEqual(invalid_alias.completed_actions, ())
        self.assertEqual(len(invalid_alias.blockers), 1)

    async def test_batch_authority_isolates_one_unavailable_platform(self):
        bilibili_lottery, bilibili_intent = repair_fixture()
        modules = {
            "bilibili": get_platform_module("bilibili"),
            "weibo": get_platform_module("weibo"),
        }

        def load(platform):
            if platform == "bilibili":
                raise PlatformModuleUnavailableError("bilibili")
            return modules.get(platform)

        original_database = lotteries.database
        lotteries.database = BatchAuthorityDatabase()
        try:
            with patch.object(
                lotteries,
                "get_platform_module",
                side_effect=load,
            ):
                authorities = (
                    await lotteries
                    .load_real_run_completion_authorities_for_lotteries(
                        {73: "bilibili", 74: "weibo"}
                    )
                )
                bilibili_plan = await lotteries.build_lottery_repair_plan(
                    bilibili_lottery,
                    completion_authority=authorities[73],
                    execution_intent=bilibili_intent,
                )
        finally:
            lotteries.database = original_database

        self.assertEqual(authorities[73].completed_actions, ())
        self.assertEqual(len(authorities[73].blockers), 1)
        self.assertEqual(
            authorities[73].blockers[0].reason,
            "platform_module_unavailable",
        )
        self.assertEqual(authorities[74].completed_actions, ("liked",))
        self.assertEqual(authorities[74].blockers, ())
        self.assertFalse(bilibili_plan["eligible"])
        self.assertEqual(
            bilibili_plan["reason"],
            "reconciliation_required",
        )
        self.assertEqual(
            bilibili_plan["completion_authority_blockers"][0]["reason"],
            "platform_module_unavailable",
        )

    async def test_partial_success_is_advertised_as_repair_executable(self):
        lottery, intent = repair_fixture()
        plan = await lotteries.build_lottery_repair_plan(
            lottery,
            completed_actions=["liked"],
            execution_intent=intent,
            dispatch_runtime_ready=True,
        )

        self.assertTrue(plan["eligible"])
        self.assertEqual(plan["reason"], "missing_actions_available")
        self.assertTrue(plan["dispatch_contract_supported"])
        self.assertTrue(plan["dispatch_workers_ready"])
        self.assertTrue(plan["dispatch_supported"])
        self.assertTrue(plan["executable"])
        self.assertIsNone(plan["dispatch_blocker"])
        self.assertTrue(plan["repair_action_plan"]["is_lottery"])
        self.assertEqual(
            plan["repair_action_plan"]["required_actions"],
            ["commented"],
        )

    async def test_eligible_repair_is_not_advertised_executable_without_live_workers(self):
        lottery, intent = repair_fixture()
        plan = await lotteries.build_lottery_repair_plan(
            lottery,
            completed_actions=["liked"],
            execution_intent=intent,
        )

        self.assertTrue(plan["eligible"])
        self.assertTrue(plan["dispatch_contract_supported"])
        self.assertFalse(plan["dispatch_workers_ready"])
        self.assertFalse(plan["dispatch_supported"])
        self.assertFalse(plan["executable"])
        self.assertEqual(
            plan["dispatch_blocker"],
            lotteries.REPAIR_DISPATCH_BLOCKER,
        )

    async def test_legacy_row_without_frozen_intent_is_fail_closed(self):
        lottery, _intent = repair_fixture()

        plan = await lotteries.build_lottery_repair_plan(
            lottery,
            completed_actions=["liked"],
            execution_intent=None,
        )

        self.assertFalse(plan["eligible"])
        self.assertFalse(plan["executable"])
        self.assertEqual(plan["reason"], "execution_intent_missing")
        self.assertEqual(plan["required_actions"], [])
        self.assertIsNone(plan["repair_action_plan"])

    async def test_mixed_legacy_phases_and_action_ledger_are_unioned(self):
        original_database = lotteries.database
        lotteries.database = FakeRepairDatabase()
        try:
            completed = await lotteries.completed_real_run_actions(
                73,
                "bilibili",
            )
        finally:
            lotteries.database = original_database

        self.assertEqual(completed, ["followed", "liked", "commented", "reposted"])

    async def test_repair_authority_scopes_all_sources_to_current_intent_account(self):
        lottery, intent = repair_fixture()
        original_database = lotteries.database
        fake_database = StrictIntentScopeDatabase()
        lotteries.database = fake_database
        try:
            authority = await lotteries.load_real_run_completion_authority(
                lottery["id"],
                lottery["platform"],
                execution_intent=intent,
            )
        finally:
            lotteries.database = original_database

        self.assertEqual(authority.completed_actions, ("liked",))
        self.assertEqual(authority.blockers, ())
        self.assertEqual(len(fake_database.queries), 4)

    async def test_repair_authority_rejects_missing_exact_scope_without_querying(self):
        class NoQueryDatabase:
            async def fetch_all(self, _query, _values=None):
                raise AssertionError("invalid scope must fail before querying")

        original_database = lotteries.database
        lotteries.database = NoQueryDatabase()
        try:
            authority = await lotteries.load_real_run_completion_authority(
                73,
                "bilibili",
                execution_intent=None,
            )
        finally:
            lotteries.database = original_database

        self.assertEqual(authority.completed_actions, ())
        self.assertEqual(len(authority.blockers), 1)
        self.assertEqual(
            authority.blockers[0].reason,
            "execution_intent_scope_unavailable",
        )

    async def test_weibo_unsettled_intent_blocks_repair_under_row_lock(self):
        lottery, intent = weibo_repair_fixture()
        original_database = lotteries.database
        fake_database = WeiboPartialSuccessDatabase()
        lotteries.database = fake_database
        try:
            authority = await lotteries.load_real_run_completion_authority(
                lottery["id"],
                "weibo",
                for_update=True,
            )
            plan = await lotteries.build_lottery_repair_plan(
                lottery,
                completion_authority=authority,
                execution_intent=intent,
            )
        finally:
            lotteries.database = original_database

        self.assertEqual(authority.completed_actions, ("liked",))
        self.assertEqual(len(authority.blockers), 1)
        self.assertTrue(
            fake_database.external_action_query.endswith("FOR UPDATE")
        )
        self.assertFalse(plan["eligible"])
        self.assertEqual(plan["reason"], "reconciliation_required")
        self.assertEqual(plan["missing_actions"], [])
        with self.assertRaises(HTTPException) as caught:
            lotteries.require_completion_authority_settled(authority)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(
            caught.exception.detail["code"],
            "reconciliation_required",
        )

    async def test_locked_completion_projection_cannot_hide_blockers(self):
        original_database = lotteries.database
        lotteries.database = WeiboPartialSuccessDatabase()
        try:
            with self.assertRaises(HTTPException) as caught:
                await lotteries.completed_real_run_actions(
                    74,
                    "weibo",
                    for_update=True,
                )
        finally:
            lotteries.database = original_database

        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(
            caught.exception.detail["code"],
            "reconciliation_required",
        )

    async def test_weibo_settled_failure_is_an_exact_repair_gap(self):
        lottery, intent = weibo_repair_fixture()
        original_database = lotteries.database
        fake_database = WeiboPartialSuccessDatabase(
            include_unknown=False,
        )
        lotteries.database = fake_database
        try:
            authority = await lotteries.load_real_run_completion_authority(
                lottery["id"],
                "weibo",
                for_update=True,
            )
            lotteries.require_completion_authority_settled(authority)
            plan = await lotteries.build_lottery_repair_plan(
                lottery,
                completion_authority=authority,
                execution_intent=intent,
            )
        finally:
            lotteries.database = original_database

        self.assertEqual(authority.completed_actions, ("liked",))
        self.assertEqual(authority.blockers, ())
        self.assertTrue(plan["eligible"])
        self.assertEqual(plan["missing_actions"], ["commented"])
        self.assertEqual(
            plan["repair_action_plan"]["required_actions"],
            ["commented"],
        )

    async def test_completed_action_evidence_failure_is_fail_closed(self):
        class FailingDatabase:
            async def fetch_all(self, _query, _values=None):
                raise RuntimeError("ledger unavailable")

        original_database = lotteries.database
        lotteries.database = FailingDatabase()
        try:
            with self.assertRaisesRegex(RuntimeError, "ledger unavailable"):
                await lotteries.completed_real_run_actions(73, "bilibili")
        finally:
            lotteries.database = original_database

    async def test_batch_local_corrupt_intent_is_reported_not_missing(self):
        lottery, _ = repair_fixture()

        plan = await lotteries.build_lottery_repair_plan(
            lottery,
            completed_actions=[],
            execution_intent=ExecutionIntentLoadFailure(
                lottery_id=lottery["id"],
                code="execution_intent_hash_mismatch",
            ),
        )

        self.assertFalse(plan["eligible"])
        self.assertFalse(plan["executable"])
        self.assertEqual(plan["reason"], "execution_intent_invalid")
        self.assertEqual(
            plan["integrity_blocker"],
            "execution_intent_hash_mismatch",
        )

    async def test_active_execution_is_not_advertised_as_repair_eligible(self):
        lottery, intent = repair_fixture(
            status="running",
            execution_lock="task-active",
        )
        original_database = lotteries.database
        lotteries.database = PartialRepairDatabase()
        try:
            plan = await lotteries.build_lottery_repair_plan(
                lottery,
                execution_intent=intent,
            )
        finally:
            lotteries.database = original_database

        self.assertFalse(plan["eligible"])
        self.assertFalse(plan["executable"])
        self.assertEqual(plan["reason"], "execution_in_flight_or_reconciliation_required")

    async def test_terminal_lottery_is_not_advertised_as_repair_eligible(self):
        lottery, intent = repair_fixture(status="participated")
        original_database = lotteries.database
        lotteries.database = PartialRepairDatabase()
        try:
            plan = await lotteries.build_lottery_repair_plan(
                lottery,
                execution_intent=intent,
            )
        finally:
            lotteries.database = original_database

        self.assertFalse(plan["eligible"])
        self.assertFalse(plan["executable"])
        self.assertEqual(plan["reason"], "lottery_not_pending")


if __name__ == "__main__":
    unittest.main()
