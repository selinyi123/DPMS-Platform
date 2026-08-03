import base64
import copy
import json
import os
import unittest

os.environ.setdefault("ENCRYPTION_KEY", base64.b64encode(b"0" * 32).decode())
os.environ.setdefault("UPDATE_SECRET", "test-secret")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")

from app.action_plan import (  # noqa: E402
    compute_action_plan_hash,
    compute_target_hash,
    validate_action_plan_v2,
)
from app.platform_modules import get_platform_module  # noqa: E402
from app.services.execution_intents import (  # noqa: E402
    ExecutionIntentError,
    ExecutionIntentLoadFailure,
    build_frozen_execution_intent,
    build_repair_execution_subset,
    build_task_execution_intent_binding,
    coerce_frozen_execution_intent,
    load_lottery_execution_intent,
    load_lottery_execution_intents,
    persist_full_execution_intent,
    persist_repair_execution_binding,
)


INTENT_ID = "00000000-0000-4000-8000-000000000001"
SOURCE_TASK_ID = "00000000-0000-4000-8000-000000000002"
REPAIR_TASK_ID = "00000000-0000-4000-8000-000000000003"
EVIDENCE_ID = "00000000-0000-4000-8000-000000000004"
LEASE_ID = "00000000-0000-4000-8000-000000000005"
GOLDEN_INTENT_HASH = (
    "8c7aff9ec4be40a4080b23262063b4eee95e397f89f7031522761a116f7c2b8e"
)
GOLDEN_REQUESTED_ACTIONS_HASH = (
    "ac4550e84d0839f172d6f344314580fd0d584fdea59fd678abe64f636e904ab2"
)
GOLDEN_REPAIR_ACTION_PLAN_HASH = (
    "ebde29bb5725f07e118001a4b6c6cd114e04fae217b71cddf3eee12b333b245e"
)
GOLDEN_BINDING_HASH = (
    "e05d548c0218e7d6221688ef1a5e70b40c434adeda08785f1c504d88fcf96fc7"
)


def complete_weibo_plan() -> dict:
    actions = ["liked", "commented", "reposted"]
    plan = {
        "version": 2,
        "platform": "weibo",
        "is_lottery": True,
        "required_actions": actions,
        "action_payloads": {
            "liked": {},
            "commented": {
                "text": "#抽奖# @品牌 参与",
                "topic_tags": ["#抽奖#"],
                "mentions": ["@品牌"],
            },
            "reposted": {
                "text": "转发参与",
                "topic_tags": [],
                "mentions": [],
            },
        },
        "content_requirements": {
            "follow_targets": [],
            "commented": {
                "topic_tags": ["#抽奖#"],
                "mentions": ["@品牌"],
            },
            "reposted": {"topic_tags": [], "mentions": []},
        },
        "source_content_requirements": {
            "follow_targets": [],
            "commented": {
                "topic_tags": ["#抽奖#"],
                "mentions": ["@品牌"],
            },
            "reposted": {"topic_tags": [], "mentions": []},
        },
        "friend_mention_requirements": {},
        "runtime_capability_requirements": (
            get_platform_module("weibo").build_runtime_capability_requirements(
                tuple(actions),
                "weibo_oauth_v1",
            )
        ),
        "execution_path_id": "weibo_oauth_v1",
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
    return plan


def lottery_and_binding():
    plan = complete_weibo_plan()
    canonical_url = "https://weibo.com/123456/AbCdEf12"
    lottery = {
        "id": 73,
        "platform": "weibo",
        "raw_url": canonical_url,
        "canonical_url": canonical_url,
        "status": "pending",
        "execution_lock": None,
        "action_plan": plan,
        "authoritative_rule_snapshot_id": 101,
        "rule_hash": "a" * 64,
        "action_plan_hash": plan["plan_hash"],
    }
    binding = {
        "rule_snapshot_id": 101,
        "rule_hash": "a" * 64,
        "action_plan_hash": plan["plan_hash"],
        "execution_path_id": "weibo_oauth_v1",
        "target_hash": compute_target_hash(canonical_url),
        "config_hash": "b" * 64,
        "execution_revision": 7,
        "required_actions": tuple(plan["required_actions"]),
        "action_plan": plan,
    }
    return lottery, binding


def frozen_intent():
    lottery, plan_binding = lottery_and_binding()
    return build_frozen_execution_intent(
        lottery,
        source_task_id=SOURCE_TASK_ID,
        source_account_id=9,
        plan_binding=plan_binding,
        intent_id=INTENT_ID,
    )


def frozen_row(intent):
    return {
        **intent.__dict__,
        "full_action_plan": json.dumps(
            intent.full_action_plan,
            ensure_ascii=False,
        ),
        "full_required_actions": json.dumps(
            list(intent.full_required_actions),
            ensure_ascii=False,
        ),
    }


class FrozenExecutionIntentTests(unittest.TestCase):
    def test_full_intent_hash_is_stable_and_detects_mutation(self):
        intent = frozen_intent()
        parsed = coerce_frozen_execution_intent(frozen_row(intent))

        self.assertEqual(intent, parsed)
        mutated = frozen_row(intent)
        plan = json.loads(mutated["full_action_plan"])
        plan["action_payloads"]["commented"]["text"] = "changed"
        mutated["full_action_plan"] = json.dumps(plan, ensure_ascii=False)
        with self.assertRaises(ExecutionIntentError) as caught:
            coerce_frozen_execution_intent(mutated)
        self.assertIn(
            caught.exception.code,
            {
                "execution_intent_action_plan_hash_mismatch",
                "execution_intent_payload_binding_mismatch",
            },
        )

    def test_repair_subset_is_deterministic_and_revalidated(self):
        intent = frozen_intent()

        subset = build_repair_execution_subset(
            intent,
            ["commented", "reposted"],
        )

        self.assertEqual(
            subset.requested_actions,
            ("commented", "reposted"),
        )
        self.assertEqual(
            set(subset.action_plan["action_payloads"]),
            {"commented", "reposted"},
        )
        self.assertEqual(
            subset.action_plan["content_requirements"]["follow_targets"],
            [],
        )
        self.assertEqual(
            list(
                subset.action_plan["runtime_capability_requirements"][
                    "actions"
                ]
            ),
            ["commented", "reposted"],
        )
        self.assertEqual(
            validate_action_plan_v2(subset.action_plan).plan_hash,
            subset.action_plan_hash,
        )
        self.assertEqual(
            subset,
            build_repair_execution_subset(
                intent,
                ["commented", "reposted"],
            ),
        )

    def test_repair_subset_rejects_full_unknown_and_reordered_actions(self):
        intent = frozen_intent()
        for actions in (
            ["liked", "commented", "reposted"],
            ["unknown"],
            ["reposted", "commented"],
        ):
            with self.subTest(actions=actions):
                with self.assertRaises(ExecutionIntentError):
                    build_repair_execution_subset(intent, actions)

    def test_binding_separates_full_evidence_hash_from_subset_hash(self):
        intent = frozen_intent()
        subset = build_repair_execution_subset(
            intent,
            ["commented", "reposted"],
        )

        binding = build_task_execution_intent_binding(
            intent,
            task_id=REPAIR_TASK_ID,
            account_id=9,
            binding_kind="repair",
            requested_actions=subset.requested_actions,
            bound_action_plan=subset.action_plan,
            execution_evidence_id=EVIDENCE_ID,
            execution_path_id=intent.execution_path_id,
            target_hash=intent.target_hash,
            config_hash="b" * 64,
            execution_revision=7,
            account_lease_id=LEASE_ID,
            account_lease_generation=3,
        )

        self.assertEqual(
            binding.evidence_action_plan_hash,
            intent.full_action_plan_hash,
        )
        self.assertEqual(
            binding.bound_action_plan_hash,
            subset.action_plan_hash,
        )
        self.assertNotEqual(
            binding.evidence_action_plan_hash,
            binding.bound_action_plan_hash,
        )
        self.assertEqual(
            binding.message_fields()["requested_action_plan_hash"],
            subset.action_plan_hash,
        )
        self.assertEqual(
            binding.execution_evidence_kind,
            "oauth_account_calibration",
        )
        self.assertIsNone(binding.exact_execution_evidence_id)
        self.assertEqual(binding.oauth_calibration_id, EVIDENCE_ID)
        self.assertEqual(
            binding.message_fields()["oauth_calibration_id"],
            EVIDENCE_ID,
        )
        # These literals are a wire-contract canary. A deliberate contract
        # version change must update Core and Worker's golden vectors together;
        # accidental field/order/type drift cannot self-consistently pass.
        self.assertEqual(intent.intent_hash, GOLDEN_INTENT_HASH)
        self.assertEqual(
            subset.requested_actions_hash,
            GOLDEN_REQUESTED_ACTIONS_HASH,
        )
        self.assertEqual(
            subset.action_plan_hash,
            GOLDEN_REPAIR_ACTION_PLAN_HASH,
        )
        self.assertEqual(binding.binding_hash, GOLDEN_BINDING_HASH)

    def test_full_binding_rejects_non_source_account(self):
        intent = frozen_intent()

        with self.assertRaises(ExecutionIntentError) as caught:
            build_task_execution_intent_binding(
                intent,
                task_id=REPAIR_TASK_ID,
                account_id=10,
                binding_kind="full",
                requested_actions=intent.full_required_actions,
                bound_action_plan=intent.full_action_plan,
                execution_evidence_id=EVIDENCE_ID,
                execution_path_id=intent.execution_path_id,
                target_hash=intent.target_hash,
                config_hash="b" * 64,
                execution_revision=7,
                account_lease_id=LEASE_ID,
                account_lease_generation=3,
            )

        self.assertEqual(
            caught.exception.code,
            "execution_intent_full_account_mismatch",
        )


class _IntentDatabase:
    def __init__(
        self,
        *,
        existing=None,
        current_generation=1,
        batch_rows=None,
    ):
        self.existing = (
            {
                **existing,
                "current_generation": current_generation,
            }
            if existing is not None
            else None
        )
        self.batch_rows = list(batch_rows or [])
        self.fetch_all_calls = 0
        self.fetch_all_queries = []
        self.executions = []
        self.affected = 0

    async def fetch_one(self, query, values=None):
        if "FROM lottery_execution_intent_heads AS head" in query:
            return self.existing
        if "ROW_COUNT()" in query:
            return {"affected": self.affected}
        raise AssertionError(query)

    async def fetch_all(self, query, values=None):
        self.fetch_all_calls += 1
        self.fetch_all_queries.append(str(query))
        if "FROM lottery_execution_intent_heads AS head" not in query:
            raise AssertionError(query)
        return self.batch_rows

    async def execute(self, query, values=None):
        statement = str(query)
        data = dict(values or {})
        self.executions.append((statement, data))
        if statement.lstrip().startswith(
            "UPDATE lottery_execution_intent_heads"
        ):
            self.affected = 1
        return 1


class ExecutionIntentPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_dispatch_persists_root_and_task_binding(self):
        lottery, plan_binding = lottery_and_binding()
        database = _IntentDatabase()

        binding = await persist_full_execution_intent(
            database,
            lottery=lottery,
            task_id=SOURCE_TASK_ID,
            account_id=9,
            plan_binding=plan_binding,
            execution_evidence_id=EVIDENCE_ID,
            account_lease_id=LEASE_ID,
            account_lease_generation=1,
        )

        statements = "\n".join(query for query, _ in database.executions)
        self.assertIn("INSERT INTO lottery_execution_intents", statements)
        self.assertIn(
            "INSERT INTO lottery_execution_intent_heads",
            statements,
        )
        self.assertIn(
            "INSERT INTO task_execution_intent_bindings",
            statements,
        )
        self.assertEqual(binding.binding_kind, "full")
        self.assertEqual(
            binding.evidence_action_plan_hash,
            binding.bound_action_plan_hash,
        )
        self.assertEqual(
            binding.execution_evidence_kind,
            "oauth_account_calibration",
        )
        binding_insert = next(
            values
            for query, values in database.executions
            if "INSERT INTO task_execution_intent_bindings" in query
        )
        self.assertEqual(
            binding_insert["oauth_calibration_id"],
            EVIDENCE_ID,
        )
        self.assertIsNone(
            binding_insert["exact_execution_evidence_id"],
        )

    async def test_full_retry_requires_explicit_head_supersede_authority(self):
        lottery, plan_binding = lottery_and_binding()
        intent = frozen_intent()
        database = _IntentDatabase(existing=frozen_row(intent))

        with self.assertRaises(ExecutionIntentError) as caught:
            await persist_full_execution_intent(
                database,
                lottery=lottery,
                task_id=REPAIR_TASK_ID,
                account_id=10,
                plan_binding=plan_binding,
                execution_evidence_id=EVIDENCE_ID,
                account_lease_id=LEASE_ID,
                account_lease_generation=1,
            )

        self.assertEqual(caught.exception.code, "execution_intent_conflict")
        self.assertEqual(database.executions, [])

    async def test_authorized_full_retry_switches_head_and_retains_old_root(self):
        lottery, plan_binding = lottery_and_binding()
        old_intent = frozen_intent()
        database = _IntentDatabase(
            existing=frozen_row(old_intent),
            current_generation=4,
        )

        binding = await persist_full_execution_intent(
            database,
            lottery=lottery,
            task_id=REPAIR_TASK_ID,
            account_id=10,
            plan_binding=plan_binding,
            execution_evidence_id=EVIDENCE_ID,
            account_lease_id=LEASE_ID,
            account_lease_generation=1,
            allow_current_intent_supersede=True,
        )

        self.assertEqual(binding.account_id, 10)
        self.assertNotEqual(binding.intent_id, old_intent.intent_id)
        root_inserts = [
            values
            for query, values in database.executions
            if "INSERT INTO lottery_execution_intents" in query
        ]
        self.assertEqual(len(root_inserts), 1)
        self.assertEqual(root_inserts[0]["source_account_id"], 10)
        self.assertFalse(
            any(
                query.lstrip().startswith(
                    "UPDATE lottery_execution_intents"
                )
                for query, _ in database.executions
            )
        )
        head_update = next(
            values
            for query, values in database.executions
            if query.lstrip().startswith(
                "UPDATE lottery_execution_intent_heads"
            )
        )
        self.assertEqual(head_update["current_intent_id"], old_intent.intent_id)
        self.assertEqual(head_update["current_generation"], 4)
        self.assertEqual(
            head_update["successor_intent_id"],
            binding.intent_id,
        )

    async def test_authorized_same_account_plan_change_advances_head(self):
        lottery, plan_binding = lottery_and_binding()
        old_intent = frozen_intent()
        changed_lottery = copy.deepcopy(lottery)
        changed_plan = copy.deepcopy(changed_lottery["action_plan"])
        changed_plan["action_payloads"]["commented"][
            "text"
        ] = "#抽奖# @品牌 新文案"
        changed_plan["plan_hash"] = compute_action_plan_hash(changed_plan)
        changed_lottery["action_plan"] = changed_plan
        changed_lottery["action_plan_hash"] = changed_plan["plan_hash"]
        changed_binding = {
            **plan_binding,
            "action_plan": changed_plan,
            "action_plan_hash": changed_plan["plan_hash"],
        }
        database = _IntentDatabase(
            existing=frozen_row(old_intent),
            current_generation=7,
        )

        binding = await persist_full_execution_intent(
            database,
            lottery=changed_lottery,
            task_id=REPAIR_TASK_ID,
            account_id=9,
            plan_binding=changed_binding,
            execution_evidence_id=EVIDENCE_ID,
            account_lease_id=LEASE_ID,
            account_lease_generation=1,
            allow_current_intent_supersede=True,
        )

        self.assertEqual(binding.account_id, 9)
        self.assertNotEqual(binding.intent_id, old_intent.intent_id)
        self.assertEqual(
            binding.bound_action_plan_hash,
            changed_plan["plan_hash"],
        )
        head_update = next(
            values
            for query, values in database.executions
            if query.lstrip().startswith(
                "UPDATE lottery_execution_intent_heads"
            )
        )
        self.assertEqual(head_update["current_generation"], 7)
        self.assertEqual(
            head_update["successor_intent_id"],
            binding.intent_id,
        )

    async def test_authorized_account_and_plan_change_advances_head(self):
        lottery, plan_binding = lottery_and_binding()
        old_intent = frozen_intent()
        changed_lottery = copy.deepcopy(lottery)
        changed_plan = copy.deepcopy(changed_lottery["action_plan"])
        changed_plan["action_payloads"]["commented"][
            "text"
        ] = "#抽奖# @品牌 换号后的新文案"
        changed_plan["plan_hash"] = compute_action_plan_hash(changed_plan)
        changed_lottery["action_plan"] = changed_plan
        changed_lottery["action_plan_hash"] = changed_plan["plan_hash"]
        changed_binding = {
            **plan_binding,
            "action_plan": changed_plan,
            "action_plan_hash": changed_plan["plan_hash"],
        }
        database = _IntentDatabase(
            existing=frozen_row(old_intent),
            current_generation=8,
        )

        binding = await persist_full_execution_intent(
            database,
            lottery=changed_lottery,
            task_id=REPAIR_TASK_ID,
            account_id=10,
            plan_binding=changed_binding,
            execution_evidence_id=EVIDENCE_ID,
            account_lease_id=LEASE_ID,
            account_lease_generation=1,
            allow_current_intent_supersede=True,
        )

        self.assertEqual(binding.account_id, 10)
        self.assertNotEqual(binding.intent_id, old_intent.intent_id)
        self.assertEqual(
            binding.bound_action_plan_hash,
            changed_plan["plan_hash"],
        )
        root_insert = next(
            values
            for query, values in database.executions
            if "INSERT INTO lottery_execution_intents" in query
        )
        self.assertEqual(root_insert["source_account_id"], 10)
        self.assertEqual(
            root_insert["full_action_plan_hash"],
            changed_plan["plan_hash"],
        )

    async def test_repair_binding_rejects_non_source_account(self):
        database = _IntentDatabase()

        with self.assertRaises(ExecutionIntentError) as caught:
            await persist_repair_execution_binding(
                database,
                intent=frozen_intent(),
                task_id=REPAIR_TASK_ID,
                account_id=10,
                requested_actions=("commented", "reposted"),
                execution_evidence_id=EVIDENCE_ID,
                config_hash="b" * 64,
                execution_revision=7,
                account_lease_id=LEASE_ID,
                account_lease_generation=1,
            )

        self.assertEqual(
            caught.exception.code,
            "execution_intent_repair_account_mismatch",
        )
        self.assertEqual(database.executions, [])

    async def test_repair_uses_current_head_source_account_not_history(self):
        lottery, plan_binding = lottery_and_binding()
        current = build_frozen_execution_intent(
            lottery,
            source_task_id=REPAIR_TASK_ID,
            source_account_id=10,
            plan_binding=plan_binding,
            intent_id="00000000-0000-4000-8000-000000000006",
        )
        database = _IntentDatabase(
            existing=frozen_row(current),
            current_generation=2,
        )

        loaded = await load_lottery_execution_intent(database, 73)

        self.assertEqual(loaded, current)
        self.assertEqual(loaded.source_account_id, 10)
        with self.assertRaises(ExecutionIntentError) as caught:
            await persist_repair_execution_binding(
                database,
                intent=loaded,
                task_id=SOURCE_TASK_ID,
                account_id=9,
                requested_actions=("commented", "reposted"),
                execution_evidence_id=EVIDENCE_ID,
                config_hash="b" * 64,
                execution_revision=7,
                account_lease_id=LEASE_ID,
                account_lease_generation=1,
            )
        self.assertEqual(
            caught.exception.code,
            "execution_intent_repair_account_mismatch",
        )

    async def test_batch_loader_uses_one_query(self):
        intent = frozen_intent()
        database = _IntentDatabase(batch_rows=[frozen_row(intent)])

        result = await load_lottery_execution_intents(
            database,
            [73, 73, 74],
        )

        self.assertEqual(database.fetch_all_calls, 1)
        self.assertEqual(result, {73: intent})
        self.assertIn(
            "JOIN lottery_execution_intents AS root",
            database.fetch_all_queries[0],
        )
        self.assertIn(
            "root.intent_id = head.current_intent_id",
            database.fetch_all_queries[0],
        )

    async def test_batch_loader_contains_corrupt_root_to_its_lottery(self):
        intent = frozen_intent()
        corrupt = frozen_row(intent)
        corrupt["lottery_id"] = 74
        corrupt["platform"] = "bilibili"
        database = _IntentDatabase(
            batch_rows=[frozen_row(intent), corrupt]
        )

        result = await load_lottery_execution_intents(
            database,
            [73, 74],
        )

        self.assertEqual(database.fetch_all_calls, 1)
        self.assertEqual(result[73], intent)
        self.assertIsInstance(result[74], ExecutionIntentLoadFailure)
        self.assertEqual(result[74].lottery_id, 74)
        self.assertEqual(
            result[74].code,
            "execution_intent_payload_binding_mismatch",
        )


if __name__ == "__main__":
    unittest.main()
