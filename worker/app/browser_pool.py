
import asyncio, psutil

from pathlib import Path

from playwright.async_api import async_playwright

from .browser_lifecycle import BrowserLifecycle

from app.utils.log import structured_log



class BrowserPool:

    def __init__(self, max_browsers=2):

        self.max_browsers = max_browsers

        self._browsers = {}

        self._lifecycles = {}

        self._playwright = None

        self._browser_type = None

        self._lock = asyncio.Lock()

        self._persistent_contexts = {}

        self._persistent_pids = {}

        self._browser_pids = {}

        self._rr_index = 0



    async def init(self):

        self._playwright = await async_playwright().start()

        self._browser_type = self._playwright.chromium

        for i in range(self.max_browsers):

            await self.create_browser(f"browser-{i}")



    async def create_browser(self, browser_id: str):

        browser = await self._browser_type.launch(

            headless=True,

            args=['--disable-gpu', '--no-sandbox', '--disable-dev-shm-usage']

        )

        self._browsers[browser_id] = browser

        self._lifecycles[browser_id] = BrowserLifecycle(browser_id)

        if hasattr(browser, '_process'):

            self._browser_pids[browser_id] = browser._process.pid



    async def get_available_browser(self):

        while True:

            async with self._lock:

                for bid, lc in list(self._lifecycles.items()):

                    mem = self._current_memory_mb()

                    reason = lc.should_recycle(mem)

                    if reason != "none":

                        await self._recycle_browser(bid, reason)

                        break

                else:

                    browser_ids = list(self._browsers.keys())

                    if not browser_ids:

                        await asyncio.sleep(1)

                        continue

                    for _ in range(len(browser_ids)):

                        bid = browser_ids[self._rr_index % len(browser_ids)]

                        self._rr_index += 1

                        lc = self._lifecycles.get(bid)

                        if lc and lc.should_recycle(self._current_memory_mb()) == "none":

                            return self._browsers[bid], bid

                    await asyncio.sleep(1)



    async def _recycle_browser(self, bid: str, reason: str):

        browser = self._browsers.pop(bid, None)

        lc = self._lifecycles.pop(bid, None)

        if not browser: return

        structured_log("info", "browser_recycling", browser_id=bid, reason=reason)

        try:

            await asyncio.wait_for(self._drain_browser(browser), timeout=30)

        except asyncio.TimeoutError:

            structured_log("error", "browser_drain_timeout", browser_id=bid)

        finally:

            await browser.close()

        if bid in self._browser_pids:

            await self._kill_browser_tree(self._browser_pids.pop(bid))

        await self.create_browser(bid)



    async def _drain_browser(self, browser):

        for ctx in browser.contexts:

            try:

                await ctx.close()

            except Exception as e:

                structured_log("error", "drain_context_error", exception=e)



    async def get_account_context(self, account_id: int, profile_dir: str, proxy: dict = None):

        if account_id in self._persistent_contexts:

            ctx = self._persistent_contexts[account_id]

            try:

                ctx.pages

                return ctx

            except Exception:

                del self._persistent_contexts[account_id]



        profile_path = Path(profile_dir)

        profile_path.mkdir(parents=True, exist_ok=True)



        ctx = await self._browser_type.launch_persistent_context(

            user_data_dir=str(profile_path),

            headless=True,

            args=['--disable-gpu', '--no-sandbox', '--disable-dev-shm-usage'],

            proxy={"server": proxy["server"]} if proxy else None,

            viewport={"width": 1920, "height": 1080},

            locale="zh-CN",

            timezone_id="Asia/Shanghai",

        )

        if hasattr(ctx, '_browser') and hasattr(ctx._browser, '_process'):

            self._persistent_pids[account_id] = ctx._browser._process.pid

        self._persistent_contexts[account_id] = ctx

        return ctx


    async def get_transient_context(self, profile_dir: str, proxy: dict = None):

        profile_path = Path(profile_dir)

        profile_path.mkdir(parents=True, exist_ok=True)

        return await self._browser_type.launch_persistent_context(

            user_data_dir=str(profile_path),

            headless=True,

            args=['--disable-gpu', '--no-sandbox', '--disable-dev-shm-usage'],

            proxy={"server": proxy["server"]} if proxy else None,

            viewport={"width": 1280, "height": 900},

            locale="zh-CN",

            timezone_id="Asia/Shanghai",

        )



    async def close_account_context(self, account_id: int):

        if account_id in self._persistent_contexts:

            ctx = self._persistent_contexts[account_id]

            pid = self._persistent_pids.get(account_id)

            await ctx.close()

            if pid:

                await self._kill_browser_tree(pid)

                del self._persistent_pids[account_id]

            del self._persistent_contexts[account_id]



    def _current_memory_mb(self):

        try:

            total = 0

            seen_pids = set()

            all_pids = list(self._browser_pids.values()) + list(self._persistent_pids.values())

            for pid in all_pids:

                if not pid or pid in seen_pids:

                    continue

                seen_pids.add(pid)

                try:

                    parent = psutil.Process(pid)

                    total += parent.memory_info().rss

                    for child in parent.children(recursive=True):

                        total += child.memory_info().rss

                except psutil.NoSuchProcess:

                    continue

            return total / (1024 * 1024)

        except Exception:

            return 0



    async def _kill_browser_tree(self, pid: int):

        try:

            parent = psutil.Process(pid)

            children = parent.children(recursive=True)

            for child in children:

                try:

                    child.kill()

                except Exception:

                    pass

            parent.kill()

        except psutil.NoSuchProcess:

            pass

        except Exception as e:

            structured_log("error", "kill_browser_tree_failed", pid=pid, exception=e)



    async def close_browser(self, bid):

        if bid in self._browsers:

            await self._browsers[bid].close()

            del self._browsers[bid]

            del self._lifecycles[bid]

            if bid in self._browser_pids:

                await self._kill_browser_tree(self._browser_pids.pop(bid))
