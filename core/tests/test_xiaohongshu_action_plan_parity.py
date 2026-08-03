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


def manual_plan(
    actions=("followed", "liked", "commented", "favorited"),
):
    payloads = {
        "followed": {"target_handle": "@抽奖博主"},
        "liked": {},
        "commented": {"text": "认真阅读后参与抽奖"},
        "favorited": {},
    }
    plan = {
        "version": 2,
        "platform": "xiaohongshu",
        "rule_snapshot_id": 71,
        "rule_hash": "a" * 64,
        "execution_path_id": "xiaohongshu_manual_v1",
        "required_actions": list(actions),
        "action_payloads": {
            action: payloads[action] for action in actions
        },
        "content_requirements": {
            "follow_targets": (
                ["@抽奖博主"] if "followed" in actions else []
            ),
            "commented": {"topic_tags": [], "mentions": []},
            "reposted": {"topic_tags": [], "mentions": []},
        },
        "executable": False,
        "review_required": False,
        "reviewed_by": "operator-1",
        "rule_complete_confirmed": True,
        "capability_blockers": ["xiaohongshu_manual_execution_selected"],
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

    def test_both_contracts_accept_canonical_nonempty_manual_subsets(self):
        for actions in (
            ("liked",),
            ("liked", "commented", "favorited"),
            ("followed", "liked", "commented", "favorited"),
        ):
            plan = manual_plan(actions)
            for contract in (core_contract, worker_contract):
                with self.subTest(
                    actions=actions,
                    contract=contract.__name__,
                ):
                    validated = contract.validate_action_plan_v2(
                        copy.deepcopy(plan),
                        require_executable=False,
                    )
                    self.assertEqual(actions, tuple(validated.required_actions))

    def test_capability_path_action_and_legacy_bucket_tampering_fail_closed(self):
        mutations = []

        executable = manual_plan()
        executable["executable"] = True
        mutations.append(executable)

        wrong_path = manual_plan()
        wrong_path["execution_path_id"] = "selector_flow"
        mutations.append(wrong_path)

        legacy_repost = manual_plan()
        legacy_repost["content_requirements"]["reposted"]["mentions"] = ["@好友"]
        mutations.append(legacy_repost)

        empty = manual_plan(())
        mutations.append(empty)

        wrong_order = manual_plan(("liked", "commented"))
        wrong_order["required_actions"] = ["commented", "liked"]
        mutations.append(wrong_order)

        reposted = manual_plan(("liked",))
        reposted["required_actions"].append("reposted")
        reposted["action_payloads"]["reposted"] = {"text": "转发参与"}
        mutations.append(reposted)

        for plan in mutations:
            plan["plan_hash"] = core_contract.compute_action_plan_hash(plan)
            self.assert_rejected_by_both(plan)


if __name__ == "__main__":
    unittest.main()
