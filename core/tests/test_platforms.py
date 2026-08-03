import base64
import json
import os
import unittest

# Valid env so importing app.db / app.config never fails during collection.
os.environ.setdefault("ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode())
os.environ.setdefault("UPDATE_SECRET", "test-secret")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")

from app.adapter_config import (  # noqa: E402
    SELECTOR_B64_ENV,
    SELECTOR_ENV,
    platform_real_adapter_kind,
)
from app.platforms import get_platform  # noqa: E402

STRUCTURED = ("bilibili", "xiaohongshu")


def _complete_config(platform):
    configured = {
            "followed": {"click": ["button.follow"], "done": ["button.following"]},
            "liked": {"click": ["button.like"], "done": ["button.liked"]},
            "commented": {
                "input": ["textarea"],
                "submit": ["button.publish"],
                "done": ["div.comment-sent"],
            },
    }
    if platform == "xiaohongshu":
        configured["favorited"] = {
            "click": ["button.favorite"],
            "done": ["button.favorited"],
        }
    else:
        configured["reposted"] = {
            "click": ["button.repost"],
            "done": ["div.repost-sent"],
        }
    return {platform: configured}


class PlatformAdapterStatusTests(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in (SELECTOR_ENV, SELECTOR_B64_ENV)}
        os.environ.pop(SELECTOR_B64_ENV, None)
        os.environ.pop(SELECTOR_ENV, None)

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_weibo_uses_oauth_adapter_without_selector_promotion(self):
        cfg = get_platform("weibo")

        self.assertTrue(cfg["action_adapter"])
        self.assertTrue(cfg["real_run_supported"])
        self.assertEqual(cfg["adapter_status"], "oauth_capability_required")
        self.assertEqual(platform_real_adapter_kind({}, "weibo"), "oauth")

    def test_bilibili_api_adapter_is_enabled_without_selectors(self):
        cfg = get_platform("bilibili")
        self.assertTrue(cfg["action_adapter"])
        self.assertEqual(cfg["adapter_status"], "configured")

    def test_complete_selector_config_enables_adapter(self):
        """Complete selectors enable supported structured mutation adapters."""
        for platform in STRUCTURED:
            os.environ[SELECTOR_ENV] = json.dumps(_complete_config(platform))
            cfg = get_platform(platform)
            self.assertTrue(cfg["action_adapter"], platform)
            self.assertEqual(
                cfg["adapter_status"],
                (
                    "exact_browser_evidence_required"
                    if platform == "xiaohongshu"
                    else "configured"
                ),
                platform,
            )
            os.environ.pop(SELECTOR_ENV, None)

    def test_xiaohongshu_exact_selectors_enable_browser_adapter(self):
        os.environ[SELECTOR_ENV] = json.dumps(_complete_config("xiaohongshu"))

        cfg = get_platform("xiaohongshu")

        self.assertTrue(cfg["action_adapter"])
        self.assertTrue(cfg["real_run_supported"])
        self.assertEqual(
            "exact_browser_evidence_required",
            cfg["adapter_status"],
        )
        self.assertEqual(
            "xiaohongshu_exact_browser_evidence_required",
            cfg["real_run_blocker"],
        )

    def test_douyin_selectors_never_enable_real_actions(self):
        os.environ[SELECTOR_ENV] = json.dumps(_complete_config("douyin"))

        cfg = get_platform("douyin")

        self.assertFalse(cfg["action_adapter"])
        self.assertFalse(cfg["real_run_supported"])
        self.assertEqual("manual_assisted_only", cfg["adapter_status"])
        self.assertEqual(
            "douyin_no_official_interaction_api",
            cfg["real_run_blocker"],
        )

    def test_bilibili_no_longer_depends_on_selector_config(self):
        os.environ[SELECTOR_ENV] = json.dumps({"bilibili": {}})
        cfg = get_platform("bilibili")
        self.assertTrue(cfg["action_adapter"])
        self.assertEqual(cfg["adapter_status"], "configured")

    def test_incomplete_weibo_observation_config_does_not_change_oauth_status(self):
        os.environ[SELECTOR_ENV] = json.dumps(
            {"weibo": {"followed": ["x"], "liked": ["y"], "reposted": ["z"]}}
        )
        cfg = get_platform("weibo")
        self.assertTrue(cfg["action_adapter"])
        self.assertEqual(cfg["adapter_status"], "oauth_capability_required")
        self.assertEqual(platform_real_adapter_kind(_complete_config("weibo"), "weibo"), "oauth")

    def test_weibo_selector_readback_never_becomes_write_adapter(self):
        config = _complete_config("weibo")
        del config["weibo"]["liked"]["done"]
        os.environ[SELECTOR_ENV] = json.dumps(config)
        cfg = get_platform("weibo")
        self.assertTrue(cfg["action_adapter"])
        self.assertEqual(cfg["adapter_status"], "oauth_capability_required")
        self.assertEqual(platform_real_adapter_kind(config, "weibo"), "oauth")

    def test_unknown_platform_returns_none(self):
        self.assertIsNone(get_platform("nonexistent"))


if __name__ == "__main__":
    unittest.main()
