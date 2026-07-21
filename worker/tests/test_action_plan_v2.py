import copy
import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.action_plan import (  # noqa: E402
    ActionPlanV2Error,
    compute_action_plan_hash,
    compute_bilibili_api_config_hash,
    validate_action_plan_v2,
)


FOLLOW_HANDLE = "@ASUS华硕官方UP"
TOPIC = "#ASUS翻转夏日#"


def plan_v2(**overrides):
    plan = {
        "version": 2,
        "platform": "bilibili",
        "is_lottery": True,
        "rule_snapshot_id": 101,
        "rule_hash": "a" * 64,
        "execution_path_id": "bilibili_api_v2",
        "required_actions": ["followed", "liked", "commented", "reposted"],
        "action_payloads": {
            "followed": {"target_handle": FOLLOW_HANDLE},
            "liked": {},
            "commented": {
                "text": f"{TOPIC} {FOLLOW_HANDLE} 我家踩稿官：Cat means 猫",
                "topic_tags": [TOPIC],
                "mentions": [FOLLOW_HANDLE],
                "media_refs": [],
                "translation": "Cat means 猫",
            },
            "reposted": {
                "text": f"{TOPIC} 转发参与",
                "topic_tags": [],
                "mentions": [],
            },
        },
        "content_requirements": {
            "follow_targets": [FOLLOW_HANDLE],
            "commented": {"topic_tags": [TOPIC], "mentions": [FOLLOW_HANDLE]},
            "reposted": {"topic_tags": [], "mentions": []},
        },
        "executable": True,
        "review_required": False,
        "confidence": 1.0,
        "source": "operator_complete_attestation",
        "reviewed_by": "operator-1",
        "rule_complete_confirmed": True,
        "unsupported_actions": [
            "topic_tag",
            "mention_account",
            "comment_content",
        ],
        "represented_requirements": [
            "topic_tag",
            "mention_account",
            "comment_content",
        ],
        "unresolved_requirements": [],
        "ambiguity_patterns": [],
        "payload_validation_errors": [],
        "capability_blockers": [],
    }
    plan.update(overrides)
    plan["plan_hash"] = compute_action_plan_hash(plan)
    return plan


class ActionPlanV2Tests(unittest.TestCase):
    def assert_code(self, expected, plan, **kwargs):
        with self.assertRaises(ActionPlanV2Error) as caught:
            validate_action_plan_v2(plan, **kwargs)
        self.assertEqual(caught.exception.code, expected)

    def test_valid_reviewed_plan_has_stable_unicode_hash(self):
        plan = plan_v2()
        shuffled = {key: plan[key] for key in reversed(list(plan))}
        self.assertEqual(compute_action_plan_hash(plan), compute_action_plan_hash(shuffled))
        validated = validate_action_plan_v2(plan, reject_media=True)
        self.assertEqual(validated.plan_hash, plan["plan_hash"])
        self.assertEqual(
            validated.plan_hash,
            "01325b491a3d5bcb71d788dfd64f0e9d0d91f00001f670d92ca8c0d0cfab74dc",
        )
        self.assertEqual(validated.follow_target_handle, FOLLOW_HANDLE)
        self.assertIn(TOPIC, validated.payload_for("commented")["text"])

    def test_legacy_plan_is_fail_closed(self):
        self.assert_code(
            "action_plan_version_unsupported",
            {"required_actions": ["commented"], "review_required": False},
        )

    def test_false_review_flag_without_operator_attestation_is_rejected(self):
        for field, value in (
            ("reviewed_by", None),
            ("reviewed_by", "  "),
            ("reviewed_by", " operator-1"),
            ("reviewed_by", "审" * 43),
            ("rule_complete_confirmed", False),
            ("rule_complete_confirmed", 1),
        ):
            with self.subTest(field=field, value=value):
                plan = plan_v2()
                plan[field] = value
                plan["plan_hash"] = compute_action_plan_hash(plan)
                self.assert_code("action_plan_review_attestation_invalid", plan)

    def test_follow_target_is_required_and_exact(self):
        for payload, code in (
            ({}, "action_payload_followed_target_required"),
            ({"target_handle": "ASUS华硕官方UP"}, "action_payload_followed_target_invalid"),
        ):
            with self.subTest(payload=payload):
                plan = plan_v2()
                plan["action_payloads"]["followed"] = payload
                plan["plan_hash"] = compute_action_plan_hash(plan)
                self.assert_code(code, plan)

        plan = plan_v2()
        plan["action_payloads"]["followed"]["target_handle"] = "@另一个账号"
        plan["plan_hash"] = compute_action_plan_hash(plan)
        self.assert_code("action_plan_follow_target_mismatch", plan)

    def test_action_scoped_topic_and_mention_requirements_must_match_exactly(self):
        plan = plan_v2()
        plan["content_requirements"]["commented"]["topic_tags"] = ["#错误话题#"]
        plan["plan_hash"] = compute_action_plan_hash(plan)
        self.assert_code("action_plan_required_topic_mismatch", plan)

        plan = plan_v2()
        plan["content_requirements"]["reposted"]["mentions"] = [FOLLOW_HANDLE]
        plan["plan_hash"] = compute_action_plan_hash(plan)
        self.assert_code("action_plan_required_mention_mismatch", plan)

    def test_hash_covers_exact_text_metadata_and_requirements(self):
        for mutate in (
            lambda plan: plan["action_payloads"]["commented"].update(text="随机评论"),
            lambda plan: plan["action_payloads"]["commented"]["mentions"].append("@other"),
            lambda plan: plan["content_requirements"]["commented"]["topic_tags"].append("#新增#"),
            lambda plan: plan.update(rule_hash="b" * 64),
        ):
            plan = plan_v2()
            mutate(plan)
            self.assert_code("action_plan_hash_mismatch", plan)

    def test_payload_keys_must_match_required_actions_exactly(self):
        plan = plan_v2()
        plan["action_payloads"].pop("reposted")
        plan["plan_hash"] = compute_action_plan_hash(plan)
        self.assert_code("action_plan_payload_binding_mismatch", plan)

    def test_declared_topic_and_mention_must_be_in_exact_text(self):
        for field, value in (
            ("topic_tags", ["#缺失话题#"]),
            ("mentions", ["@缺失账号"]),
        ):
            with self.subTest(field=field):
                plan = plan_v2()
                plan["action_payloads"]["commented"][field] = value
                plan["content_requirements"]["commented"][field] = value
                plan["plan_hash"] = compute_action_plan_hash(plan)
                self.assert_code("action_payload_required_token_missing", plan)

    def test_nonempty_media_is_explicitly_unsupported_by_current_worker(self):
        plan = plan_v2()
        plan["action_payloads"]["commented"]["media_refs"] = ["evidence:photo-1"]
        plan["plan_hash"] = compute_action_plan_hash(plan)
        validate_action_plan_v2(plan)
        self.assert_code("action_payload_media_unsupported", plan, reject_media=True)

    def test_required_actions_are_unique_and_canonical_ordered(self):
        for actions, expected in (
            (["liked", "followed"], "action_plan_action_order_invalid"),
            (["followed", "followed"], "action_plan_required_actions_invalid"),
            (["followed", "unknown"], "action_plan_required_actions_invalid"),
        ):
            with self.subTest(actions=actions):
                plan = plan_v2()
                plan["required_actions"] = actions
                plan["action_payloads"] = {
                    action: copy.deepcopy(plan["action_payloads"].get(action, {}))
                    for action in set(actions)
                }
                plan["plan_hash"] = compute_action_plan_hash(plan)
                self.assert_code(expected, plan)

    def test_config_hash_is_execution_revision_scoped(self):
        self.assertEqual(
            compute_bilibili_api_config_hash(7),
            "7ea85ddf973664b5825f8a065c5866706176969209e4800060f2f390d5e67fc0",
        )
        self.assertNotEqual(
            compute_bilibili_api_config_hash(1),
            compute_bilibili_api_config_hash(2),
        )
        with self.assertRaises(ActionPlanV2Error):
            compute_bilibili_api_config_hash(0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
