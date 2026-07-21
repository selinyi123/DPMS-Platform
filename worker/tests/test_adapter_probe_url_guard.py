import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.adapter_probe import validated_probe_url  # noqa: E402


class AdapterProbeUrlGuardTests(unittest.TestCase):
    def test_accepts_known_platform_target_and_redirect_hosts(self):
        for url in (
            "https://b23.tv/abc123",
            "https://www.bilibili.com/opus/1220306071196794898",
            "https://t.bilibili.com/1220306071196794898",
        ):
            with self.subTest(url=url):
                self.assertEqual(validated_probe_url("bilibili", url), url)

    def test_rejects_userinfo_private_host_and_non_default_port(self):
        for url in (
            "https://www.bilibili.com:443@127.0.0.1/opus/1220306071196794898",
            "https://127.0.0.1/opus/1220306071196794898",
            "https://www.bilibili.com:444/opus/1220306071196794898",
            "http://www.bilibili.com/opus/1220306071196794898",
        ):
            with self.subTest(url=url):
                with self.assertRaisesRegex(ValueError, "adapter_probe_target_not_allowed"):
                    validated_probe_url("bilibili", url)

    def test_rejects_cross_platform_or_unknown_redirect(self):
        for platform, url in (
            ("bilibili", "https://example.com/opus/1220306071196794898"),
            ("bilibili", "https://weibo.com/123/status"),
            ("unknown", "https://www.bilibili.com/opus/1220306071196794898"),
        ):
            with self.subTest(platform=platform, url=url):
                with self.assertRaisesRegex(ValueError, "adapter_probe_target_not_allowed"):
                    validated_probe_url(platform, url)


if __name__ == "__main__":
    unittest.main()
