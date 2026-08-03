import copy
import unittest

from app.action_plan import (
    ACTION_ORDER,
    DOUYIN_ACTION_ORDER,
    DOUYIN_MANUAL_EXECUTION_PATH,
    DOUYIN_NO_OFFICIAL_API_BLOCKER,
    XIAOHONGSHU_ACTION_ORDER,
    XIAOHONGSHU_BROWSER_EXECUTION_PATH,
    XIAOHONGSHU_MANUAL_EXECUTION_PATH,
    XIAOHONGSHU_NO_OFFICIAL_API_BLOCKER,
    ActionPlanV2Error,
    compute_action_plan_hash,
    compute_bilibili_api_config_hash,
    compute_target_hash,
    compute_xiaohongshu_browser_config_hash,
    validate_action_plan_v2,
)
from app.platform_modules import get_platform_module


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


def complete_xiaohongshu_plan(
    required_actions: tuple[str, ...] = XIAOHONGSHU_ACTION_ORDER,
    *,
    execution_path_id: str = XIAOHONGSHU_MANUAL_EXECUTION_PATH,
    executable: bool = False,
) -> dict:
    action_payloads = {
        "followed": {"target_handle": "@小红书博主"},
        "liked": {},
        "commented": {"text": "已认真阅读，参与抽奖"},
        "favorited": {},
    }
    plan = {
        "version": 2,
        "platform": "xiaohongshu",
        "is_lottery": True,
        "required_actions": list(required_actions),
        "action_payloads": {
            action: action_payloads[action] for action in required_actions
        },
        # Keep the v2 compatibility shape: repost requirements remain present
        # and empty even though repost is not one of the XHS four actions.
        "content_requirements": {
            "follow_targets": (
                ["@小红书博主"] if "followed" in required_actions else []
            ),
            "commented": {"topic_tags": [], "mentions": []},
            "reposted": {"topic_tags": [], "mentions": []},
        },
        "execution_path_id": execution_path_id,
        "rule_snapshot_id": 202,
        "rule_hash": "b" * 64,
        "review_required": False,
        "executable": executable,
        "confidence": 1.0,
        "source": "operator_complete_attestation",
        "reviewed_by": "operator-1",
        "rule_complete_confirmed": True,
        "unsupported_actions": [],
        "represented_requirements": [],
        "unresolved_requirements": [],
        "ambiguity_patterns": [],
        "payload_validation_errors": [],
        "capability_blockers": (
            []
            if execution_path_id == XIAOHONGSHU_BROWSER_EXECUTION_PATH
            else [XIAOHONGSHU_NO_OFFICIAL_API_BLOCKER]
        ),
    }
    plan["plan_hash"] = compute_action_plan_hash(plan)
    return plan


def complete_douyin_plan(*, use_favorite: bool = True) -> dict:
    last_action = "favorited" if use_favorite else "reposted"
    action_payloads = {
        "followed": {"target_handle": "@抖音博主"},
        "liked": {},
        "commented": {
            "text": "#夏日好物# @品牌官方 参与抽奖",
            "topic_tags": ["#夏日好物#"],
            "mentions": ["@品牌官方"],
        },
        last_action: {} if use_favorite else {"text": "转发参与"},
    }
    plan = {
        "version": 2,
        "platform": "douyin",
        "is_lottery": True,
        "required_actions": ["followed", "liked", "commented", last_action],
        "action_payloads": action_payloads,
        "content_requirements": {
            "follow_targets": ["@抖音博主"],
            "commented": {
                "topic_tags": ["#夏日好物#"],
                "mentions": ["@品牌官方"],
            },
            "reposted": {"topic_tags": [], "mentions": []},
        },
        "execution_path_id": DOUYIN_MANUAL_EXECUTION_PATH,
        "rule_snapshot_id": 303,
        "rule_hash": "c" * 64,
        "review_required": False,
        "executable": False,
        "confidence": 1.0,
        "source": "operator_complete_attestation",
        "reviewed_by": "operator-1",
        "rule_complete_confirmed": True,
        "unsupported_actions": ["topic_tag", "mention_account"],
        "represented_requirements": ["topic_tag", "mention_account"],
        "unresolved_requirements": [],
        "ambiguity_patterns": [],
        "payload_validation_errors": [],
        "capability_blockers": [DOUYIN_NO_OFFICIAL_API_BLOCKER],
    }
    plan["plan_hash"] = compute_action_plan_hash(plan)
    return plan


class ActionPlanV2Tests(unittest.TestCase):
    def test_global_order_adds_favorite_without_reordering_existing_actions(self):
        self.assertEqual(
            ("followed", "liked", "commented", "favorited", "reposted"),
            ACTION_ORDER,
        )

    def test_unknown_platform_cannot_inherit_bilibili_plan_semantics(self):
        plan = complete_plan()
        plan["platform"] = "unknown"
        plan["plan_hash"] = compute_action_plan_hash(plan)

        with self.assertRaisesRegex(
            ActionPlanV2Error, "action_plan_platform_unsupported"
        ):
            validate_action_plan_v2(plan)

    def test_douyin_explicitly_supports_favorite_and_repost_as_distinct_actions(self):
        self.assertEqual(ACTION_ORDER, DOUYIN_ACTION_ORDER)

        favorite_plan = validate_action_plan_v2(
            complete_douyin_plan(use_favorite=True), require_executable=False
        )
        repost_plan = validate_action_plan_v2(
            complete_douyin_plan(use_favorite=False), require_executable=False
        )

        self.assertIn("favorited", favorite_plan.required_actions)
        self.assertNotIn("reposted", favorite_plan.required_actions)
        self.assertIn("reposted", repost_plan.required_actions)
        self.assertNotIn("favorited", repost_plan.required_actions)

        combined = complete_douyin_plan(use_favorite=True)
        combined["required_actions"].append("reposted")
        combined["action_payloads"]["reposted"] = {"text": "转发参与"}
        combined["plan_hash"] = compute_action_plan_hash(combined)
        combined_plan = validate_action_plan_v2(combined, require_executable=False)
        self.assertEqual(DOUYIN_ACTION_ORDER, combined_plan.required_actions)

    def test_douyin_plain_repost_does_not_require_invented_text(self):
        plan = complete_douyin_plan(use_favorite=False)
        plan["action_payloads"]["reposted"] = {}
        plan["plan_hash"] = compute_action_plan_hash(plan)

        validated = validate_action_plan_v2(plan, require_executable=False)

        self.assertEqual({}, validated.payload_for("reposted"))

    def test_douyin_manual_plan_never_satisfies_executable_validation(self):
        with self.assertRaisesRegex(ActionPlanV2Error, "action_plan_not_executable"):
            validate_action_plan_v2(complete_douyin_plan())

    def test_douyin_rejects_executable_claim_and_foreign_path(self):
        executable = complete_douyin_plan()
        executable["executable"] = True
        executable["plan_hash"] = compute_action_plan_hash(executable)
        with self.assertRaisesRegex(
            ActionPlanV2Error, "douyin_manual_plan_must_be_non_executable"
        ):
            validate_action_plan_v2(executable, require_executable=False)

        foreign_path = complete_douyin_plan()
        foreign_path["execution_path_id"] = "bilibili_api_v2"
        foreign_path["plan_hash"] = compute_action_plan_hash(foreign_path)
        with self.assertRaisesRegex(
            ActionPlanV2Error, "douyin_execution_path_invalid"
        ):
            validate_action_plan_v2(foreign_path, require_executable=False)

        forged = complete_douyin_plan()
        forged["executable"] = True
        forged["execution_path_id"] = "bilibili_api_v2"
        forged["plan_hash"] = compute_action_plan_hash(forged)
        with self.assertRaisesRegex(
            ActionPlanV2Error, "douyin_manual_plan_must_be_non_executable"
        ):
            validate_action_plan_v2(forged, require_executable=False)

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

    def test_lone_surrogate_reviewer_is_rejected_without_encoder_error(self):
        plan = complete_plan()
        plan["reviewed_by"] = "\ud800"

        with self.assertRaises(ActionPlanV2Error) as raised:
            validate_action_plan_v2(plan)

        self.assertEqual(
            "action_plan_review_attestation_invalid",
            raised.exception.code,
        )

    def test_bilibili_rejects_foreign_execution_path(self):
        plan = complete_plan()
        plan["execution_path_id"] = "weibo_oauth_v1"
        plan["plan_hash"] = compute_action_plan_hash(plan)

        with self.assertRaises(ActionPlanV2Error) as raised:
            validate_action_plan_v2(plan)

        self.assertEqual(
            "bilibili_execution_path_not_supported",
            raised.exception.code,
        )

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

    def test_xiaohongshu_manual_plan_accepts_canonical_nonempty_subsets(self):
        for actions in (
            ("liked",),
            ("liked", "commented", "favorited"),
            XIAOHONGSHU_ACTION_ORDER,
        ):
            with self.subTest(actions=actions):
                validated = validate_action_plan_v2(
                    complete_xiaohongshu_plan(actions),
                    require_executable=False,
                )
                self.assertEqual(actions, validated.required_actions)

    def test_xiaohongshu_manual_plan_never_satisfies_executable_validation(self):
        with self.assertRaisesRegex(ActionPlanV2Error, "action_plan_not_executable"):
            validate_action_plan_v2(complete_xiaohongshu_plan())

    def test_xiaohongshu_browser_plan_is_executable_for_canonical_subsets(self):
        validated = validate_action_plan_v2(
            complete_xiaohongshu_plan(
                ("followed", "commented", "favorited"),
                execution_path_id=XIAOHONGSHU_BROWSER_EXECUTION_PATH,
                executable=True,
            )
        )

        self.assertEqual(
            ("followed", "commented", "favorited"),
            validated.required_actions,
        )
        self.assertEqual(
            XIAOHONGSHU_BROWSER_EXECUTION_PATH,
            validated.execution_path_id,
        )

    def test_xiaohongshu_validator_rejects_executable_claim_in_manual_mode(self):
        plan = complete_xiaohongshu_plan()
        plan["executable"] = True
        plan["plan_hash"] = compute_action_plan_hash(plan)

        with self.assertRaisesRegex(
            ActionPlanV2Error,
            "xiaohongshu_manual_plan_must_be_non_executable",
        ):
            validate_action_plan_v2(plan, require_executable=False)

        plan["execution_path_id"] = "bilibili_api_v2"
        plan["plan_hash"] = compute_action_plan_hash(plan)
        with self.assertRaisesRegex(
            ActionPlanV2Error,
            "xiaohongshu_execution_path_not_supported",
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

    def test_xiaohongshu_config_hash_binds_selectors_and_revision(self):
        selectors = {
            "followed": {"click": ["follow"], "done": ["following"]},
        }
        self.assertNotEqual(
            compute_xiaohongshu_browser_config_hash(1, selectors),
            compute_xiaohongshu_browser_config_hash(2, selectors),
        )
        changed = {
            "followed": {"click": ["follow-v2"], "done": ["following"]},
        }
        self.assertNotEqual(
            compute_xiaohongshu_browser_config_hash(1, selectors),
            compute_xiaohongshu_browser_config_hash(1, changed),
        )

    def test_xiaohongshu_dispatch_binding_covers_exact_plan_target_and_config(self):
        plan = complete_xiaohongshu_plan(
            ("followed", "commented"),
            execution_path_id=XIAOHONGSHU_BROWSER_EXECUTION_PATH,
            executable=True,
        )
        selectors = {
            "followed": {"click": ["follow"], "done": ["following"]},
            "liked": {"click": ["like"], "done": ["liked"]},
            "commented": {
                "input": ["comment"],
                "submit": ["submit"],
                "done": ["commented"],
            },
            "favorited": {
                "click": ["favorite"],
                "done": ["favorited"],
            },
        }
        canonical_url = (
            "canonical://xiaohongshu/note/64f1a2b3c4d5e6f7a8b9c0d1"
        )
        binding = get_platform_module(
            "xiaohongshu"
        ).build_dispatch_plan_binding(
            lottery={
                "action_plan": plan,
                "authoritative_rule_snapshot_id": plan[
                    "rule_snapshot_id"
                ],
                "rule_hash": plan["rule_hash"],
                "action_plan_hash": plan["plan_hash"],
                "canonical_url": canonical_url,
            },
            task_mode="real_run",
            account={"execution_revision": 9},
            selector_config=selectors,
            stored_execution_path=XIAOHONGSHU_BROWSER_EXECUTION_PATH,
        )

        self.assertEqual(compute_target_hash(canonical_url), binding["target_hash"])
        self.assertEqual(
            compute_xiaohongshu_browser_config_hash(9, selectors),
            binding["config_hash"],
        )
        self.assertEqual(plan["plan_hash"], binding["action_plan_hash"])
        self.assertEqual(9, binding["execution_revision"])

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

    def test_xiaohongshu_plan_rejects_empty_wrong_order_and_repost(self):
        empty = complete_xiaohongshu_plan(())

        wrong_order = complete_xiaohongshu_plan(("liked", "commented"))
        wrong_order["required_actions"] = ["commented", "liked"]
        wrong_order["plan_hash"] = compute_action_plan_hash(wrong_order)

        reposted = complete_xiaohongshu_plan(("liked",))
        reposted["required_actions"].append("reposted")
        reposted["action_payloads"]["reposted"] = {"text": "转发参与"}
        reposted["plan_hash"] = compute_action_plan_hash(reposted)

        for plan, expected in (
            (empty, "action_plan_required_actions_invalid"),
            (wrong_order, "action_plan_action_order_invalid"),
            (reposted, "action_plan_required_actions_invalid"),
        ):
            with self.subTest(expected=expected):
                with self.assertRaisesRegex(ActionPlanV2Error, expected):
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
