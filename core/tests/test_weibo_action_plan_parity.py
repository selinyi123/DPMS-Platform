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
    module_name = "dpms_worker_weibo_action_plan_contract"
    spec = importlib.util.spec_from_file_location(module_name, worker_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("worker action-plan contract cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


worker_contract = load_worker_contract()


def oauth_plan(*, friend_handles=("@好友甲", "@好友乙", "@好友丙")):
    source_mentions = ["@官方客服"]
    bound_mentions = [*source_mentions, "@品牌官方", *friend_handles]
    actions = list(core_contract.WEIBO_ACTION_ORDER)
    plan = {
        "version": 2,
        "platform": "weibo",
        "required_actions": actions,
        "action_payloads": {
            "followed": {"target_handle": "@品牌官方"},
            "liked": {},
            "commented": {
                "text": "参与微博抽奖 " + " ".join(bound_mentions),
                "topic_tags": [],
                "mentions": bound_mentions,
            },
            "favorited": {},
            "reposted": {},
        },
        "source_content_requirements": {
            "follow_targets": ["@品牌官方"],
            "commented": {"topic_tags": [], "mentions": source_mentions},
            "reposted": {"topic_tags": [], "mentions": []},
        },
        "content_requirements": {
            "follow_targets": ["@品牌官方"],
            "commented": {"topic_tags": [], "mentions": bound_mentions},
            "reposted": {"topic_tags": [], "mentions": []},
        },
        "friend_mention_requirements": {
            "commented": {"mode": "exact", "count": 3}
        },
        "runtime_capability_requirements": (
            core_contract.weibo_runtime_capability_requirements(actions)
        ),
        "execution_path_id": core_contract.WEIBO_OAUTH_EXECUTION_PATH,
        "rule_snapshot_id": 91,
        "rule_hash": "b" * 64,
        "review_required": False,
        "executable": True,
        "reviewed_by": "admin-1",
        "rule_complete_confirmed": True,
        "capability_blockers": [],
    }
    plan["plan_hash"] = core_contract.compute_action_plan_hash(plan)
    return plan


class WeiboActionPlanParityTests(unittest.TestCase):
    def validate(self, contract, plan):
        return contract.validate_action_plan_v2(copy.deepcopy(plan))

    def rejection_code(self, contract, plan):
        with self.assertRaises(contract.ActionPlanV2Error) as raised:
            self.validate(contract, plan)
        return raised.exception.code

    def test_oauth_contract_hash_and_actions_are_identical(self):
        plan = oauth_plan()
        self.assertEqual(
            core_contract.WEIBO_OAUTH_EXECUTION_PATH,
            worker_contract.WEIBO_OAUTH_EXECUTION_PATH,
        )
        self.assertEqual(
            core_contract.weibo_runtime_capability_requirements(
                core_contract.WEIBO_ACTION_ORDER
            ),
            worker_contract.weibo_runtime_capability_requirements(
                worker_contract.WEIBO_ACTION_ORDER
            ),
        )
        self.assertEqual(
            core_contract.compute_action_plan_hash(plan),
            worker_contract.compute_action_plan_hash(plan),
        )
        for contract in (core_contract, worker_contract):
            with self.subTest(contract=contract.__name__):
                self.assertEqual(
                    tuple(contract.WEIBO_ACTION_ORDER),
                    tuple(self.validate(contract, plan).required_actions),
                )

    def test_normalized_duplicate_friends_fail_with_same_code(self):
        plan = oauth_plan(friend_handles=("@Alice", "@alice", "@好友丙"))
        for contract in (core_contract, worker_contract):
            with self.subTest(contract=contract.__name__):
                self.assertEqual(
                    "action_plan_friend_mention_requirement_binding_mismatch",
                    self.rejection_code(contract, plan),
                )

    def test_non_handle_mentions_fail_with_same_code(self):
        plan = oauth_plan(friend_handles=("好友甲", "@好友乙", "@好友丙"))
        for contract in (core_contract, worker_contract):
            with self.subTest(contract=contract.__name__):
                self.assertEqual(
                    "action_payload_mentions_invalid",
                    self.rejection_code(contract, plan),
                )

    def test_lone_surrogates_fail_with_same_precise_field_codes(self):
        text_plan = oauth_plan()
        text_plan["action_payloads"]["commented"]["text"] = "\ud800"
        mention_plan = oauth_plan()
        mention_plan["action_payloads"]["commented"]["mentions"][0] = (
            "@\ud800"
        )
        for contract in (core_contract, worker_contract):
            with self.subTest(contract=contract.__name__, field="text"):
                self.assertEqual(
                    "action_payload_commented_text_invalid",
                    self.rejection_code(contract, text_plan),
                )
            with self.subTest(contract=contract.__name__, field="mentions"):
                self.assertEqual(
                    "action_payload_mentions_invalid",
                    self.rejection_code(contract, mention_plan),
                )

    def test_mention_prefix_does_not_satisfy_required_handle(self):
        for contract in (core_contract, worker_contract):
            with self.subTest(contract=contract.__name__):
                with self.assertRaises(contract.ActionPlanV2Error) as raised:
                    contract.validate_action_payload(
                        "commented",
                        {"text": "hello @alice2", "mentions": ["@alice"]},
                        platform="weibo",
                    )
                self.assertEqual(
                    raised.exception.code,
                    "action_payload_required_token_missing",
                )


if __name__ == "__main__":
    unittest.main()
