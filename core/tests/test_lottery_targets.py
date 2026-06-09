import unittest

from app.utils.lottery_targets import validate_lottery_target


class LotteryTargetValidationTests(unittest.TestCase):
    def test_accepts_bilibili_video(self):
        result = validate_lottery_target("bilibili", "https://www.bilibili.com/video/BV1xx411c7mD")
        self.assertTrue(result.valid)
        self.assertEqual("video", result.kind)

    def test_accepts_bilibili_dynamic(self):
        result = validate_lottery_target("bilibili", "https://t.bilibili.com/123456789")
        self.assertTrue(result.valid)
        self.assertEqual("dynamic", result.kind)

    def test_rejects_bilibili_homepage_and_test_query(self):
        for url in (
            "https://www.bilibili.com/",
            "https://www.bilibili.com/?dpms_gate_test=1",
            "https://www.bilibili.com/video/not-a-video-id",
        ):
            with self.subTest(url=url):
                result = validate_lottery_target("bilibili", url)
                self.assertFalse(result.valid)
                self.assertEqual("bilibili_actionable_url_required", result.reason)

    def test_keeps_other_platform_urls_generic(self):
        result = validate_lottery_target("weibo", "https://weibo.com/123")
        self.assertTrue(result.valid)
        self.assertEqual("generic", result.kind)


if __name__ == "__main__":
    unittest.main()
