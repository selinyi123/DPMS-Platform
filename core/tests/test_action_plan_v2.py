import copy
import unittest

from app.action_plan import (
    ACTION_ORDER,
    XIAOHONGSHU_ACTION_ORDER,
    XIAOHONGSHU_MANUAL_EXECUTION_PATH,
    XIAOHONGSHU_NO_OFFICIAL_API_BLOCKER,
    ActionPlanV2Error,
    compute_action_plan_hash,
    compute_bilibili_api_config_hash,
    validate_action_plan_v2,
)


def complete_plan() -> dict:
    plan = {
        "version": 2,
        "platform": "bilibili",
        "is_lottery": True,
        "required_actions": ["followed", "liked", "commented", "reposted"],
        "action_payloads": {
            "followed": {"target_handle": "@ASUS华硕官方UP"},
            "liked": {},
            "commented": {
                "text": "#ASUS翻转夏日# @ASUS华硕官方UP 我家踩稿官：Cat means 猫",
                "topic_tags": ["#ASUS翻转夏日#"],
                "mentions": ["@ASUS华硕官方UP"],
                "media_refs": [],
                "translation": "Cat means 猫",
            },
            "reposted": {
                "text": "#ASUS翻转夏日# 转发参与",
                "topic_tags": [],
                "mentions": [],
            },
        },
        "content_requirements": {
            "follow_targets": ["@ASUS华硕官方UP"],
            "commented": {
                "topic_tags": ["#ASUS翻转夏日#"],
                "mentions": ["@ASUS华硕官方UP"],
            },
            "reposted": {"topic_tags": [], "mentions": []},
        },
        "execution_path_id": "bilibili_api_v2",
        "rule_snapshot_id": 101,
        "rule_hash": "a" * 64,
        "review_required": False,
        "executable": True,
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
    plan["plan_hash"] = compute_action_plan_hash(plan)
    return plan


def complete_xiaohongshu_plan() -> dict:
    plan = {
        "version": 2,
        "platform": "xiaohongshu",
        "is_lottery": True,
        "required_actions": list(XIAOHONGSHU_ACTION_ORDER),
        "action_payloads": {
            "followed": {"target_handle": "@小红书博主"},
            "liked": {},
            "commented": {"text": "已认真阅读，参与抽奖"},
            "favorited": {},
        },
        # Keep the v2 compatibility shape: repost requirements remain present
        # and empty even though repost is not one of the XHS four actions.
        "content_requirements": {
            "follow_targets": ["@小红书博主"],
            "commented": {"topic_tags": [], "mentions": []},
            "reposted": {"topic_tags": [], "mentions": []},
        },
        "execution_path_id": XIAOHONGSHU_MANUAL_EXECUTION_PATH,
        "rule_snapshot_id": 202,
        "rule_hash": "b" * 64,
        "review_required": False,
        "executable": False,
        "confidence": 1.0,
        "source": "operator_complete_attestation",
        "reviewed_by": "operator-1",
        "rule_complete_confirmed": True,
        "unsupported_actions": [],
        "represented_requirements": [],
        "unresolved_requirements": [],
        "ambiguity_patterns": [],
        "payload_validation_errors": [],
        "capability_blockers": [XIAOHONGSHU_NO_OFFICIAL_API_BLOCKER],
    }
    plan["plan_hash"] = compute_action_plan_hash(plan)
    return plan


class ActionPlanV2Tests(unittest.TestCase):
    def test_global_order_adds_favorite_without_reordering_existing_actions(self):
        self.assertEqual(
            ("followed", "liked", "commented", "favorited", "reposted"),
            ACTION_ORDER,
        )

    def test_complete_exact_plan_is_valid(self):
        validated = validate_action_plan_v2(complete_plan(), reject_media=True)

        self.assertEqual("@ASUS华硕官方UP", validated.follow_target_handle)
        self.assertEqual(
            ["#ASUS翻转夏日#"],
            validated.content_requirements["commented"]["topic_tags"],
        )
        self.assertEqual(
            "01325b491a3d5bcb71d788dfd64f0e9d0d91f00001f670d92ca8c0d0cfab74dc",
            validated.plan_hash,
        )

    def test_false_review_flag_without_operator_attestation_is_rejected(self):
        for field, value in (
            ("reviewed_by", None),
            ("reviewed_by", "  "),
            ("rule_complete_confirmed", False),
        ):
            with self.subTest(field=field, value=value):
                plan = complete_plan()
                plan[field] = value
                plan["plan_hash"] = compute_action_plan_hash(plan)
                with self.assertRaisesRegex(
                    ActionPlanV2Error, "action_plan_review_attestation_invalid"
                ):
                    validate_action_plan_v2(plan)

    def test_follow_payload_requires_exact_reviewed_target(self):
        plan = complete_plan()
        plan["action_payloads"]["followed"]["target_handle"] = "@另一账号"
        plan["plan_hash"] = compute_action_plan_hash(plan)

        with self.assertRaisesRegex(
            ActionPlanV2Error, "action_plan_follow_target_mismatch"
        ):
            validate_action_plan_v2(plan)

    def test_wrong_comment_topic_cannot_satisfy_source_requirement(self):
        plan = complete_plan()
        plan["action_payloads"]["commented"]["topic_tags"] = ["#另一个话题#"]
        plan["action_payloads"]["commented"]["text"] = (
            "#另一个话题# @ASUS华硕官方UP 我家踩稿官：Cat means 猫"
        )
        plan["plan_hash"] = compute_action_plan_hash(plan)

        with self.assertRaisesRegex(
            ActionPlanV2Error, "action_plan_required_topic_mismatch"
        ):
            validate_action_plan_v2(plan)

    def test_wrong_comment_mention_cannot_satisfy_source_requirement(self):
        plan = complete_plan()
        plan["action_payloads"]["commented"]["mentions"] = ["@另一账号"]
        plan["action_payloads"]["commented"]["text"] = (
            "#ASUS翻转夏日# @另一账号 我家踩稿官：Cat means 猫"
        )
        plan["plan_hash"] = compute_action_plan_hash(plan)

        with self.assertRaisesRegex(
            ActionPlanV2Error, "action_plan_required_mention_mismatch"
        ):
            validate_action_plan_v2(plan)

    def test_repost_requirement_cannot_be_moved_to_comment(self):
        plan = complete_plan()
        plan["content_requirements"]["reposted"]["topic_tags"] = ["#转发专用#"]
        plan["action_payloads"]["commented"]["topic_tags"].append("#转发专用#")
        plan["action_payloads"]["commented"]["text"] += " #转发专用#"
        plan["plan_hash"] = compute_action_plan_hash(plan)

        with self.assertRaisesRegex(
            ActionPlanV2Error, "action_plan_required_topic_mismatch"
        ):
            validate_action_plan_v2(plan)

    def test_plan_hash_covers_content_requirements(self):
        plan = complete_plan()
        changed = copy.deepcopy(plan)
        changed["content_requirements"]["commented"]["mentions"] = []

        self.assertNotEqual(
            compute_action_plan_hash(plan), compute_action_plan_hash(changed)
        )

    def test_media_is_rejected_for_current_api_path(self):
        plan = complete_plan()
        plan["action_payloads"]["commented"]["media_refs"] = ["evidence://cat.jpg"]
        plan["plan_hash"] = compute_action_plan_hash(plan)

        with self.assertRaisesRegex(
            ActionPlanV2Error, "action_payload_media_unsupported"
        ):
            validate_action_plan_v2(plan, reject_media=True)

    def test_config_hash_binds_execution_revision(self):
        self.assertEqual(
            "7ea85ddf973664b5825f8a065c5866706176969209e4800060f2f390d5e67fc0",
            compute_bilibili_api_config_hash(7),
        )
        self.assertNotEqual(
            compute_bilibili_api_config_hash(7),
            compute_bilibili_api_config_hash(8),
        )

    def test_config_hash_rejects_non_positive_or_boolean_revision(self):
        for value in (0, -1, True, "7"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    ActionPlanV2Error, "execution_revision_invalid"
                ):
                    compute_bilibili_api_config_hash(value)

    def test_xiaohongshu_manual_plan_binds_exact_four_action_checklist(self):
        validated = validate_action_plan_v2(
            complete_xiaohongshu_plan(), require_executable=False
        )

        self.assertEqual(XIAOHONGSHU_ACTION_ORDER, validated.required_actions)
        self.assertEqual(
            "已认真阅读，参与抽奖",
            validated.payload_for("commented")["text"],
        )
        self.assertEqual({}, validated.payload_for("favorited"))

    def test_xiaohongshu_manual_plan_never_satisfies_executable_validation(self):
        with self.assertRaisesRegex(ActionPlanV2Error, "action_plan_not_executable"):
            validate_action_plan_v2(complete_xiaohongshu_plan())

    def test_xiaohongshu_validator_rejects_executable_claim_in_manual_mode(self):
        plan = complete_xiaohongshu_plan()
        plan["executable"] = True
        plan["plan_hash"] = compute_action_plan_hash(plan)

        with self.assertRaisesRegex(
            ActionPlanV2Error,
            "xiaohongshu_manual_plan_must_be_non_executable",
        ):
            validate_action_plan_v2(plan, require_executable=False)

    def test_xiaohongshu_validator_requires_manual_execution_path(self):
        plan = complete_xiaohongshu_plan()
        plan["execution_path_id"] = "xiaohongshu_selector_v1"
        plan["plan_hash"] = compute_action_plan_hash(plan)

        with self.assertRaisesRegex(
            ActionPlanV2Error,
            "xiaohongshu_execution_path_not_supported",
        ):
            validate_action_plan_v2(plan, require_executable=False)

    def test_xiaohongshu_validator_requires_empty_legacy_repost_bucket(self):
        plan = complete_xiaohongshu_plan()
        plan["content_requirements"]["reposted"]["topic_tags"] = ["#转发#"]
        plan["plan_hash"] = compute_action_plan_hash(plan)

        with self.assertRaisesRegex(
            ActionPlanV2Error,
            "xiaohongshu_repost_content_not_supported",
        ):
            validate_action_plan_v2(plan, require_executable=False)

    def test_xiaohongshu_comment_requires_exact_reviewed_text(self):
        plan = complete_xiaohongshu_plan()
        plan["action_payloads"]["commented"] = {}
        plan["plan_hash"] = compute_action_plan_hash(plan)

        with self.assertRaisesRegex(
            ActionPlanV2Error, "action_payload_commented_text_required"
        ):
            validate_action_plan_v2(plan, require_executable=False)

    def test_xiaohongshu_plan_requires_all_four_actions_in_canonical_order(self):
        plan = complete_xiaohongshu_plan()
        plan["required_actions"].remove("favorited")
        plan["action_payloads"].pop("favorited")
        plan["plan_hash"] = compute_action_plan_hash(plan)

        with self.assertRaisesRegex(
            ActionPlanV2Error, "xiaohongshu_four_action_plan_required"
        ):
            validate_action_plan_v2(plan, require_executable=False)

    def test_favorite_is_not_silently_enabled_for_bilibili(self):
        plan = complete_plan()
        plan["required_actions"].insert(3, "favorited")
        plan["action_payloads"]["favorited"] = {}
        plan["plan_hash"] = compute_action_plan_hash(plan)

        with self.assertRaisesRegex(
            ActionPlanV2Error, "action_plan_required_actions_invalid"
        ):
            validate_action_plan_v2(plan, require_executable=False)


if __name__ == "__main__":
    unittest.main()
