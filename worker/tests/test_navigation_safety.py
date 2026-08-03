import unittest

from app.utils.navigation_safety import (
    install_main_frame_navigation_guard,
    validated_platform_canonical_uri,
    validated_platform_content_url,
    validated_platform_navigation_url,
)


class FakeRequest:
    def __init__(self, url, frame, *, navigation=True):
        self.url = url
        self.frame = frame
        self._navigation = navigation

    def is_navigation_request(self):
        return self._navigation


class FakeRoute:
    def __init__(self, request):
        self.request = request
        self.aborted = False
        self.continued = False

    async def abort(self):
        self.aborted = True

    async def continue_(self):
        self.continued = True


class FakePage:
    def __init__(self):
        self.main_frame = object()
        self.pattern = None
        self.handler = None

    async def route(self, pattern, handler):
        self.pattern = pattern
        self.handler = handler


class NavigationSafetyTests(unittest.IsolatedAsyncioTestCase):
    def test_validated_url_requires_platform_https_host(self):
        self.assertEqual(
            validated_platform_navigation_url("bilibili", "https://t.bilibili.com/123456789"),
            "https://t.bilibili.com/123456789",
        )
        for value in (
            "http://t.bilibili.com/123456789",
            "https://127.0.0.1/123456789",
            "https://weibo.com/123456/status",
        ):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "platform_navigation_target_not_allowed"):
                    validated_platform_navigation_url("bilibili", value)

    def test_final_content_identity_accepts_all_existing_canonical_forms(self):
        cases = (
            (
                "bilibili",
                "https://www.bilibili.com/opus/1220306071196794898?spm_id_from=333.999.0.0",
                "canonical://bilibili/dynamic/opus_1220306071196794898",
            ),
            (
                "bilibili",
                "https://t.bilibili.com/1220306071196794898",
                "canonical://bilibili/dynamic/1220306071196794898",
            ),
            (
                "bilibili",
                "https://www.bilibili.com/video/BV1xx411c7mD/",
                "canonical://bilibili/video/BV1xx411c7mD",
            ),
            (
                "bilibili",
                "https://www.bilibili.com/read/cv123456",
                "canonical://bilibili/article/cv123456",
            ),
            (
                "weibo",
                "https://weibo.com/123456/PCAGRFqKj?refer_flag=1001030103_",
                "canonical://weibo/status/PCAGRFqKj",
            ),
            (
                "weibo",
                "https://m.weibo.cn/detail/4890123456789012",
                "canonical://weibo/status/4890123456789012",
            ),
            (
                "weibo",
                "https://m.weibo.cn/detail/7987885345",
                "canonical://weibo/status/7987885345",
            ),
            (
                "xiaohongshu",
                "https://www.xiaohongshu.com/discovery/item/64F1A2B3C4D5E6F7A8B9C0D1",
                "canonical://xiaohongshu/note/64f1a2b3c4d5e6f7a8b9c0d1",
            ),
            (
                "douyin",
                "https://www.douyin.com/video/7300000000000000000",
                "canonical://douyin/video/7300000000000000000",
            ),
            (
                "douyin",
                "https://www.iesdouyin.com/share/video/7300000000000000000/",
                "canonical://douyin/video/7300000000000000000",
            ),
            (
                "douyin",
                "https://www.douyin.com/note/7520000000000000000",
                "canonical://douyin/note/7520000000000000000",
            ),
        )
        for platform, final_url, canonical_uri in cases:
            with self.subTest(platform=platform, final_url=final_url):
                self.assertEqual(
                    validated_platform_content_url(platform, final_url, canonical_uri),
                    final_url,
                )

    def test_final_content_identity_rejects_homepage_other_post_and_unresolved_short_link(self):
        cases = (
            (
                "bilibili",
                "https://www.bilibili.com/",
                "canonical://bilibili/dynamic/opus_1220306071196794898",
            ),
            (
                "bilibili",
                "https://www.bilibili.com/opus/9999999999999999999",
                "canonical://bilibili/dynamic/opus_1220306071196794898",
            ),
            (
                "weibo",
                "https://t.cn/AbCdEf",
                "canonical://weibo/status/PCAGRFqKj",
            ),
            (
                "xiaohongshu",
                "https://www.xiaohongshu.com/explore/ffffffffffffffffffffffff",
                "canonical://xiaohongshu/note/64f1a2b3c4d5e6f7a8b9c0d1",
            ),
            (
                "douyin",
                "https://www.douyin.com/video/7300000000000000001",
                "canonical://douyin/video/7300000000000000000",
            ),
            (
                "douyin",
                "https://douyin.com/note/7520000000000000000",
                "canonical://douyin/note/7520000000000000000",
            ),
            (
                "douyin",
                "https://www.iesdouyin.com/share/note/7520000000000000000",
                "canonical://douyin/note/7520000000000000000",
            ),
        )
        for platform, final_url, canonical_uri in cases:
            with self.subTest(platform=platform, final_url=final_url):
                with self.assertRaisesRegex(ValueError, "content_identity_mismatch"):
                    validated_platform_content_url(platform, final_url, canonical_uri)

    def test_canonical_identity_rejects_legacy_url_wrong_platform_and_unknown_type(self):
        for platform, canonical_uri in (
            ("bilibili", "https://t.bilibili.com/123456789"),
            ("bilibili", "canonical://weibo/status/123456789"),
            ("bilibili", "canonical://bilibili/dynamic/settings"),
            ("bilibili", "canonical://bilibili/dynamic/" + "\uff11" * 9),
            ("bilibili", "canonical://bilibili/dynamic/" + "1" * 21),
            ("bilibili", "canonical://bilibili/video/not-a-video-id"),
            ("bilibili", "canonical://bilibili/video/av" + "\uff11" * 9),
            ("bilibili", "canonical://bilibili/article/123456"),
            ("bilibili", "canonical://bilibili/article/cv"),
            ("bilibili", "canonical://bilibili/article/CV123456"),
            ("bilibili", "canonical://bilibili/article/cv123456suffix"),
            ("bilibili", "canonical://bilibili/article/cv１２３４５６"),
            ("weibo", "canonical://weibo/video/123456789"),
            ("weibo", "canonical://weibo/status/invalid_slug"),
            ("weibo", "canonical://weibo/status/0"),
            ("weibo", "canonical://weibo/status/07987885345"),
            ("weibo", "canonical://weibo/status/9223372036854775808"),
            ("weibo", "canonical://weibo/status/７９８７８８５３４５"),
            ("xiaohongshu", "canonical://xiaohongshu/note/abc123"),
            ("xiaohongshu", "canonical://xiaohongshu/note/64f1a2b3c4d5e6f7a8b9c0d"),
            ("xiaohongshu", "canonical://xiaohongshu/note/64f1a2b3c4d5e6f7a8b9c0dg"),
            ("douyin", "canonical://douyin/video/"),
            ("douyin", "canonical://douyin/video/1"),
            (
                "douyin",
                "canonical://douyin/video/123456789012345678901234567890123",
            ),
            ("douyin", "canonical://douyin/video/１２３４５６７８"),
            ("douyin", "canonical://douyin/note/abc"),
            ("douyin", "canonical://douyin/note/752000000000000000"),
            ("douyin", "canonical://douyin/note/７５２００００００００００００００００"),
        ):
            with self.subTest(platform=platform, canonical_uri=canonical_uri):
                with self.assertRaisesRegex(ValueError, "canonical_identity_invalid"):
                    validated_platform_canonical_uri(platform, canonical_uri)

    def test_bilibili_final_article_url_requires_exact_cv_numeric_id(self):
        canonical_uri = "canonical://bilibili/article/cv123456"
        for final_url in (
            "https://www.bilibili.com/read/123456",
            "https://www.bilibili.com/read/cv",
            "https://www.bilibili.com/read/CV123456",
            "https://www.bilibili.com/read/cv123456suffix",
            "https://www.bilibili.com/read/cv１２３４５６",
        ):
            with self.subTest(final_url=final_url):
                with self.assertRaisesRegex(ValueError, "content_identity_mismatch"):
                    validated_platform_content_url(
                        "bilibili",
                        final_url,
                        canonical_uri,
                    )

    def test_bilibili_final_url_rejects_malformed_dynamic_and_video_ids(self):
        cases = (
            (
                "https://t.bilibili.com/settings",
                "canonical://bilibili/dynamic/123456789",
            ),
            (
                "https://www.bilibili.com/opus/" + "\uff11" * 9,
                "canonical://bilibili/dynamic/123456789",
            ),
            (
                "https://www.bilibili.com/video/not-a-video-id",
                "canonical://bilibili/video/BV1xx411c7mD",
            ),
        )
        for final_url, canonical_uri in cases:
            with self.subTest(final_url=final_url):
                with self.assertRaisesRegex(ValueError, "content_identity_mismatch"):
                    validated_platform_content_url(
                        "bilibili",
                        final_url,
                        canonical_uri,
                    )

    def test_xiaohongshu_final_url_rejects_malformed_note_id(self):
        with self.assertRaisesRegex(ValueError, "content_identity_mismatch"):
            validated_platform_content_url(
                "xiaohongshu",
                "https://www.xiaohongshu.com/explore/abc123",
                "canonical://xiaohongshu/note/64f1a2b3c4d5e6f7a8b9c0d1",
            )

    async def test_main_frame_cross_host_redirect_is_aborted_before_continue(self):
        page = FakePage()
        await install_main_frame_navigation_guard(page, "bilibili")
        route = FakeRoute(FakeRequest("https://127.0.0.1/admin", page.main_frame))

        await page.handler(route)

        self.assertTrue(route.aborted)
        self.assertFalse(route.continued)

    async def test_allowed_main_frame_navigation_continues(self):
        page = FakePage()
        await install_main_frame_navigation_guard(page, "bilibili")
        route = FakeRoute(FakeRequest("https://www.bilibili.com/opus/123456789", page.main_frame))

        await page.handler(route)

        self.assertFalse(route.aborted)
        self.assertTrue(route.continued)

    async def test_bound_guard_rejects_another_post_on_same_host(self):
        page = FakePage()
        await install_main_frame_navigation_guard(
            page,
            "bilibili",
            "canonical://bilibili/dynamic/123456789",
        )
        route = FakeRoute(
            FakeRequest("https://www.bilibili.com/opus/999999999", page.main_frame)
        )

        await page.handler(route)

        self.assertTrue(route.aborted)
        self.assertFalse(route.continued)

    async def test_bound_guard_allows_the_reviewed_content(self):
        page = FakePage()
        await install_main_frame_navigation_guard(
            page,
            "bilibili",
            "canonical://bilibili/dynamic/123456789",
        )
        route = FakeRoute(
            FakeRequest("https://www.bilibili.com/opus/123456789", page.main_frame)
        )

        await page.handler(route)

        self.assertFalse(route.aborted)
        self.assertTrue(route.continued)


if __name__ == "__main__":
    unittest.main()
