import asyncio
import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.browser_pool import (
    CHROMIUM_SANDBOX_IGNORED_DEFAULT_ARGS,
    BrowserPool,
    BrowserPoolCapacityExceeded,
)


class BrowserPoolLazyInitializationTests(
    unittest.IsolatedAsyncioTestCase
):
    async def asyncSetUp(self):
        self.lease_token = (
            "2bd774c4-58aa-4756-b197-c831828b5fa4"
        )
        self.acquire_lease = AsyncMock(
            return_value=self.lease_token
        )
        self.renew_lease = AsyncMock()
        self.release_lease = AsyncMock(return_value=True)
        patchers = (
            patch(
                "app.browser_pool."
                "acquire_account_profile_context_lease",
                new=self.acquire_lease,
            ),
            patch(
                "app.browser_pool."
                "renew_account_profile_context_lease",
                new=self.renew_lease,
            ),
            patch(
                "app.browser_pool."
                "release_account_profile_context_lease",
                new=self.release_lease,
            ),
        )
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def account_profile(root: str, account_id: int) -> str:
        return str(Path(root) / "weibo" / f"account_{account_id}")

    def fake_runtime(self):
        browsers = []

        async def launch(**_kwargs):
            browser = SimpleNamespace(
                contexts=[],
                close=AsyncMock(),
            )
            browsers.append(browser)
            return browser

        browser_type = SimpleNamespace(
            launch=AsyncMock(side_effect=launch),
            launch_persistent_context=AsyncMock(),
        )
        playwright = SimpleNamespace(
            chromium=browser_type,
            stop=AsyncMock(),
        )
        starter = SimpleNamespace(
            start=AsyncMock(return_value=playwright)
        )
        factory = MagicMock(return_value=starter)
        return factory, starter, playwright, browser_type, browsers

    async def test_init_starts_no_playwright_or_browser_process(self):
        factory, starter, _runtime, browser_type, _browsers = (
            self.fake_runtime()
        )
        pool = BrowserPool(max_browsers=2)
        with patch("app.browser_pool.async_playwright", factory):
            await pool.init()

        factory.assert_not_called()
        starter.start.assert_not_awaited()
        browser_type.launch.assert_not_awaited()

    async def test_first_shared_browser_request_initializes_once(self):
        factory, starter, runtime, browser_type, browsers = (
            self.fake_runtime()
        )
        pool = BrowserPool(max_browsers=2)
        with patch("app.browser_pool.async_playwright", factory):
            await pool.init()
            first, first_id = await pool.get_available_browser()
            second, second_id = await pool.get_available_browser()
            await pool.close()
            await pool.close()

        self.assertIn(first, browsers)
        self.assertIn(second, browsers)
        self.assertEqual({first_id, second_id}, {"browser-0", "browser-1"})
        factory.assert_called_once_with()
        starter.start.assert_awaited_once_with()
        self.assertEqual(browser_type.launch.await_count, 2)
        for call in browser_type.launch.await_args_list:
            self.assertNotIn("--no-sandbox", call.kwargs["args"])
            self.assertEqual(
                call.kwargs["ignore_default_args"],
                CHROMIUM_SANDBOX_IGNORED_DEFAULT_ARGS,
            )
        runtime.stop.assert_awaited_once_with()

    async def test_account_context_does_not_prelaunch_shared_browsers(self):
        factory, starter, runtime, browser_type, _browsers = (
            self.fake_runtime()
        )
        context = SimpleNamespace(pages=[], close=AsyncMock())
        browser_type.launch_persistent_context.return_value = context
        pool = BrowserPool(max_browsers=3)
        with (
            patch("app.browser_pool.async_playwright", factory),
            patch.object(
                pool,
                "_kill_browser_tree",
                new=AsyncMock(),
            ),
        ):
            await pool.init()
            with tempfile.TemporaryDirectory() as profile_root:
                pool.profiles_root = Path(profile_root)
                loaded = await pool.get_account_context(
                    7,
                    self.account_profile(profile_root, 7),
                    platform="weibo",
                )
                await pool.close()

        self.assertIs(loaded, context)
        starter.start.assert_awaited_once_with()
        browser_type.launch.assert_not_awaited()
        browser_type.launch_persistent_context.assert_awaited_once()
        launch_kwargs = (
            browser_type.launch_persistent_context.await_args.kwargs
        )
        self.assertNotIn("--no-sandbox", launch_kwargs["args"])
        self.assertEqual(
            launch_kwargs["ignore_default_args"],
            CHROMIUM_SANDBOX_IGNORED_DEFAULT_ARGS,
        )
        runtime.stop.assert_awaited_once_with()

    async def test_close_waits_for_inflight_context_launch_and_closes_it(self):
        factory, _starter, runtime, browser_type, _browsers = (
            self.fake_runtime()
        )
        launch_started = asyncio.Event()
        allow_launch = asyncio.Event()
        context = SimpleNamespace(pages=[], close=AsyncMock())

        async def launch_persistent_context(**_kwargs):
            launch_started.set()
            await allow_launch.wait()
            return context

        browser_type.launch_persistent_context.side_effect = (
            launch_persistent_context
        )
        pool = BrowserPool(max_browsers=1)
        with (
            patch("app.browser_pool.async_playwright", factory),
            patch.object(
                pool,
                "_kill_browser_tree",
                new=AsyncMock(),
            ),
            tempfile.TemporaryDirectory() as profile_root,
        ):
            pool.profiles_root = Path(profile_root)
            launch_task = asyncio.create_task(
                pool.get_account_context(
                    7,
                    self.account_profile(profile_root, 7),
                    platform="weibo",
                )
            )
            await launch_started.wait()
            close_task = asyncio.create_task(pool.close())
            await asyncio.sleep(0)
            self.assertFalse(close_task.done())

            allow_launch.set()
            self.assertIs(await launch_task, context)
            await close_task

            with self.assertRaisesRegex(
                RuntimeError,
                "browser_pool_closed",
            ):
                await pool.get_account_context(
                    8,
                    self.account_profile(profile_root, 8),
                    platform="weibo",
                )

        context.close.assert_awaited_once_with()
        browser_type.launch_persistent_context.assert_awaited_once()
        runtime.stop.assert_awaited_once_with()

    async def test_close_tracks_and_drains_transient_contexts(self):
        factory, _starter, runtime, browser_type, _browsers = (
            self.fake_runtime()
        )
        context = SimpleNamespace(pages=[], close=AsyncMock())
        browser_type.launch_persistent_context.return_value = context
        pool = BrowserPool(max_browsers=1)
        with (
            patch("app.browser_pool.async_playwright", factory),
            tempfile.TemporaryDirectory() as profile_dir,
        ):
            loaded = await pool.get_transient_context(profile_dir)
            self.assertIs(loaded, context)
            self.assertEqual(
                list(pool._transient_contexts.values()),
                [context],
            )
            await pool.close()

        context.close.assert_awaited_once_with()
        self.assertEqual(pool._transient_contexts, {})
        launch_kwargs = (
            browser_type.launch_persistent_context.await_args.kwargs
        )
        self.assertNotIn("--no-sandbox", launch_kwargs["args"])
        self.assertEqual(
            launch_kwargs["ignore_default_args"],
            CHROMIUM_SANDBOX_IGNORED_DEFAULT_ARGS,
        )
        runtime.stop.assert_awaited_once_with()

    async def test_playwright_stop_failure_clears_runtime_handles(self):
        factory, _starter, runtime, _browser_type, _browsers = (
            self.fake_runtime()
        )
        runtime.stop.side_effect = RuntimeError("transport already closed")
        pool = BrowserPool(max_browsers=1)
        with (
            patch("app.browser_pool.async_playwright", factory),
            patch("app.browser_pool.structured_log") as log,
        ):
            await pool._ensure_playwright()
            await pool.close()

        runtime.stop.assert_awaited_once_with()
        self.assertIsNone(pool._playwright)
        self.assertIsNone(pool._browser_type)
        self.assertFalse(pool._shared_browsers_initialized)
        self.assertTrue(
            any(
                call.args[1] == "playwright_stop_failed"
                for call in log.call_args_list
            )
        )

    async def test_recycle_and_account_launch_use_one_lock_order(self):
        factory, _starter, _runtime, browser_type, _browsers = (
            self.fake_runtime()
        )
        context = SimpleNamespace(pages=[], close=AsyncMock())
        browser_type.launch_persistent_context.return_value = context
        pool = BrowserPool(max_browsers=1)
        recycle_close_started = asyncio.Event()
        allow_recycle_close = asyncio.Event()
        old_browser = SimpleNamespace(contexts=[], close=AsyncMock())
        pool._browsers["browser-0"] = old_browser
        pool._lifecycles["browser-0"] = SimpleNamespace(
            should_recycle=lambda _memory: "ttl"
        )
        pool._shared_browsers_initialized = True

        async def close_for_recycle(browser_id):
            recycle_close_started.set()
            await allow_recycle_close.wait()
            pool._browsers.pop(browser_id, None)
            pool._lifecycles.pop(browser_id, None)

        with (
            patch("app.browser_pool.async_playwright", factory),
            patch.object(
                pool,
                "close_browser",
                new=AsyncMock(side_effect=close_for_recycle),
            ),
            tempfile.TemporaryDirectory() as profile_root,
        ):
            pool.profiles_root = Path(profile_root)
            shared_task = asyncio.create_task(
                pool.get_available_browser()
            )
            await recycle_close_started.wait()
            account_task = asyncio.create_task(
                pool.get_account_context(
                    7,
                    self.account_profile(profile_root, 7),
                    platform="weibo",
                )
            )
            await asyncio.sleep(0)
            allow_recycle_close.set()
            shared_result, account_result = await asyncio.wait_for(
                asyncio.gather(shared_task, account_task),
                timeout=1,
            )

        self.assertEqual(shared_result[1], "browser-0")
        self.assertIs(account_result, context)
        await pool.close()

    async def test_busy_persistent_contexts_enforce_hard_capacity(self):
        factory, _starter, _runtime, browser_type, _browsers = (
            self.fake_runtime()
        )
        context = SimpleNamespace(
            pages=[object()],
            close=AsyncMock(),
        )
        browser_type.launch_persistent_context.return_value = context
        pool = BrowserPool(max_browsers=1)
        pool.max_persistent_contexts = 1
        with (
            patch("app.browser_pool.async_playwright", factory),
            patch.object(
                pool,
                "_kill_browser_tree",
                new=AsyncMock(),
            ),
            tempfile.TemporaryDirectory() as profile_root,
        ):
            pool.profiles_root = Path(profile_root)
            await pool.get_account_context(
                7,
                self.account_profile(profile_root, 7),
                platform="weibo",
            )
            with self.assertRaisesRegex(
                BrowserPoolCapacityExceeded,
                "persistent_context_capacity_exhausted",
            ):
                await pool.get_account_context(
                    8,
                    self.account_profile(profile_root, 8),
                    platform="weibo",
                )
            await pool.close()

        self.assertEqual(
            browser_type.launch_persistent_context.await_count,
            1,
        )


if __name__ == "__main__":
    unittest.main()
