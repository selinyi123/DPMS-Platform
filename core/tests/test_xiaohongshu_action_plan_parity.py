import copy
import importlib.util
import sys
import unittest
from pathlib import Path

from app import action_plan as core_contract


def load_worker_contract():
    candidates = (
        Path(__file__).resolve().parents[2] / "worker" / "app" / "action_plan.py",
        Path("/worker/app/action_plan.py"),
    )
    worker_path = next((path for path in candidates if path.is_file()), None)
    if worker_path is None:
        raise RuntimeError("worker action-plan contract is unavailable")
    module_name = "dpms_worker_xiaohongshu_action_plan_contract"
    spec = importlib.util.spec_from_file_location(module_name, worker_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("worker action-plan contract cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


worker_contract = load_worker_contract()


def manual_plan():
    plan = {
        "version": 2,
        "platform": "xiaohongshu",
        "rule_snapshot_id": 71,
        "rule_hash": "a" * 64,
        "execution_path_id": "xiaohongshu_manual_v1",
        "required_actions": ["followed", "liked", "commented", "favorited"],
        "action_payloads": {
            "followed": {"target_handle": "@抽奖博主"},
            "liked": {},
            "commented": {"text": "认真阅读后参与抽奖"},
            "favorited": {},
        },
        "content_requirements": {
            "follow_targets": ["@抽奖博主"],
            "commented": {"topic_tags": [], "mentions": []},
            "reposted": {"topic_tags": [], "mentions": []},
        },
        "executable": False,
        "review_required": False,
        "reviewed_by": "operator-1",
        "rule_complete_confirmed": True,
        "capability_blockers": ["xiaohongshu_no_official_interaction_api"],
    }
    plan["plan_hash"] = core_contract.compute_action_plan_hash(plan)
    return plan


class XiaohongshuActionPlanParityTests(unittest.TestCase):
    def assert_rejected_by_both(self, plan):
        for contract in (core_contract, worker_contract):
            with self.subTest(contract=contract.__name__):
                with self.assertRaises(contract.ActionPlanV2Error):
                    contract.validate_action_plan_v2(
                        copy.deepcopy(plan),
                        require_executable=False,
                    )

    def test_canonical_constants_and_hash_are_identical(self):
        plan = manual_plan()
        self.assertEqual(
            core_contract.XIAOHONGSHU_MANUAL_EXECUTION_PATH,
            worker_contract.XIAOHONGSHU_MANUAL_EXECUTION_PATH,
        )
        self.assertEqual(
            tuple(core_contract.XIAOHONGSHU_ACTION_ORDER),
            tuple(worker_contract.XIAOHONGSHU_REQUIRED_ACTIONS),
        )
        self.assertEqual(
            core_contract.compute_action_plan_hash(plan),
            worker_contract.compute_action_plan_hash(plan),
        )

    def test_both_contracts_accept_only_the_manual_four_action_envelope(self):
        plan = manual_plan()
        for contract in (core_contract, worker_contract):
            with self.subTest(contract=contract.__name__):
                validated = contract.validate_action_plan_v2(
                    copy.deepcopy(plan),
                    require_executable=False,
                )
                self.assertEqual(
                    ("followed", "liked", "commented", "favorited"),
                    tuple(validated.required_actions),
                )

    def test_capability_path_action_and_legacy_bucket_tampering_fail_closed(self):
        mutations = []

        executable = manual_plan()
        executable["executable"] = True
        mutations.append(executable)

        wrong_path = manual_plan()
        wrong_path["execution_path_id"] = "selector_flow"
        mutations.append(wrong_path)

        missing_favorite = manual_plan()
        missing_favorite["required_actions"].remove("favorited")
        missing_favorite["action_payloads"].pop("favorited")
        mutations.append(missing_favorite)

        legacy_repost = manual_plan()
        legacy_repost["content_requirements"]["reposted"]["mentions"] = ["@好友"]
        mutations.append(legacy_repost)

        for plan in mutations:
            plan["plan_hash"] = core_contract.compute_action_plan_hash(plan)
            self.assert_rejected_by_both(plan)


if __name__ == "__main__":
    unittest.main()
