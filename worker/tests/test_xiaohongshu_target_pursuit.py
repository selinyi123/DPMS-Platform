import unittest
from unittest.mock import AsyncMock, patch

from app.xiaohongshu_target_pursuit import (
    MUTATING_REQUEST_MARKERS,
    READ_ONLY_CONTEXT_INIT_SCRIPT,
    TargetPursuitBrowserError,
    _canonical_note_url,
    _canonical_profile_url,
    _capture_detail_snapshots,
    _install_read_only_guard,
    _new_isolated_read_only_context,
    _request_age_rejection,
    _request_is_read_only,
    _source_observation_fields,
    _source_entry_url,
    collect_xiaohongshu_target_evidence,
)


class XiaohongshuTargetPursuitSafetyTests(unittest.TestCase):
    def test_keyword_is_encoded_into_exact_platform_search_route(self):
        value = _source_entry_url("keyword", "抽奖 & 福利")

        self.assertEqual(
            (
                "https://www.xiaohongshu.com/search_result"
                "?keyword=%E6%8A%BD%E5%A5%96%20%26%20%E7%A6%8F%E5%88%A9"
                "&source=web_search_result_notes"
            ),
            value,
        )

    def test_keyword_over_64_characters_is_rejected_not_truncated(self):
        exact = "奖" * 64
        self.assertIn(
            f"keyword={'%E5%A5%96' * 64}",
            _source_entry_url("keyword", exact),
        )

        with self.assertRaisesRegex(
            TargetPursuitBrowserError,
            "xiaohongshu_keyword_too_long",
        ):
            _source_entry_url("keyword", exact + "奖")

    def test_author_profile_drops_query_capabilities(self):
        value = _canonical_profile_url(
            "https://www.xiaohongshu.com/user/profile/"
            "64f1a2b3c4d5e6f7a8b9c0d1?xsec_token=secret"
        )

        self.assertEqual(
            (
                "https://www.xiaohongshu.com/user/profile/"
                "64f1a2b3c4d5e6f7a8b9c0d1"
            ),
            value,
        )
        self.assertNotIn("xsec", value)

    def test_five_character_profile_id_matches_public_contract(self):
        self.assertEqual(
            "https://www.xiaohongshu.com/user/profile/aB_9-",
            _canonical_profile_url(
                "https://www.xiaohongshu.com/user/profile/aB_9-"
            ),
        )
        with self.assertRaises(TargetPursuitBrowserError):
            _canonical_profile_url(
                "https://www.xiaohongshu.com/user/profile/aB_9"
            )

    def test_note_url_is_direct_canonical_and_query_free(self):
        self.assertEqual(
            (
                "https://www.xiaohongshu.com/explore/"
                "64f1a2b3c4d5e6f7a8b9c0d1"
            ),
            _canonical_note_url(
                "https://www.xiaohongshu.com/discovery/item/"
                "64F1A2B3C4D5E6F7A8B9C0D1?xsec_source=pc"
            ),
        )
        self.assertIsNone(
            _canonical_note_url(
                "https://evil.example/explore/"
                "64f1a2b3c4d5e6f7a8b9c0d1"
            )
        )

    def test_non_browser_source_and_bad_profile_fail_closed(self):
        with self.assertRaises(TargetPursuitBrowserError):
            _source_entry_url("offline_search_result", "export.json")
        with self.assertRaises(TargetPursuitBrowserError):
            _canonical_profile_url(
                "https://www.xiaohongshu.com/explore/"
                "64f1a2b3c4d5e6f7a8b9c0d1"
            )

    def test_request_age_is_bounded(self):
        self.assertIsNone(
            _request_age_rejection(
                "100000",
                now_ms=100001,
            )
        )
        self.assertEqual(
            "future",
            _request_age_rejection(
                "130002",
                now_ms=100001,
            ),
        )
        self.assertEqual(
            "stale",
            _request_age_rejection(
                "10000",
                now_ms=100000,
            ),
        )

    def test_read_only_guard_has_every_interaction_family(self):
        joined = " ".join(MUTATING_REQUEST_MARKERS)
        for marker in (
            "like",
            "follow",
            "collect",
            "favorite",
            "comment",
            "publish",
            "share",
        ):
            self.assertIn(marker, joined)
        self.assertTrue(
            _request_is_read_only(
                "GET",
                "https://www.xiaohongshu.com/explore/"
                "64f1a2b3c4d5e6f7a8b9c0d1",
            )
        )
        self.assertFalse(
            _request_is_read_only(
                "POST",
                "https://edith.xiaohongshu.com/api/sns/web/v1/search/notes",
            )
        )
        self.assertFalse(
            _request_is_read_only(
                "GET",
                "https://www.xiaohongshu.com/api/comment/create",
            )
        )

    def test_keyword_and_author_evidence_include_verifiable_source(self):
        note_url = (
            "https://www.xiaohongshu.com/explore/"
            "64f1a2b3c4d5e6f7a8b9c0d1"
        )
        search_url = _source_entry_url("keyword", "抽奖")
        keyword_fields = _source_observation_fields(
            "keyword",
            "抽奖",
            search_url,
            note_url,
            observed_at="2026-07-29T00:00:00+00:00",
        )
        self.assertEqual("抽奖", keyword_fields["search_result"]["query"])
        self.assertEqual(
            note_url,
            keyword_fields["search_result"]["note_url"],
        )
        self.assertEqual(
            note_url,
            keyword_fields["source_observation"]["note_url"],
        )

        profile_url = (
            "https://www.xiaohongshu.com/user/profile/"
            "64f1a2b3c4d5e6f7a8b9c0d1"
        )
        author_fields = _source_observation_fields(
            "author_profile",
            f"{profile_url}?xsec_token=must-not-survive",
            profile_url,
            note_url,
            observed_at="2026-07-29T00:00:00+00:00",
        )
        self.assertNotIn(
            "xsec_token",
            str(author_fields["source_observation"]),
        )
        self.assertEqual(
            profile_url,
            author_fields["source_observation"][
                "author_profile_url"
            ],
        )


class XiaohongshuTargetPursuitIsolationTests(
    unittest.IsolatedAsyncioTestCase
):
    async def test_isolated_context_blocks_service_workers_and_uses_state(self):
        storage_state = {
            "cookies": [{"name": "session", "value": "masked"}],
            "origins": [],
        }
        persistent_context = AsyncMock()
        persistent_context.storage_state.return_value = storage_state
        isolated_context = AsyncMock()
        browser = AsyncMock()
        browser.new_context.return_value = isolated_context
        pool = AsyncMock()
        pool.get_available_browser.return_value = (browser, "browser-0")

        result = await _new_isolated_read_only_context(
            pool,
            persistent_context,
        )

        self.assertIs(isolated_context, result)
        browser.new_context.assert_awaited_once_with(
            storage_state=storage_state,
            service_workers="block",
            accept_downloads=False,
            viewport={"width": 1920, "height": 1080},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )
        isolated_context.route.assert_awaited_once()
        isolated_context.add_init_script.assert_awaited_once_with(
            READ_ONLY_CONTEXT_INIT_SCRIPT
        )

    async def test_guard_is_context_wide_and_fails_closed(self):
        context = AsyncMock()
        await _install_read_only_guard(context)
        pattern, guard = context.route.await_args.args
        self.assertEqual("**/*", pattern)

        class Request:
            def __init__(self, method, url, navigation=False):
                self.method = method
                self.url = url
                self._navigation = navigation

            def is_navigation_request(self):
                return self._navigation

        async def exercise(method, url, navigation=False):
            route = AsyncMock()
            route.request = Request(method, url, navigation)
            await guard(route)
            return route

        allowed = await exercise(
            "GET",
            "https://www.xiaohongshu.com/explore/"
            "64f1a2b3c4d5e6f7a8b9c0d1",
            True,
        )
        allowed.continue_.assert_awaited_once()
        allowed.abort.assert_not_awaited()

        post = await exercise(
            "POST",
            "https://edith.xiaohongshu.com/api/sns/web/v1/search/notes",
        )
        post.abort.assert_awaited_once()
        post.continue_.assert_not_awaited()

        interaction = await exercise(
            "GET",
            "https://www.xiaohongshu.com/api/comment/create",
        )
        interaction.abort.assert_awaited_once()

        external_navigation = await exercise(
            "GET",
            "https://evil.example/phish",
            True,
        )
        external_navigation.abort.assert_awaited_once()

    async def test_init_script_disables_browser_write_channels(self):
        context = AsyncMock()
        await _install_read_only_guard(context)
        script = context.add_init_script.await_args.args[0]

        for channel in (
            "WebSocket",
            "EventSource",
            "Worker",
            "SharedWorker",
            "sendBeacon",
            "fetch",
            "XMLHttpRequest",
            "HTMLFormElement",
            "requestSubmit",
            "submit",
        ):
            self.assertIn(channel, script)
        for method in ("GET", "HEAD", "OPTIONS"):
            self.assertIn(method, script)
        for marker in MUTATING_REQUEST_MARKERS:
            self.assertIn(marker, script)

    async def test_body_is_captured_before_expansion(self):
        timeline = []
        snapshots = iter(
            (
                {
                    "title": "抽奖",
                    "body_text": "收起状态正文",
                    "expanded_text": "收起状态正文",
                },
                {
                    "title": "抽奖",
                    "body_text": "收起状态正文；展开后规则",
                    "expanded_text": "收起状态正文；展开后规则",
                },
            )
        )

        async def snapshot(_page):
            timeline.append("snapshot")
            return next(snapshots)

        async def expand(_page):
            timeline.append("expand")

        with patch(
            "app.xiaohongshu_target_pursuit._detail_dom_snapshot",
            side_effect=snapshot,
        ), patch(
            "app.xiaohongshu_target_pursuit._expand_read_only_text",
            side_effect=expand,
        ):
            result = await _capture_detail_snapshots(object())

        self.assertEqual(
            ["snapshot", "expand", "snapshot"],
            timeline,
        )
        self.assertEqual("收起状态正文", result["body_text"])
        self.assertEqual(
            "收起状态正文；展开后规则",
            result["expanded_text"],
        )

    async def test_scan_uses_and_finally_closes_only_isolated_context(self):
        note_url = (
            "https://www.xiaohongshu.com/explore/"
            "64f1a2b3c4d5e6f7a8b9c0d1"
        )
        persistent_context = AsyncMock()
        isolated_context = AsyncMock()
        page = AsyncMock()
        isolated_context.new_page.return_value = page
        pool = AsyncMock()
        pool.get_account_context.return_value = persistent_context

        async def hydrate(_context, candidate, *, account):
            self.assertIs(isolated_context, _context)
            self.assertEqual(7, account["id"])
            return candidate

        with patch(
            "app.xiaohongshu_target_pursuit._select_read_only_account",
            new=AsyncMock(
                return_value={"id": 7, "execution_revision": 3}
            ),
        ), patch(
            "app.xiaohongshu_target_pursuit.prepare_account_login",
            new=AsyncMock(),
        ) as prepare_login, patch(
            "app.xiaohongshu_target_pursuit."
            "_new_isolated_read_only_context",
            new=AsyncMock(return_value=isolated_context),
        ), patch(
            "app.xiaohongshu_target_pursuit.detect_page_risk",
            new=AsyncMock(),
        ), patch(
            "app.xiaohongshu_target_pursuit._collect_note_links",
            new=AsyncMock(
                return_value=[
                    {
                        "raw_url": note_url,
                        "card_text": "抽奖",
                        "author": {},
                    }
                ]
            ),
        ), patch(
            "app.xiaohongshu_target_pursuit._hydrate_candidate",
            side_effect=hydrate,
        ):
            result = await collect_xiaohongshu_target_evidence(
                pool,
                "keyword",
                "抽奖",
                max_candidates=1,
            )

        prepare_login.assert_awaited_once_with(
            persistent_context,
            7,
            "xiaohongshu",
        )
        persistent_context.new_page.assert_not_awaited()
        isolated_context.new_page.assert_awaited_once()
        page.close.assert_awaited_once()
        isolated_context.close.assert_awaited_once()
        self.assertEqual(
            {"query": "抽奖", "note_url": note_url},
            {
                "query": result[0]["search_result"]["query"],
                "note_url": result[0]["search_result"]["note_url"],
            },
        )
        self.assertEqual(
            note_url,
            result[0]["source_observation"]["note_url"],
        )

    async def test_isolated_context_closes_when_source_collection_fails(self):
        persistent_context = AsyncMock()
        isolated_context = AsyncMock()
        page = AsyncMock()
        isolated_context.new_page.return_value = page
        pool = AsyncMock()
        pool.get_account_context.return_value = persistent_context

        with patch(
            "app.xiaohongshu_target_pursuit._select_read_only_account",
            new=AsyncMock(
                return_value={"id": 7, "execution_revision": 3}
            ),
        ), patch(
            "app.xiaohongshu_target_pursuit.prepare_account_login",
            new=AsyncMock(),
        ), patch(
            "app.xiaohongshu_target_pursuit."
            "_new_isolated_read_only_context",
            new=AsyncMock(return_value=isolated_context),
        ), patch(
            "app.xiaohongshu_target_pursuit.detect_page_risk",
            new=AsyncMock(),
        ), patch(
            "app.xiaohongshu_target_pursuit._collect_note_links",
            new=AsyncMock(side_effect=RuntimeError("mock_dom_failure")),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "mock_dom_failure",
            ):
                await collect_xiaohongshu_target_evidence(
                    pool,
                    "keyword",
                    "抽奖",
                    max_candidates=1,
                )

        page.close.assert_awaited_once()
        isolated_context.close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
