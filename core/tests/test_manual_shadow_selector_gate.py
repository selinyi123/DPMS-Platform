import unittest

from app.platform_modules.shadow import missing_manual_shadow_selector_phases


COMMENT_CONFIG = {
    "input": ["textarea.comment"],
    "submit": ["button.publish"],
}


class ManualShadowSelectorGateTests(unittest.TestCase):
    def test_bilibili_api_shadow_does_not_require_browser_observation_config(self):
        self.assertEqual(
            (),
            missing_manual_shadow_selector_phases(
                "bilibili",
                ["followed", "liked", "commented", "reposted"],
                {},
                "bilibili_api_v2",
            ),
        )

    def test_douyin_device_shadow_does_not_require_browser_selectors(self):
        self.assertEqual(
            (),
            missing_manual_shadow_selector_phases(
                "douyin",
                ["followed", "liked", "commented", "favorited"],
                {},
                "douyin_device_v1",
            ),
        )

    def test_xiaohongshu_requires_comment_input_and_submit(self):
        required = ["followed", "liked", "commented", "favorited"]
        self.assertEqual(
            ("commented",),
            missing_manual_shadow_selector_phases(
                "xiaohongshu",
                required,
                {"commented": {"input": ["textarea.comment"]}},
            ),
        )
        self.assertEqual(
            (),
            missing_manual_shadow_selector_phases(
                "xiaohongshu",
                required,
                {"commented": COMMENT_CONFIG},
            ),
        )

    def test_xiaohongshu_browser_shadow_requires_full_mutation_readback_contract(self):
        required = ["followed", "liked", "commented", "favorited"]
        self.assertEqual(
            ("followed", "liked", "commented", "favorited"),
            missing_manual_shadow_selector_phases(
                "xiaohongshu",
                required,
                {"commented": COMMENT_CONFIG},
                "xiaohongshu_browser_v1",
            ),
        )
        configured = {
            "followed": {
                "click": ["button.follow"],
                "done": ["button.followed"],
            },
            "liked": {
                "click": ["button.like"],
                "done": ["button.liked"],
            },
            "commented": {
                **COMMENT_CONFIG,
                "done": ["div.comment-published"],
            },
            "favorited": {
                "click": ["button.favorite"],
                "done": ["button.favorited"],
            },
        }
        self.assertEqual(
            (),
            missing_manual_shadow_selector_phases(
                "xiaohongshu",
                required,
                configured,
                "xiaohongshu_browser_v1",
            ),
        )

    def test_weibo_oauth_plan_shadow_uses_browser_observation_requirements(self):
        required = ["commented", "favorited", "reposted"]
        self.assertEqual(
            ("favorited",),
            missing_manual_shadow_selector_phases(
                "weibo",
                required,
                {"commented": COMMENT_CONFIG},
            ),
        )
        self.assertEqual(
            (),
            missing_manual_shadow_selector_phases(
                "weibo",
                required,
                {
                    "commented": COMMENT_CONFIG,
                    "favorited": {"done": ["button.collected"]},
                },
            ),
        )

    def test_douyin_requires_explicit_favorite_and_repost_done_state(self):
        required = ["commented", "favorited", "reposted"]
        self.assertEqual(
            ("favorited", "reposted"),
            missing_manual_shadow_selector_phases(
                "douyin",
                required,
                {"commented": COMMENT_CONFIG},
            ),
        )
        self.assertEqual(
            (),
            missing_manual_shadow_selector_phases(
                "douyin",
                required,
                {
                    "commented": COMMENT_CONFIG,
                    "favorited": {"done": ["button.collected"]},
                    "reposted": {"done": ["div.repost-complete"]},
                },
            ),
        )


if __name__ == "__main__":
    unittest.main()
