import unittest

from app.utils.lottery_targets import validate_lottery_target


class LotteryTargetValidationTests(unittest.TestCase):
    def test_known_platform_case_variant_does_not_fall_back_to_generic(self):
        result = validate_lottery_target(
            " BILIBILI ",
            "https://evil.example/video/BV1xx411c7mD",
        )

        self.assertFalse(result.valid)
        self.assertEqual("bilibili_actionable_url_required", result.reason)

    def test_accepts_bilibili_video(self):
        result = validate_lottery_target("bilibili", "https://www.bilibili.com/video/BV1xx411c7mD")
        self.assertTrue(result.valid)
        self.assertEqual("video", result.kind)

    def test_accepts_bilibili_dynamic(self):
        result = validate_lottery_target("bilibili", "https://t.bilibili.com/123456789")
        self.assertTrue(result.valid)
        self.assertEqual("dynamic", result.kind)

    def test_accepts_default_ports(self):
        cases = (("https://www.bilibili.com:443/opus/1220306071196794898", "dynamic"),)
        for url, kind in cases:
            with self.subTest(url=url):
                result = validate_lottery_target("bilibili", url)
                self.assertTrue(result.valid)
                self.assertEqual(kind, result.kind)

    def test_rejects_plain_http_for_structured_platforms(self):
        for platform, url, kind in (
            ("bilibili", "http://t.bilibili.com/123456789", "dynamic"),
            ("weibo", "http://t.cn/A6abcdef", "short_link"),
            ("xiaohongshu", "http://xhslink.com/a/AbC123def", "short_link"),
            ("douyin", "http://v.douyin.com/abc123/", "short_link"),
        ):
            with self.subTest(platform=platform):
                result = validate_lottery_target(platform, url)
                self.assertFalse(result.valid)
                self.assertEqual(kind, result.kind)
                self.assertEqual("https_required", result.reason)

    def test_does_not_misdiagnose_unsafe_or_foreign_http_as_https_only(self):
        cases = (
            (
                "http://operator@www.bilibili.com/opus/1220306071196794898",
                "invalid_url",
            ),
            (
                "http://www.bilibili.com:444/opus/1220306071196794898",
                "invalid_url",
            ),
            (
                "http://www.bilibili.com.evil.example/opus/1220306071196794898",
                "bilibili_actionable_url_required",
            ),
        )
        for url, reason in cases:
            with self.subTest(url=url):
                result = validate_lottery_target("bilibili", url)
                self.assertFalse(result.valid)
                self.assertEqual(reason, result.reason)

    def test_rejects_userinfo_hostname_and_port_confusion(self):
        for url in (
            "https://www.bilibili.com:443@127.0.0.1/opus/1220306071196794898",
            "https://operator@www.bilibili.com/opus/1220306071196794898",
            "https://www.bilibili.com:444/opus/1220306071196794898",
            "https://www.bilibili.com:not-a-port/opus/1220306071196794898",
            "https://www.bilibili.com.evil.example/opus/1220306071196794898",
        ):
            with self.subTest(url=url):
                result = validate_lottery_target("bilibili", url)
                self.assertFalse(result.valid)

    def test_rejects_unsafe_authority_for_generic_platform(self):
        for url in (
            "https://trusted.example:443@127.0.0.1/path",
            "https://trusted.example:8443/path",
        ):
            with self.subTest(url=url):
                result = validate_lottery_target("kuaishou", url)
                self.assertFalse(result.valid)
                self.assertEqual("invalid_url", result.reason)

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
            # Official statuses/queryid documentation uses this 10-digit MID.
            "https://m.weibo.cn/status/7987885345",
            "https://weibo.com/detail/9223372036854775807",
        ):
            with self.subTest(url=url):
                result = validate_lottery_target("weibo", url)
                self.assertTrue(result.valid)
                self.assertEqual("status", result.kind)

    def test_rejects_noncanonical_or_out_of_range_weibo_numeric_status_ids(self):
        for value in (
            "0",
            "01",
            "9223372036854775808",
            "\uff17\uff19\uff18\uff17\uff18\uff18\uff15\uff13\uff14\uff15",
        ):
            with self.subTest(value=value):
                result = validate_lottery_target(
                    "weibo",
                    f"https://m.weibo.cn/status/{value}",
                )
                self.assertFalse(result.valid)
                self.assertEqual("weibo_actionable_url_required", result.reason)

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
        result = validate_lottery_target("xiaohongshu", "https://xhslink.com/a/AbC123def")
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

    def test_accepts_douyin_note_url(self):
        result = validate_lottery_target(
            "douyin", "https://www.douyin.com/note/7659275356428852849"
        )

        self.assertTrue(result.valid)
        self.assertEqual("note", result.kind)

    def test_rejects_malformed_douyin_note_id(self):
        result = validate_lottery_target("douyin", "https://www.douyin.com/note/1")

        self.assertFalse(result.valid)
        self.assertEqual("douyin_actionable_url_required", result.reason)

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
