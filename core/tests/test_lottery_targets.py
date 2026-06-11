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

    def test_accepts_weibo_status_urls(self):
        for url in (
            "https://weibo.com/3937348351/PCAGRFqKj",
            "https://weibo.com/detail/4890123456789012",
            "https://m.weibo.cn/status/4890123456789012",
            "https://m.weibo.cn/detail/PCAGRFqKj",
        ):
            with self.subTest(url=url):
                result = validate_lottery_target("weibo", url)
                self.assertTrue(result.valid)
                self.assertEqual("status", result.kind)

    def test_accepts_weibo_short_link(self):
        result = validate_lottery_target("weibo", "https://t.cn/A6abcdef")
        self.assertTrue(result.valid)
        self.assertEqual("short_link", result.kind)

    def test_rejects_weibo_homepage_and_profiles(self):
        for url in (
            "https://weibo.com/",
            "https://weibo.com/u/3937348351",
            "https://weibo.com/n/somebody",
            "https://weibo.com/hot/weibo",
            "https://weibo.com/3937348351",
        ):
            with self.subTest(url=url):
                result = validate_lottery_target("weibo", url)
                self.assertFalse(result.valid)
                self.assertEqual("weibo_actionable_url_required", result.reason)

    def test_accepts_xiaohongshu_note_urls(self):
        for url in (
            "https://www.xiaohongshu.com/explore/64f1a2b3c4d5e6f7a8b9c0d1",
            "https://www.xiaohongshu.com/explore/64f1a2b3c4d5e6f7a8b9c0d1?xsec_token=AB1234",
            "https://www.xiaohongshu.com/discovery/item/64f1a2b3c4d5e6f7a8b9c0d1",
        ):
            with self.subTest(url=url):
                result = validate_lottery_target("xiaohongshu", url)
                self.assertTrue(result.valid)
                self.assertEqual("note", result.kind)

    def test_accepts_xiaohongshu_short_link(self):
        result = validate_lottery_target("xiaohongshu", "http://xhslink.com/a/AbC123def")
        self.assertTrue(result.valid)
        self.assertEqual("short_link", result.kind)

    def test_rejects_xiaohongshu_homepage_and_profiles(self):
        for url in (
            "https://www.xiaohongshu.com/",
            "https://www.xiaohongshu.com/explore",
            "https://www.xiaohongshu.com/user/profile/5ff0e6410000000001008400",
            "https://www.xiaohongshu.com/explore/not-a-note-id",
        ):
            with self.subTest(url=url):
                result = validate_lottery_target("xiaohongshu", url)
                self.assertFalse(result.valid)
                self.assertEqual("xiaohongshu_actionable_url_required", result.reason)

    def test_accepts_douyin_video_urls(self):
        for url in (
            "https://www.douyin.com/video/7300000000000000000",
            "https://www.iesdouyin.com/share/video/7300000000000000000/",
        ):
            with self.subTest(url=url):
                result = validate_lottery_target("douyin", url)
                self.assertTrue(result.valid)
                self.assertEqual("video", result.kind)

    def test_accepts_douyin_short_link(self):
        result = validate_lottery_target("douyin", "https://v.douyin.com/abc123/")
        self.assertTrue(result.valid)
        self.assertEqual("short_link", result.kind)

    def test_rejects_douyin_homepage_and_profiles(self):
        for url in (
            "https://www.douyin.com/",
            "https://www.douyin.com/user/MS4wLjABAAAA",
            "https://www.douyin.com/video/not-a-video-id",
        ):
            with self.subTest(url=url):
                result = validate_lottery_target("douyin", url)
                self.assertFalse(result.valid)
                self.assertEqual("douyin_actionable_url_required", result.reason)

    def test_keeps_other_platform_urls_generic(self):
        result = validate_lottery_target("kuaishou", "https://www.kuaishou.com/short-video/abcdef")
        self.assertTrue(result.valid)
        self.assertEqual("generic", result.kind)


if __name__ == "__main__":
    unittest.main()
