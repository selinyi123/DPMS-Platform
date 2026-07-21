import base64
import json
import os
import unittest

# Valid env so importing app.db / app.config never fails during collection.
os.environ.setdefault("ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode())
os.environ.setdefault("UPDATE_SECRET", "test-secret")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")

from app.adapter_config import SELECTOR_B64_ENV, SELECTOR_ENV  # noqa: E402
from app.platforms import get_platform  # noqa: E402

STRUCTURED = ("bilibili", "weibo", "douyin", "xiaohongshu")
SELECTOR_GATED = ("weibo", "douyin", "xiaohongshu")


def _complete_config(platform):
    return {
        platform: {
            "followed": {"click": ["button.follow"], "done": ["button.following"]},
            "liked": {"click": ["button.like"], "done": ["button.liked"]},
            "reposted": {"click": ["button.repost"], "done": ["div.repost-sent"]},
            "commented": {
                "input": ["textarea"],
                "submit": ["button.publish"],
                "done": ["div.comment-sent"],
            },
        }
    }


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

    def test_no_selector_config_stays_calibration_required(self):
        for platform in SELECTOR_GATED:
            cfg = get_platform(platform)
            self.assertFalse(cfg["action_adapter"], platform)
            self.assertEqual(cfg["adapter_status"], "calibration_required", platform)

    def test_bilibili_api_adapter_is_enabled_without_selectors(self):
        cfg = get_platform("bilibili")
        self.assertTrue(cfg["action_adapter"])
        self.assertEqual(cfg["adapter_status"], "configured")

    def test_complete_selector_config_enables_adapter(self):
        """A complete selector config flips action_adapter on for EVERY structured
        platform, including bilibili (previously special-cased off)."""
        for platform in STRUCTURED:
            os.environ[SELECTOR_ENV] = json.dumps(_complete_config(platform))
            cfg = get_platform(platform)
            self.assertTrue(cfg["action_adapter"], platform)
            self.assertEqual(cfg["adapter_status"], "configured", platform)
            os.environ.pop(SELECTOR_ENV, None)

    def test_bilibili_no_longer_depends_on_selector_config(self):
        os.environ[SELECTOR_ENV] = json.dumps({"bilibili": {}})
        cfg = get_platform("bilibili")
        self.assertTrue(cfg["action_adapter"])
        self.assertEqual(cfg["adapter_status"], "configured")

    def test_incomplete_config_does_not_enable(self):
        # Missing the commented input/submit group -> not complete.
        os.environ[SELECTOR_ENV] = json.dumps(
            {"weibo": {"followed": ["x"], "liked": ["y"], "reposted": ["z"]}}
        )
        cfg = get_platform("weibo")
        self.assertFalse(cfg["action_adapter"])
        self.assertEqual(cfg["adapter_status"], "calibration_required")

    def test_config_without_success_readback_does_not_enable(self):
        config = _complete_config("weibo")
        del config["weibo"]["liked"]["done"]
        os.environ[SELECTOR_ENV] = json.dumps(config)
        cfg = get_platform("weibo")
        self.assertFalse(cfg["action_adapter"])
        self.assertEqual(cfg["adapter_status"], "calibration_required")

    def test_unknown_platform_returns_none(self):
        self.assertIsNone(get_platform("nonexistent"))


if __name__ == "__main__":
    unittest.main()
