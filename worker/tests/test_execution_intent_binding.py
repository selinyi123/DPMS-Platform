import copy
import json
import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.action_plan import (  # noqa: E402
    canonical_json_bytes,
    compute_action_plan_hash,
    compute_rule_hash,
    compute_target_hash,
    sha256_hex,
    validate_action_plan_v2,
)
from app.execution_intents import (  # noqa: E402
    ExecutionIntentValidationError,
    _derive_subset_plan,
    validate_task_execution_intent,
)
from app.real_run_gate import (  # noqa: E402
    RealRunGateBlocked,
    _TASK_DECISION_QUERY,
    _validate_account_lease,
)


TASK_ID = "11111111-1111-4111-8111-111111111111"
SOURCE_TASK_ID = "22222222-2222-4222-8222-222222222222"
INTENT_ID = "33333333-3333-4333-8333-333333333333"
EVIDENCE_ID = "44444444-4444-4444-8444-444444444444"
LEASE_ID = "55555555-5555-4555-8555-555555555555"
RAW_URL = "https://t.bilibili.com/123456789012"
CANONICAL_URL = "canonical://bilibili/dynamic/123456789012"
RULE_TEXT = "关注 @author，点赞、评论并转发"
RULE_HASH = compute_rule_hash(RULE_TEXT)
CONFIG_HASH = "a" * 64
GOLDEN_INTENT_HASH = (
    "eda6f0c703fcaec3f2134cc7d65a29d6b27971dfc630af2ed85c032e33e55cdf"
)
GOLDEN_REQUESTED_ACTIONS_HASH = (
    "b1949898fec0252e07f8f9a8295a27b65393e5dd10d8cf3361eec9c4d428109f"
)
GOLDEN_REPAIR_ACTION_PLAN_HASH = (
    "b26d2a26205509d8e9d7389c6ccb25a9392c53cbe1af8fa4b8f4fa020ed929be"
)
GOLDEN_BINDING_HASH = (
    "f77fefd1c93bb988de1a59543f49ced7b4b94fbd1bb0de00e254663844dcc20f"
)


class ExecutionIntentQueryContractTests(unittest.TestCase):
    def test_worker_resolves_the_exact_historical_root_from_task_binding(self):
        query = " ".join(_TASK_DECISION_QUERY.split())

        self.assertLess(
            query.index(
                "LEFT JOIN task_execution_intent_bindings intent_binding"
            ),
            query.index(
                "LEFT JOIN lottery_execution_intents intent_root"
            ),
        )
        self.assertIn(
            "intent_root.lottery_id = intent_binding.lottery_id",
            query,
        )
        self.assertIn(
            "intent_root.intent_id = intent_binding.intent_id",
            query,
        )
        self.assertNotIn(
            "intent_root.lottery_id = tr.lottery_id",
            query,
        )


def _plan():
    value = {
        "version": 2,
        "platform": "bilibili",
        "rule_snapshot_id": 17,
        "rule_hash": RULE_HASH,
        "execution_path_id": "bilibili_api_v2",
        "required_actions": [
            "followed",
            "liked",
            "commented",
            "reposted",
        ],
        "action_payloads": {
            "followed": {"target_handle": "@author"},
            "liked": {},
            "commented": {"text": "参加抽奖"},
            "reposted": {"text": "转发抽奖"},
        },
        "content_requirements": {
            "follow_targets": ["@author"],
            "commented": {"topic_tags": [], "mentions": []},
            "reposted": {"topic_tags": [], "mentions": []},
        },
        "executable": True,
        "review_required": False,
        "reviewed_by": "operator",
        "rule_complete_confirmed": True,
    }
    value["plan_hash"] = compute_action_plan_hash(value)
    return validate_action_plan_v2(
        value,
        require_executable=True,
        reject_media=True,
    )


def _hash(value):
    return sha256_hex(canonical_json_bytes(value))


def _contract(kind="repair", requested=("commented", "reposted")):
    full = _plan()
    target_hash = compute_target_hash(CANONICAL_URL)
    full_actions_hash = _hash(list(full.required_actions))
    root_payload = {
        "contract_version": 1,
        "intent_id": INTENT_ID,
        "lottery_id": 73,
        "source_task_id": SOURCE_TASK_ID,
        "source_account_id": 41,
        "platform": "bilibili",
        "raw_url": RAW_URL,
        "canonical_url": CANONICAL_URL,
        "full_action_plan": full.plan,
        "full_action_plan_hash": full.plan_hash,
        "full_required_actions": list(full.required_actions),
        "full_required_actions_hash": full_actions_hash,
        "rule_snapshot_id": full.rule_snapshot_id,
        "rule_hash": full.rule_hash,
        "execution_path_id": full.execution_path_id,
        "target_hash": target_hash,
    }
    intent_hash = _hash(root_payload)
    requested = full.required_actions if kind == "full" else tuple(requested)
    bound = full if kind == "full" else _derive_subset_plan(full, requested)
    requested_hash = _hash(
        {
            "contract_version": 1,
            "execution_intent_id": INTENT_ID,
            "execution_intent_hash": intent_hash,
            "requested_actions": list(requested),
        }
    )
    binding_payload = {
        "contract_version": 1,
        "task_id": TASK_ID,
        "execution_intent_id": INTENT_ID,
        "execution_intent_hash": intent_hash,
        "lottery_id": 73,
        "account_id": 41,
        "binding_kind": kind,
        "requested_actions": list(requested),
        "requested_actions_hash": requested_hash,
        "bound_action_plan": bound.plan,
        "bound_action_plan_hash": bound.plan_hash,
        "evidence_action_plan_hash": full.plan_hash,
        "rule_snapshot_id": full.rule_snapshot_id,
        "rule_hash": full.rule_hash,
        "execution_evidence_id": EVIDENCE_ID,
        "execution_evidence_kind": "exact_execution_evidence",
        "exact_execution_evidence_id": EVIDENCE_ID,
        "oauth_calibration_id": None,
        "execution_path_id": full.execution_path_id,
        "target_hash": target_hash,
        "config_hash": CONFIG_HASH,
        "execution_revision": 5,
        "account_lease_id": LEASE_ID,
        "account_lease_generation": 7,
    }
    binding_hash = _hash(binding_payload)
    task = {
        "task_id": TASK_ID,
        "account_id": "41",
        "lottery_id": "73",
        "platform": "bilibili",
        "raw_url": RAW_URL,
        "canonical_url": CANONICAL_URL,
        "action_plan": json.dumps(full.plan, ensure_ascii=False),
        "action_plan_hash": full.plan_hash,
        "execution_evidence_id": EVIDENCE_ID,
        "execution_evidence_kind": "exact_execution_evidence",
        "exact_execution_evidence_id": EVIDENCE_ID,
        "oauth_calibration_id": "",
        "execution_path_id": full.execution_path_id,
        "target_hash": target_hash,
        "config_hash": CONFIG_HASH,
        "execution_revision": "5",
        "account_lease_id": LEASE_ID,
        "account_lease_generation": "7",
        "execution_intent_id": INTENT_ID,
        "execution_intent_hash": intent_hash,
        "execution_intent_kind": kind,
        "execution_intent_binding_hash": binding_hash,
        "requested_actions": json.dumps(
            list(requested),
            ensure_ascii=False,
        ),
        "requested_actions_hash": requested_hash,
        "requested_action_plan_hash": bound.plan_hash,
    }
    row = {
        "root_contract_version": 1,
        "root_intent_id": INTENT_ID,
        "root_intent_hash": intent_hash,
        "root_lottery_id": 73,
        "root_source_task_id": SOURCE_TASK_ID,
        "root_source_account_id": 41,
        "root_platform": "bilibili",
        "root_raw_url": RAW_URL,
        "root_canonical_url": CANONICAL_URL,
        "root_full_action_plan": json.dumps(full.plan, ensure_ascii=False),
        "root_full_action_plan_hash": full.plan_hash,
        "root_full_required_actions": json.dumps(
            list(full.required_actions),
            ensure_ascii=False,
        ),
        "root_full_required_actions_hash": full_actions_hash,
        "root_rule_snapshot_id": full.rule_snapshot_id,
        "root_rule_hash": full.rule_hash,
        "root_execution_path_id": full.execution_path_id,
        "root_target_hash": target_hash,
        "binding_contract_version": 1,
        "binding_task_id": TASK_ID,
        "binding_intent_id": INTENT_ID,
        "binding_lottery_id": 73,
        "binding_account_id": 41,
        "binding_kind": kind,
        "binding_requested_actions": json.dumps(
            list(requested),
            ensure_ascii=False,
        ),
        "binding_requested_actions_hash": requested_hash,
        "binding_action_plan": json.dumps(bound.plan, ensure_ascii=False),
        "binding_action_plan_hash": bound.plan_hash,
        "binding_evidence_action_plan_hash": full.plan_hash,
        "binding_rule_snapshot_id": full.rule_snapshot_id,
        "binding_rule_hash": full.rule_hash,
        "binding_execution_evidence_id": EVIDENCE_ID,
        "binding_execution_evidence_kind": "exact_execution_evidence",
        "binding_exact_execution_evidence_id": EVIDENCE_ID,
        "binding_oauth_calibration_id": None,
        "binding_execution_path_id": full.execution_path_id,
        "binding_target_hash": target_hash,
        "binding_config_hash": CONFIG_HASH,
        "binding_execution_revision": 5,
        "binding_account_lease_id": LEASE_ID,
        "binding_account_lease_generation": 7,
        "binding_hash": binding_hash,
        "task_execution_evidence_id": EVIDENCE_ID,
        "task_execution_path_id": full.execution_path_id,
        "task_target_hash": target_hash,
        "task_config_hash": CONFIG_HASH,
        "account_execution_revision": 5,
        "task_account_lease_id": LEASE_ID,
        "task_account_lease_generation": 7,
    }
    return task, row, full, bound


def _legacy_contract(full):
    task = {
        "task_id": TASK_ID,
        "account_id": "41",
        "lottery_id": "73",
        "platform": "bilibili",
        "mode": "real_run",
        "action_plan": json.dumps(full.plan, ensure_ascii=False),
        "action_plan_hash": full.plan_hash,
        "execution_intent_id": "",
        "requested_actions": "[]",
        "legacy_source_stream": "lottery_tasks",
        "legacy_source_message_id": "1700000000000-0",
    }
    payload = {
        key: value
        for key, value in task.items()
        if not key.startswith("legacy_source_")
    }
    return task, {
        "legacy_outbox_stream_key": "lottery_tasks",
        "legacy_outbox_status": "sent",
        "legacy_outbox_dedup_key": TASK_ID,
        "legacy_outbox_payload": json.dumps(payload, ensure_ascii=False),
    }


class ExecutionIntentBindingTests(unittest.TestCase):
    def validate(self, task, row, full):
        return validate_task_execution_intent(
            task,
            row,
            task_id=TASK_ID,
            account_id=41,
            lottery_id=73,
            platform="bilibili",
            full_plan=full,
            expected_evidence_kind="exact_execution_evidence",
        )

    def test_repair_executes_only_hash_bound_strict_subset(self):
        task, row, full, bound = _contract()
        result = self.validate(task, row, full)
        self.assertEqual(result.binding_kind, "repair")
        self.assertEqual(result.requested_actions, ("commented", "reposted"))
        self.assertEqual(result.action_plan.plan_hash, bound.plan_hash)
        self.assertEqual(result.action_plan.required_actions, result.requested_actions)
        self.assertEqual(row["root_intent_hash"], GOLDEN_INTENT_HASH)
        self.assertEqual(
            row["binding_requested_actions_hash"],
            GOLDEN_REQUESTED_ACTIONS_HASH,
        )
        self.assertEqual(
            row["binding_action_plan_hash"],
            GOLDEN_REPAIR_ACTION_PLAN_HASH,
        )
        self.assertEqual(row["binding_hash"], GOLDEN_BINDING_HASH)

    def assert_rehashed_cross_account_rejected(self, *, kind, code):
        task, row, full, _bound = _contract(kind=kind)
        row["binding_account_id"] = 42
        task["account_id"] = "42"
        binding_payload = {
            "contract_version": 1,
            "task_id": TASK_ID,
            "execution_intent_id": INTENT_ID,
            "execution_intent_hash": row["root_intent_hash"],
            "lottery_id": 73,
            "account_id": 42,
            "binding_kind": kind,
            "requested_actions": json.loads(
                row["binding_requested_actions"]
            ),
            "requested_actions_hash": row[
                "binding_requested_actions_hash"
            ],
            "bound_action_plan": json.loads(
                row["binding_action_plan"]
            ),
            "bound_action_plan_hash": row[
                "binding_action_plan_hash"
            ],
            "evidence_action_plan_hash": row[
                "binding_evidence_action_plan_hash"
            ],
            "rule_snapshot_id": row["binding_rule_snapshot_id"],
            "rule_hash": row["binding_rule_hash"],
            "execution_evidence_id": EVIDENCE_ID,
            "execution_evidence_kind": "exact_execution_evidence",
            "exact_execution_evidence_id": EVIDENCE_ID,
            "oauth_calibration_id": None,
            "execution_path_id": row["binding_execution_path_id"],
            "target_hash": row["binding_target_hash"],
            "config_hash": CONFIG_HASH,
            "execution_revision": 5,
            "account_lease_id": LEASE_ID,
            "account_lease_generation": 7,
        }
        row["binding_hash"] = _hash(binding_payload)
        task["execution_intent_binding_hash"] = row["binding_hash"]

        with self.assertRaises(ExecutionIntentValidationError) as caught:
            validate_task_execution_intent(
                task,
                row,
                task_id=TASK_ID,
                account_id=42,
                lottery_id=73,
                platform="bilibili",
                full_plan=full,
                expected_evidence_kind="exact_execution_evidence",
            )

        self.assertEqual(
            caught.exception.code,
            code,
        )

    def test_repair_rejects_rehashed_binding_for_another_account(self):
        self.assert_rehashed_cross_account_rejected(
            kind="repair",
            code="execution_intent_repair_account_mismatch",
        )

    def test_full_rejects_rehashed_binding_for_another_account(self):
        self.assert_rehashed_cross_account_rejected(
            kind="full",
            code="execution_intent_full_account_mismatch",
        )

    def test_validated_repair_binding_requires_repair_run_lease(self):
        task, row, full, _bound = _contract()
        result = self.validate(task, row, full)
        row.update(
            {
                "lease_account_id": 41,
                "lease_id": LEASE_ID,
                "lease_generation": 7,
                "lease_operation_kind": "repair_run",
                "lease_owner_id": TASK_ID,
                "lease_task_id": TASK_ID,
                "lease_active": 1,
                "lease_unreleased": 1,
                "lease_latest_generation": 1,
                "active_account_lease_count": 1,
                "task_reconciliation_required": 0,
            }
        )

        lease = _validate_account_lease(
            task,
            row,
            task_id=TASK_ID,
            account_id=41,
            execution_intent_kind=result.binding_kind,
        )
        self.assertEqual(lease, (LEASE_ID, 7))

        row["lease_operation_kind"] = "real_run"
        with self.assertRaises(RealRunGateBlocked) as caught:
            _validate_account_lease(
                task,
                row,
                task_id=TASK_ID,
                account_id=41,
                execution_intent_kind=result.binding_kind,
            )
        self.assertEqual(
            caught.exception.code,
            "account_lease_binding_invalid",
        )

    def test_full_binding_preserves_complete_plan(self):
        task, row, full, _ = _contract(kind="full")
        result = self.validate(task, row, full)
        self.assertEqual(result.binding_kind, "full")
        self.assertEqual(result.requested_actions, full.required_actions)
        self.assertEqual(result.action_plan.plan_hash, full.plan_hash)

    def test_message_subset_tampering_is_rejected(self):
        task, row, full, _ = _contract()
        task["requested_actions"] = '["liked"]'
        with self.assertRaises(ExecutionIntentValidationError) as caught:
            self.validate(task, row, full)
        self.assertEqual(
            caught.exception.code,
            "execution_intent_message_binding_mismatch",
        )

    def test_database_binding_hash_tampering_is_rejected(self):
        task, row, full, _ = _contract()
        row["binding_hash"] = "f" * 64
        task["execution_intent_binding_hash"] = "f" * 64
        with self.assertRaises(ExecutionIntentValidationError) as caught:
            self.validate(task, row, full)
        self.assertEqual(
            caught.exception.code,
            "execution_intent_binding_hash_mismatch",
        )

    def test_root_without_task_binding_fails_closed(self):
        task, row, full, _ = _contract()
        for key in list(row):
            if key.startswith("binding_"):
                row[key] = None
        with self.assertRaises(ExecutionIntentValidationError) as caught:
            self.validate(task, row, full)
        self.assertEqual(caught.exception.code, "execution_intent_binding_missing")

    def test_pre_contract_task_remains_full_only(self):
        _, _, full, _ = _contract()
        task, row = _legacy_contract(full)
        result = self.validate(task, row, full)
        self.assertTrue(result.legacy)
        self.assertEqual(result.binding_kind, "legacy_full")
        self.assertEqual(result.requested_actions, full.required_actions)

    def test_new_full_task_cannot_use_contract_absence_as_legacy_authority(self):
        _, _, full, _ = _contract()
        task, _row = _legacy_contract(full)
        task.pop("legacy_source_stream")
        task.pop("legacy_source_message_id")
        with self.assertRaises(ExecutionIntentValidationError) as caught:
            self.validate(task, {}, full)
        self.assertEqual(
            caught.exception.code,
            "execution_intent_legacy_authority_missing",
        )

    def test_forged_legacy_provenance_without_outbox_authority_is_rejected(self):
        _, _, full, _ = _contract()
        task, _row = _legacy_contract(full)
        with self.assertRaises(ExecutionIntentValidationError) as caught:
            self.validate(task, {}, full)
        self.assertEqual(
            caught.exception.code,
            "execution_intent_legacy_authority_missing",
        )

    def test_legacy_outbox_plan_mismatch_is_rejected(self):
        _, _, full, _ = _contract()
        task, row = _legacy_contract(full)
        payload = json.loads(row["legacy_outbox_payload"])
        payload_plan = json.loads(payload["action_plan"])
        payload_plan["required_actions"] = ["liked"]
        payload["action_plan"] = json.dumps(payload_plan, ensure_ascii=False)
        row["legacy_outbox_payload"] = json.dumps(payload, ensure_ascii=False)
        with self.assertRaises(ExecutionIntentValidationError) as caught:
            self.validate(task, row, full)
        self.assertEqual(
            caught.exception.code,
            "execution_intent_legacy_outbox_mismatch",
        )

    def test_legacy_task_cannot_claim_a_repair_subset(self):
        _, _, full, _ = _contract()
        task = {"requested_actions": '["commented"]'}
        with self.assertRaises(ExecutionIntentValidationError) as caught:
            self.validate(task, {}, full)
        self.assertEqual(caught.exception.code, "execution_intent_root_missing")


if __name__ == "__main__":
    unittest.main()
