import base64
import os
import unittest
from datetime import datetime, timezone

os.environ.setdefault(
    "ENCRYPTION_KEY",
    base64.b64encode(b"0" * 32).decode(),
)
os.environ.setdefault("UPDATE_SECRET", "test-secret")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")

from app.action_plan import compute_xiaohongshu_browser_config_hash  # noqa: E402
from app.services import real_run_readiness  # noqa: E402
from shared.xiaohongshu_browser_contract import (
    XIAOHONGSHU_BROWSER_CONTRACT_VERSION,
    XIAOHONGSHU_BROWSER_EXECUTION_PATH,
    XIAOHONGSHU_BROWSER_PROBE_OBSERVATION_KIND,
    XIAOHONGSHU_BROWSER_SHADOW_OBSERVATION_KIND,
    XiaohongshuBrowserContractError,
    compute_xiaohongshu_browser_config_hash as shared_config_hash,
    compute_xiaohongshu_comment_text_hash,
    format_xiaohongshu_observed_at,
    hash_xiaohongshu_browser_observation,
    validate_xiaohongshu_browser_observation_binding,
)


def complete_selector_config():
    return {
        "followed": {"click": ["button.follow"], "done": ["button.following"]},
        "liked": {"click": ["button.like"], "done": ["button.liked"]},
        "commented": {
            "input": ["textarea.comment"],
            "submit": ["button.submit"],
            "done": ["div.comment-success"],
        },
        "favorited": {
            "click": ["button.favorite"],
            "done": ["button.favorited"],
        },
    }


def complete_observation():
    config_hash = shared_config_hash(7, complete_selector_config())
    return {
        "contract_version": XIAOHONGSHU_BROWSER_CONTRACT_VERSION,
        "platform": "xiaohongshu",
        "execution_path_id": XIAOHONGSHU_BROWSER_EXECUTION_PATH,
        "lottery_id": 11,
        "account_id": 13,
        "execution_revision": 7,
        "target_hash": "a" * 64,
        "observed_target_hash": "a" * 64,
        "rule_snapshot_id": 17,
        "rule_hash": "b" * 64,
        "action_plan_hash": "c" * 64,
        "config_hash": config_hash,
        "required_actions": ["followed", "liked", "commented", "favorited"],
        "follow_target_handle": "@brand",
        "comment_text_hash": compute_xiaohongshu_comment_text_hash(
            "精确评论文案"
        ),
        "observation_kind": XIAOHONGSHU_BROWSER_PROBE_OBSERVATION_KIND,
        "observed_at": format_xiaohongshu_observed_at(
            datetime(2026, 7, 31, 9, 30, tzinfo=timezone.utc)
        ),
        "evidence_id": "probe-1",
        "side_effects": False,
        "account_authenticated": True,
        "target_identity_verified": True,
        "selector_observation_complete": True,
        "capability_checks": {
            "followed": True,
            "liked": True,
            "commented": True,
            "favorited": True,
        },
    }


def expected_binding(observation):
    return {
        "expected_observation_kind": observation["observation_kind"],
        "expected_evidence_id": observation["evidence_id"],
        "expected_lottery_id": observation["lottery_id"],
        "expected_account_id": observation["account_id"],
        "expected_execution_revision": observation["execution_revision"],
        "expected_target_hash": observation["target_hash"],
        "expected_rule_snapshot_id": observation["rule_snapshot_id"],
        "expected_rule_hash": observation["rule_hash"],
        "expected_action_plan_hash": observation["action_plan_hash"],
        "expected_config_hash": observation["config_hash"],
        "expected_actions": tuple(observation["required_actions"]),
        "expected_follow_target_handle": observation["follow_target_handle"],
        "expected_comment_text_hash": observation["comment_text_hash"],
    }


class XiaohongshuBrowserContractTests(unittest.TestCase):
    def test_core_and_shared_config_hash_are_identical_and_revision_bound(self):
        configured = complete_selector_config()
        self.assertEqual(
            shared_config_hash(7, configured),
            compute_xiaohongshu_browser_config_hash(7, configured),
        )
        self.assertNotEqual(
            shared_config_hash(7, configured),
            shared_config_hash(8, configured),
        )

    def test_probe_binding_recomputes_the_exact_canonical_hash(self):
        observation = complete_observation()
        observation_hash = hash_xiaohongshu_browser_observation(observation)

        validated = validate_xiaohongshu_browser_observation_binding(
            observation,
            source_observation_kind=observation["observation_kind"],
            source_observation_hash=observation_hash,
            evidence_observation_kind=observation["observation_kind"],
            evidence_observation_hash=observation_hash,
            **expected_binding(observation),
        )

        self.assertEqual(observation_hash, validated.observation_hash)

    def test_missing_or_changed_immutable_binding_fails_closed(self):
        observation = complete_observation()
        for field, changed in (
            ("account_id", 99),
            ("execution_revision", 8),
            ("observed_target_hash", "d" * 64),
            ("rule_snapshot_id", 18),
            ("action_plan_hash", "e" * 64),
            ("selector_observation_complete", False),
        ):
            with self.subTest(field=field):
                tampered = dict(observation, **{field: changed})
                tampered_hash = hash_xiaohongshu_browser_observation(tampered)
                with self.assertRaises(XiaohongshuBrowserContractError):
                    validate_xiaohongshu_browser_observation_binding(
                        tampered,
                        source_observation_kind=observation["observation_kind"],
                        source_observation_hash=tampered_hash,
                        evidence_observation_kind=observation["observation_kind"],
                        evidence_observation_hash=tampered_hash,
                        **expected_binding(observation),
                    )

        missing = dict(observation)
        missing.pop("rule_hash")
        with self.assertRaisesRegex(
            XiaohongshuBrowserContractError,
            "xiaohongshu_browser_observation_schema_invalid",
        ):
            validate_xiaohongshu_browser_observation_binding(
                missing,
                source_observation_kind=observation["observation_kind"],
                source_observation_hash=(
                    hash_xiaohongshu_browser_observation(missing)
                ),
                evidence_observation_kind=observation["observation_kind"],
                evidence_observation_hash=(
                    hash_xiaohongshu_browser_observation(missing)
                ),
                **expected_binding(observation),
            )

    def test_exact_comment_whitespace_changes_the_binding(self):
        self.assertNotEqual(
            compute_xiaohongshu_comment_text_hash("精确评论文案"),
            compute_xiaohongshu_comment_text_hash("精确评论文案 "),
        )

    def test_core_requires_both_exact_probe_and_shadow_observations(self):
        probe = complete_observation()
        shadow = dict(
            probe,
            observation_kind=(
                XIAOHONGSHU_BROWSER_SHADOW_OBSERVATION_KIND
            ),
            evidence_id="shadow-1",
        )
        probe_hash = hash_xiaohongshu_browser_observation(probe)
        shadow_hash = hash_xiaohongshu_browser_observation(shadow)
        row = {
            "probe_id": "probe-1",
            "shadow_task_id": "shadow-1",
            "probe_observation": probe,
            "probe_observation_kind": probe["observation_kind"],
            "probe_observation_hash": probe_hash,
            "evidence_probe_observation_kind": probe["observation_kind"],
            "evidence_probe_observation_hash": probe_hash,
            "shadow_observation": shadow,
            "shadow_observation_kind": shadow["observation_kind"],
            "shadow_observation_hash": shadow_hash,
            "evidence_shadow_observation_kind": shadow["observation_kind"],
            "evidence_shadow_observation_hash": shadow_hash,
        }
        expected = expected_binding(probe)
        self.assertTrue(
            real_run_readiness
            ._exact_xiaohongshu_browser_observations_valid(
                row,
                lottery_id=expected["expected_lottery_id"],
                account_id=expected["expected_account_id"],
                rule_snapshot_id=expected["expected_rule_snapshot_id"],
                target_hash=expected["expected_target_hash"],
                rule_hash=expected["expected_rule_hash"],
                action_plan_hash=expected["expected_action_plan_hash"],
                config_hash=expected["expected_config_hash"],
                required_actions=expected["expected_actions"],
                execution_revision=expected[
                    "expected_execution_revision"
                ],
                follow_target_handle=expected[
                    "expected_follow_target_handle"
                ],
                comment_text_hash=expected[
                    "expected_comment_text_hash"
                ],
            )
        )

        tampered_shadow = dict(shadow, config_hash="f" * 64)
        row["shadow_observation"] = tampered_shadow
        row["shadow_observation_hash"] = (
            hash_xiaohongshu_browser_observation(tampered_shadow)
        )
        row["evidence_shadow_observation_hash"] = row[
            "shadow_observation_hash"
        ]
        self.assertFalse(
            real_run_readiness
            ._exact_xiaohongshu_browser_observations_valid(
                row,
                lottery_id=expected["expected_lottery_id"],
                account_id=expected["expected_account_id"],
                rule_snapshot_id=expected["expected_rule_snapshot_id"],
                target_hash=expected["expected_target_hash"],
                rule_hash=expected["expected_rule_hash"],
                action_plan_hash=expected["expected_action_plan_hash"],
                config_hash=expected["expected_config_hash"],
                required_actions=expected["expected_actions"],
                execution_revision=expected[
                    "expected_execution_revision"
                ],
                follow_target_handle=expected[
                    "expected_follow_target_handle"
                ],
                comment_text_hash=expected[
                    "expected_comment_text_hash"
                ],
            )
        )


if __name__ == "__main__":
    unittest.main()
