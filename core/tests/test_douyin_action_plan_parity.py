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
    module_name = "dpms_worker_douyin_action_plan_contract"
    spec = importlib.util.spec_from_file_location(module_name, worker_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("worker action-plan contract cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


worker_contract = load_worker_contract()


def manual_plan(*, action="favorited"):
    payload = {} if action == "favorited" else {"text": "转发参与"}
    plan = {
        "version": 2,
        "platform": "douyin",
        "rule_snapshot_id": 81,
        "rule_hash": "d" * 64,
        "execution_path_id": "douyin_manual_v1",
        "required_actions": ["followed", "liked", "commented", action],
        "action_payloads": {
            "followed": {"target_handle": "@抽奖博主"},
            "liked": {},
            "commented": {"text": "认真阅读后参与抽奖"},
            action: payload,
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
        "capability_blockers": ["douyin_no_official_interaction_api"],
    }
    plan["plan_hash"] = core_contract.compute_action_plan_hash(plan)
    return plan


class DouyinActionPlanParityTests(unittest.TestCase):
    def validate(self, contract, plan):
        return contract.validate_action_plan_v2(
            copy.deepcopy(plan),
            require_executable=False,
        )

    def rejection_code(self, contract, plan):
        with self.assertRaises(contract.ActionPlanV2Error) as raised:
            self.validate(contract, plan)
        return raised.exception.code

    def test_constants_hash_and_variable_actions_are_identical(self):
        self.assertEqual(
            core_contract.DOUYIN_MANUAL_EXECUTION_PATH,
            worker_contract.DOUYIN_MANUAL_EXECUTION_PATH,
        )
        self.assertEqual(
            core_contract.DOUYIN_DEVICE_EXECUTION_PATH,
            worker_contract.DOUYIN_DEVICE_EXECUTION_PATH,
        )
        self.assertEqual(
            core_contract.DOUYIN_NO_OFFICIAL_API_BLOCKER,
            worker_contract.DOUYIN_NO_OFFICIAL_API_BLOCKER,
        )
        self.assertEqual(
            tuple(core_contract.DOUYIN_ACTION_ORDER),
            tuple(worker_contract.DOUYIN_ACTION_ORDER),
        )
        plan = manual_plan(action="favorited")
        self.assertEqual(
            core_contract.compute_action_plan_hash(plan),
            worker_contract.compute_action_plan_hash(plan),
        )
        for contract in (core_contract, worker_contract):
            with self.subTest(contract=contract.__name__):
                validated = self.validate(contract, plan)
                self.assertIn("favorited", validated.required_actions)

    def test_executable_and_path_tampering_use_the_same_fail_closed_codes(self):
        executable = manual_plan()
        executable["executable"] = True
        executable["plan_hash"] = core_contract.compute_action_plan_hash(executable)

        wrong_path = manual_plan()
        wrong_path["execution_path_id"] = "bilibili_api_v2"
        wrong_path["plan_hash"] = core_contract.compute_action_plan_hash(wrong_path)

        for plan, expected in (
            (executable, "douyin_manual_plan_must_be_non_executable"),
            (wrong_path, "douyin_execution_path_invalid"),
        ):
            for contract in (core_contract, worker_contract):
                with self.subTest(expected=expected, contract=contract.__name__):
                    self.assertEqual(expected, self.rejection_code(contract, plan))

    def test_repost_is_rejected_in_both_contracts(self):
        plan = manual_plan(action="reposted")
        plan["action_payloads"]["reposted"] = {}
        plan["plan_hash"] = core_contract.compute_action_plan_hash(plan)

        for contract in (core_contract, worker_contract):
            with self.subTest(contract=contract.__name__):
                self.assertEqual(
                    "action_plan_required_actions_invalid",
                    self.rejection_code(contract, plan),
                )


if __name__ == "__main__":
    unittest.main()
