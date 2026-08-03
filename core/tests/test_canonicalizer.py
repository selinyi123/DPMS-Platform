import unittest
from unittest.mock import patch

import httpx

from app.utils.canonicalizer import (
    BilibiliCanonicalizer,
    CanonicalizationError,
    DouyinCanonicalizer,
    WeiboCanonicalizer,
    XiaohongshuCanonicalizer,
    canonicalize_platform_url,
    resolve_platform_short_link,
)


class _RedirectResponse:
    def __init__(self, location: str | None, status_code: int = 302):
        self.status_code = status_code
        self.headers = {"location": location} if location else {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class _RedirectClient:
    def __init__(
        self,
        location: str | None,
        *,
        head_status: int = 302,
        get_status: int = 302,
    ):
        self.location = location
        self.head_status = head_status
        self.get_status = get_status
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def head(self, url, **_kwargs):
        self.requests.append(url)
        return _RedirectResponse(
            self.location if self.head_status in {301, 302, 303, 307, 308} else None,
            self.head_status,
        )

    def stream(self, method, url, **_kwargs):
        self.requests.append(f"{method} {url}")
        return _RedirectResponse(
            self.location if self.get_status in {301, 302, 303, 307, 308} else None,
            self.get_status,
        )


class _FailingRequest:
    def __init__(self, exc):
        self.exc = exc

    async def __aenter__(self):
        raise self.exc

    async def __aexit__(self, *_exc):
        return False


class _FailingRedirectClient:
    def __init__(self, exc):
        self.exc = exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def head(self, _url, **_kwargs):
        raise self.exc

    def stream(self, _method, _url, **_kwargs):
        return _FailingRequest(self.exc)


class ShortLinkSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_safe_platform_redirect_is_returned_without_following_unchecked_hops(self):
        client = _RedirectClient("https://www.bilibili.com/opus/1220306071196794898")
        with patch("httpx.AsyncClient", return_value=client):
            resolved = await resolve_platform_short_link(
                "https://b23.tv/AbCdEf",
                "bilibili",
                "b23.tv",
            )

        self.assertEqual(resolved, "https://www.bilibili.com/opus/1220306071196794898")
        self.assertEqual(client.requests, ["https://b23.tv/AbCdEf"])

    async def test_redirect_to_private_or_foreign_host_is_rejected_before_request(self):
        for location in ("http://127.0.0.1/admin", "https://evil.example/steal"):
            with self.subTest(location=location):
                client = _RedirectClient(location)
                with patch("httpx.AsyncClient", return_value=client):
                    with self.assertRaisesRegex(ValueError, "canonicalization_target_not_allowed"):
                        await resolve_platform_short_link(
                            "https://b23.tv/AbCdEf",
                            "bilibili",
                            "b23.tv",
                        )
                self.assertEqual(client.requests, ["https://b23.tv/AbCdEf"])

    async def test_get_fallback_preserves_shorteners_that_reject_head(self):
        client = _RedirectClient(
            "https://www.bilibili.com/opus/1220306071196794898",
            head_status=405,
        )
        with patch("httpx.AsyncClient", return_value=client):
            resolved = await resolve_platform_short_link(
                "https://b23.tv/AbCdEf",
                "bilibili",
                "b23.tv",
            )

        self.assertEqual(resolved, "https://www.bilibili.com/opus/1220306071196794898")
        self.assertEqual(
            client.requests,
            ["https://b23.tv/AbCdEf", "GET https://b23.tv/AbCdEf"],
        )

    async def test_get_fallback_handles_head_success_without_location(self):
        client = _RedirectClient(
            "https://m.bilibili.com/video/BV1xx411c7mD",
            head_status=200,
        )
        with patch("httpx.AsyncClient", return_value=client):
            result = await BilibiliCanonicalizer.canonicalize(
                "https://b23.tv/AbCdEf"
            )

        self.assertEqual(
            "canonical://bilibili/video/BV1xx411c7mD",
            result.to_uri(),
        )
        self.assertEqual(
            ["https://b23.tv/AbCdEf", "GET https://b23.tv/AbCdEf"],
            client.requests,
        )

    async def test_unresolved_short_link_has_stable_non_secret_error(self):
        client = _RedirectClient(None, head_status=200, get_status=200)
        with patch("httpx.AsyncClient", return_value=client):
            with self.assertRaises(CanonicalizationError) as context:
                await BilibiliCanonicalizer.canonicalize(
                    "https://b23.tv/expired"
                )

        self.assertEqual(
            "canonicalization_short_link_unresolved",
            context.exception.code,
        )
        self.assertFalse(context.exception.retryable)

    async def test_short_link_timeout_has_stable_retryable_error(self):
        timeout = httpx.ReadTimeout("timed out")
        client = _FailingRedirectClient(timeout)
        with patch("httpx.AsyncClient", return_value=client):
            with self.assertRaises(CanonicalizationError) as context:
                await BilibiliCanonicalizer.canonicalize(
                    "https://b23.tv/AbCdEf"
                )

        self.assertEqual(
            "canonicalization_short_link_timeout",
            context.exception.code,
        )
        self.assertTrue(context.exception.retryable)


class WeiboCanonicalizerTests(unittest.IsolatedAsyncioTestCase):
    async def test_canonicalizes_desktop_status_url(self):
        result = await WeiboCanonicalizer.canonicalize("https://weibo.com/3937348351/PCAGRFqKj?type=comment")
        self.assertEqual("canonical://weibo/status/PCAGRFqKj", result.to_uri())

    async def test_canonicalizes_detail_and_mobile_to_same_status(self):
        detail = await WeiboCanonicalizer.canonicalize("https://weibo.com/detail/4890123456789012")
        mobile = await WeiboCanonicalizer.canonicalize("https://m.weibo.cn/status/4890123456789012")
        self.assertEqual(detail.to_uri(), mobile.to_uri())
        self.assertEqual("canonical://weibo/status/4890123456789012", detail.to_uri())

    async def test_canonicalizes_official_ten_digit_numeric_status_example(self):
        result = await WeiboCanonicalizer.canonicalize(
            "https://m.weibo.cn/status/7987885345"
        )
        self.assertEqual("canonical://weibo/status/7987885345", result.to_uri())

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

    async def test_rejects_invalid_note_id_after_short_link_resolution(self):
        with patch(
            "app.utils.canonicalizer.resolve_short_link",
            return_value="https://www.xiaohongshu.com/explore/not-a-note-id",
        ):
            with self.assertRaises(ValueError):
                await XiaohongshuCanonicalizer.canonicalize(
                    "https://xhslink.com/valid-short-token"
                )


class DouyinCanonicalizerTests(unittest.IsolatedAsyncioTestCase):
    async def test_canonicalizes_video_url(self):
        result = await DouyinCanonicalizer.canonicalize("https://www.douyin.com/video/7300000000000000000")
        self.assertEqual("canonical://douyin/video/7300000000000000000", result.to_uri())

    async def test_rejects_non_ascii_or_out_of_range_video_id(self):
        for url in (
            "https://www.douyin.com/video/1",
            "https://www.douyin.com/video/123456789012345678901234567890123",
            "https://www.douyin.com/video/１２３４５６７８",
            "https://www.iesdouyin.com/share/video/1/",
        ):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    await DouyinCanonicalizer.canonicalize(url)

    async def test_canonicalizes_iesdouyin_share_url_to_same_video(self):
        web = await DouyinCanonicalizer.canonicalize("https://www.douyin.com/video/7300000000000000000")
        share = await DouyinCanonicalizer.canonicalize("https://www.iesdouyin.com/share/video/7300000000000000000/")
        self.assertEqual(web.to_uri(), share.to_uri())

    async def test_canonicalizes_note_url(self):
        result = await DouyinCanonicalizer.canonicalize(
            "https://www.douyin.com/note/7659275356428852849"
        )

        self.assertEqual(
            "canonical://douyin/note/7659275356428852849", result.to_uri()
        )

    async def test_rejects_malformed_note_id(self):
        with self.assertRaises(ValueError):
            await DouyinCanonicalizer.canonicalize("https://www.douyin.com/note/1")

    async def test_rejects_profile_url(self):
        with self.assertRaises(ValueError):
            await DouyinCanonicalizer.canonicalize("https://www.douyin.com/user/MS4wLjABAAAA")


class PlatformDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_known_platform_case_variant_uses_structured_canonicalizer(self):
        result = await canonicalize_platform_url(
            " BILIBILI ",
            "https://www.bilibili.com/video/BV1xx411c7mD",
        )

        self.assertEqual("canonical://bilibili/video/BV1xx411c7mD", result)

    async def test_bilibili_video_regression(self):
        result = await BilibiliCanonicalizer.canonicalize("https://www.bilibili.com/video/BV1xx411c7mD")
        self.assertEqual("canonical://bilibili/video/BV1xx411c7mD", result.to_uri())

    async def test_bilibili_mobile_video_and_opus(self):
        video = await BilibiliCanonicalizer.canonicalize(
            "https://m.bilibili.com/video/BV1xx411c7mD?share_source=copy"
        )
        opus = await BilibiliCanonicalizer.canonicalize(
            "https://m.bilibili.com/opus/1220306071196794898"
        )

        self.assertEqual(
            "canonical://bilibili/video/BV1xx411c7mD",
            video.to_uri(),
        )
        self.assertEqual(
            "canonical://bilibili/dynamic/opus_1220306071196794898",
            opus.to_uri(),
        )

    async def test_bilibili_article_requires_exact_cv_numeric_path(self):
        result = await BilibiliCanonicalizer.canonicalize(
            "https://www.bilibili.com/read/cv123456?from=search"
        )
        self.assertEqual(
            "canonical://bilibili/article/cv123456",
            result.to_uri(),
        )

        for invalid in (
            "https://www.bilibili.com/read/123456",
            "https://www.bilibili.com/read/cv",
            "https://www.bilibili.com/read/CV123456",
            "https://www.bilibili.com/read/cv123456/extra",
            "https://www.bilibili.com/archive/read/cv123456",
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    await BilibiliCanonicalizer.canonicalize(invalid)

    async def test_trailing_dot_host_uses_the_same_normalized_identity(self):
        result = await BilibiliCanonicalizer.canonicalize(
            "https://www.bilibili.com./opus/1220306071196794898"
        )
        self.assertEqual(
            "canonical://bilibili/dynamic/opus_1220306071196794898",
            result.to_uri(),
        )

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
