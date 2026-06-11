import unittest

from app.utils.canonicalizer import (
    BilibiliCanonicalizer,
    DouyinCanonicalizer,
    WeiboCanonicalizer,
    XiaohongshuCanonicalizer,
    canonicalize_platform_url,
)


class WeiboCanonicalizerTests(unittest.IsolatedAsyncioTestCase):
    async def test_canonicalizes_desktop_status_url(self):
        result = await WeiboCanonicalizer.canonicalize("https://weibo.com/3937348351/PCAGRFqKj?type=comment")
        self.assertEqual("canonical://weibo/status/PCAGRFqKj", result.to_uri())

    async def test_canonicalizes_detail_and_mobile_to_same_status(self):
        detail = await WeiboCanonicalizer.canonicalize("https://weibo.com/detail/4890123456789012")
        mobile = await WeiboCanonicalizer.canonicalize("https://m.weibo.cn/status/4890123456789012")
        self.assertEqual(detail.to_uri(), mobile.to_uri())
        self.assertEqual("canonical://weibo/status/4890123456789012", detail.to_uri())

    async def test_rejects_profile_url(self):
        with self.assertRaises(ValueError):
            await WeiboCanonicalizer.canonicalize("https://weibo.com/u/3937348351")


class XiaohongshuCanonicalizerTests(unittest.IsolatedAsyncioTestCase):
    async def test_canonicalizes_explore_note(self):
        result = await XiaohongshuCanonicalizer.canonicalize(
            "https://www.xiaohongshu.com/explore/64F1A2B3C4D5E6F7A8B9C0D1?xsec_token=AB12"
        )
        self.assertEqual("canonical://xiaohongshu/note/64f1a2b3c4d5e6f7a8b9c0d1", result.to_uri())

    async def test_discovery_item_matches_explore(self):
        explore = await XiaohongshuCanonicalizer.canonicalize(
            "https://www.xiaohongshu.com/explore/64f1a2b3c4d5e6f7a8b9c0d1"
        )
        discovery = await XiaohongshuCanonicalizer.canonicalize(
            "https://www.xiaohongshu.com/discovery/item/64f1a2b3c4d5e6f7a8b9c0d1"
        )
        self.assertEqual(explore.to_uri(), discovery.to_uri())

    async def test_rejects_explore_root(self):
        with self.assertRaises(ValueError):
            await XiaohongshuCanonicalizer.canonicalize("https://www.xiaohongshu.com/explore")


class DouyinCanonicalizerTests(unittest.IsolatedAsyncioTestCase):
    async def test_canonicalizes_video_url(self):
        result = await DouyinCanonicalizer.canonicalize("https://www.douyin.com/video/7300000000000000000")
        self.assertEqual("canonical://douyin/video/7300000000000000000", result.to_uri())

    async def test_canonicalizes_iesdouyin_share_url_to_same_video(self):
        web = await DouyinCanonicalizer.canonicalize("https://www.douyin.com/video/7300000000000000000")
        share = await DouyinCanonicalizer.canonicalize("https://www.iesdouyin.com/share/video/7300000000000000000/")
        self.assertEqual(web.to_uri(), share.to_uri())

    async def test_rejects_profile_url(self):
        with self.assertRaises(ValueError):
            await DouyinCanonicalizer.canonicalize("https://www.douyin.com/user/MS4wLjABAAAA")


class PlatformDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_bilibili_video_regression(self):
        result = await BilibiliCanonicalizer.canonicalize("https://www.bilibili.com/video/BV1xx411c7mD")
        self.assertEqual("canonical://bilibili/video/BV1xx411c7mD", result.to_uri())

    async def test_dispatches_weibo_and_xiaohongshu(self):
        weibo = await canonicalize_platform_url("weibo", "https://weibo.com/detail/4890123456789012")
        xhs = await canonicalize_platform_url(
            "xiaohongshu", "https://www.xiaohongshu.com/explore/64f1a2b3c4d5e6f7a8b9c0d1"
        )
        self.assertEqual("canonical://weibo/status/4890123456789012", weibo)
        self.assertEqual("canonical://xiaohongshu/note/64f1a2b3c4d5e6f7a8b9c0d1", xhs)

    async def test_dispatches_douyin(self):
        result = await canonicalize_platform_url("douyin", "https://www.douyin.com/video/7300000000000000000")
        self.assertEqual("canonical://douyin/video/7300000000000000000", result)

    async def test_unmapped_platform_uses_generic_hash(self):
        result = await canonicalize_platform_url("kuaishou", "https://www.kuaishou.com/short-video/abcdef")
        self.assertTrue(result.startswith("canonical://kuaishou/url/"))


if __name__ == "__main__":
    unittest.main()
