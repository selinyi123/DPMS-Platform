import json
import unittest
from dataclasses import replace
from types import MappingProxyType
from unittest.mock import patch

from app import adapter_config
from app.adapter_config import (
    platform_real_adapter_kind,
    recommended_config_from_probe,
    selector_config_complete,
    selector_phase_configured,
    selector_phases_for_platform,
    selector_real_phase_configured,
)
from app.platform_modules import get_platform_module


def _probe_result(platform="bilibili", recommended=None, **extra):
    """A stored probe result shaped like worker handle_probe writes it."""
    result = {
        "_summary": {"ready_for_real_actions": True},
        **extra,
    }
    if recommended is not None:
        result["_recommended_config"] = recommended
    return result


def _complete_bilibili_recommendation():
    # Matches what worker build_recommended_config emits for a fully visible page.
    return {
        "bilibili": {
            "followed": ["button:has-text('关注')"],
            "liked": ["[aria-label*='点赞']"],
            "reposted": ["button:has-text('转发')"],
            "commented": {"input": ["textarea"], "submit": ["button:has-text('发布')"], "text": "参与抽奖"},
        }
    }


class RecommendedConfigFromProbeTests(unittest.TestCase):
    def test_extracts_platform_recommendation(self):
        result = _probe_result(recommended=_complete_bilibili_recommendation())
        config = recommended_config_from_probe(result, "bilibili")
        self.assertEqual(config["followed"], ["button:has-text('关注')"])
        self.assertIn("commented", config)

    def test_accepts_json_string_result(self):
        # adapter_calibrations.result is stored as a JSON string column.
        result = json.dumps(_probe_result(recommended=_complete_bilibili_recommendation()))
        config = recommended_config_from_probe(result, "bilibili")
        self.assertTrue(config)

    def test_probe_recommendation_without_success_readback_stays_incomplete(self):
        result = _probe_result(recommended=_complete_bilibili_recommendation())
        config = recommended_config_from_probe(result, "bilibili")
        self.assertFalse(selector_config_complete("bilibili", config))

    def test_missing_recommendation_returns_empty(self):
        self.assertEqual(recommended_config_from_probe(_probe_result(), "bilibili"), {})
        self.assertEqual(recommended_config_from_probe({}, "bilibili"), {})
        self.assertEqual(recommended_config_from_probe(None, "bilibili"), {})

    def test_wrong_platform_returns_empty(self):
        result = _probe_result(recommended=_complete_bilibili_recommendation())
        self.assertEqual(recommended_config_from_probe(result, "weibo"), {})

    def test_incomplete_recommendation_is_extracted_but_fails_gate(self):
        # A probe that only saw follow/like/repost (no comment input+submit pair)
        # still yields a dict, but must not pass the real-actions completeness bar.
        partial = {"bilibili": {"followed": ["a"], "liked": ["b"], "reposted": ["c"]}}
        config = recommended_config_from_probe(_probe_result(recommended=partial), "bilibili")
        self.assertTrue(config)
        self.assertFalse(selector_config_complete("bilibili", config))

    def test_malformed_recommendation_shapes_return_empty(self):
        for bad in ([], "nope", 5, {"_recommended_config": []}, {"_recommended_config": {"bilibili": "x"}}):
            self.assertEqual(recommended_config_from_probe(bad, "bilibili"), {})

    def test_douyin_observation_config_covers_all_five_distinct_phases(self):
        config = {
            "followed": ["button:has-text('关注')"],
            "liked": ["[data-e2e='like-icon']"],
            "commented": {"input": ["textarea"], "submit": ["button:has-text('发布')"]},
            "favorited": {"done": ["[data-state='collected']"]},
            "reposted": {"done": ["[data-state='reposted']"]},
        }
        self.assertEqual(
            selector_phases_for_platform("douyin"),
            ("followed", "liked", "commented", "favorited", "reposted"),
        )
        self.assertTrue(selector_config_complete("douyin", config))
        self.assertTrue(selector_phase_configured("douyin", config, "favorited"))
        self.assertTrue(selector_phase_configured("douyin", config, "reposted"))

    def test_douyin_generic_share_control_cannot_cover_favorite_or_repost(self):
        config = {
            "followed": ["follow"],
            "liked": ["like"],
            "commented": {"input": ["textarea"], "submit": ["submit"]},
            "favorited": ["[data-e2e='share-icon']"],
            "reposted": ["[data-e2e='share-icon']"],
        }
        self.assertFalse(selector_config_complete("douyin", config))
        self.assertFalse(selector_phase_configured("douyin", config, "favorited"))
        self.assertFalse(selector_phase_configured("douyin", config, "reposted"))

    def test_changing_douyin_phase_contract_does_not_change_other_platforms(self):
        douyin = get_platform_module("douyin")
        changed_contracts = dict(douyin.shadow_phase_contracts)
        changed_contracts["favorited"] = "click_or_state"
        changed_douyin = replace(
            douyin,
            shadow_phase_contracts=MappingProxyType(changed_contracts),
        )

        def module_for(platform):
            if platform == "douyin":
                return changed_douyin
            return get_platform_module(platform)

        with patch.object(adapter_config, "get_platform_module", side_effect=module_for):
            self.assertTrue(
                selector_phase_configured(
                    "douyin",
                    {"favorited": ["button.collect"]},
                    "favorited",
                )
            )
            self.assertFalse(
                selector_phase_configured(
                    "weibo",
                    {"commented": {"done": ["comment.visible"]}},
                    "commented",
                )
            )
            self.assertTrue(
                selector_phase_configured(
                    "xiaohongshu",
                    {"favorited": ["button.collect"]},
                    "favorited",
                )
            )

    def test_weibo_selectors_are_five_phase_observation_only(self):
        config = {
            "followed": {"done": ["button.following"]},
            "liked": {"done": ["button.liked"]},
            "commented": {"input": ["textarea"], "submit": ["button.submit"]},
            "favorited": {"done": ["button.favorited"]},
            "reposted": {"done": ["div.reposted"]},
        }

        self.assertEqual(
            selector_phases_for_platform("weibo"),
            ("followed", "liked", "commented", "favorited", "reposted"),
        )
        self.assertTrue(selector_config_complete("weibo", config))
        self.assertEqual(
            platform_real_adapter_kind({"weibo": config}, "weibo"),
            "oauth",
        )

    def test_xiaohongshu_real_selectors_require_post_action_states(self):
        manual_compatible = {
            "followed": ["button.follow"],
            "liked": ["button.like"],
            "commented": {
                "input": ["textarea.comment"],
                "submit": ["button.submit"],
            },
            "favorited": ["button.favorite"],
        }
        self.assertTrue(
            selector_phase_configured(
                "xiaohongshu",
                manual_compatible,
                "commented",
            )
        )
        self.assertFalse(
            selector_real_phase_configured(
                "xiaohongshu",
                manual_compatible,
                "commented",
            )
        )
        self.assertFalse(
            selector_config_complete("xiaohongshu", manual_compatible)
        )

        real_config = {
            "followed": {
                "click": ["button.follow"],
                "done": ["button.following"],
            },
            "liked": {
                "click": ["button.like"],
                "done": ["button.liked"],
            },
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
        self.assertTrue(selector_config_complete("xiaohongshu", real_config))
        self.assertEqual(
            "selector",
            platform_real_adapter_kind(
                {"xiaohongshu": real_config},
                "xiaohongshu",
            ),
        )


if __name__ == "__main__":
    unittest.main()
