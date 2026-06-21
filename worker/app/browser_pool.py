import asyncio
import os
import psutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright

from .browser_lifecycle import BrowserLifecycle
from app.utils.log import structured_log


@dataclass
class PersistentContextState:
    context: Any
    profile_dir: str
    pid: int | None
    created_at: datetime = field(default_factory=datetime.utcnow)
    last_used_at: datetime = field(default_factory=datetime.utcnow)
    use_count: int = 0

    def touch(self):
        self.last_used_at = datetime.utcnow()
        self.use_count += 1


class BrowserPool:
    def __init__(self, max_browsers=2):
        self.max_browsers = max_browsers
        self._browsers = {}
        self._lifecycles = {}
        self._playwright = None
        self._browser_type = None
        self._lock = asyncio.Lock()
        self._persistent_contexts = {}
        self._persistent_context_meta: dict[int, PersistentContextState] = {}
        self._persistent_pids = {}
        self._browser_pids = {}
        self._rr_index = 0
        self.persistent_context_ttl = timedelta(seconds=_env_int("WORKER_PERSISTENT_CONTEXT_TTL_SECONDS", 21600))
        self.persistent_context_idle = timedelta(seconds=_env_int("WORKER_PERSISTENT_CONTEXT_IDLE_SECONDS", 1800))
        self.max_persistent_contexts = _env_int("WORKER_MAX_PERSISTENT_CONTEXTS", 20)

    async def init(self):
        self._playwright = await async_playwright().start()
        self._browser_type = self._playwright.chromium
        for i in range(self.max_browsers):
            await self.create_browser(f"browser-{i}")

    async def create_browser(self, browser_id: str):
        browser = await self._browser_type.launch(
            headless=True,
            args=["--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage"],
        )
        self._browsers[browser_id] = browser
        self._lifecycles[browser_id] = BrowserLifecycle(browser_id)
        if hasattr(browser, "_process"):
            self._browser_pids[browser_id] = browser._process.pid
        structured_log("info", "browser_created", browser_id=browser_id, pid=self._browser_pids.get(browser_id))

    async def close_browser(self, browser_id: str):
        browser = self._browsers.pop(browser_id, None)
        self._lifecycles.pop(browser_id, None)
        pid = self._browser_pids.pop(browser_id, None)
        if not browser:
            return
        try:
            await self._drain_browser(browser)
            await browser.close()
        except Exception as exc:
            structured_log("error", "browser_close_failed", browser_id=browser_id, exception=exc)
        if pid:
            await self._kill_browser_tree(pid)
        structured_log("info", "browser_closed", browser_id=browser_id, pid=pid)

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
        structured_log("info", "browser_recycling", browser_id=bid, reason=reason)
        await self.close_browser(bid)
        await self.create_browser(bid)

    async def _drain_browser(self, browser):
        for ctx in list(browser.contexts):
            try:
                await ctx.close()
            except Exception as e:
                structured_log("error", "drain_context_error", exception=e)

    async def get_account_context(self, account_id: int, profile_dir: str, proxy: dict = None):
        async with self._lock:
            await self.prune_persistent_contexts(reason="before_get_account_context", exclude_account_id=account_id)
            state = self._persistent_context_meta.get(account_id)
            if state:
                if await self._context_alive(state.context):
                    state.touch()
                    structured_log(
                        "info",
                        "persistent_context_reused",
                        account_id=account_id,
                        use_count=state.use_count,
                        age_seconds=int((datetime.utcnow() - state.created_at).total_seconds()),
                    )
                    return state.context
                await self._forget_persistent_context(account_id, reason="stale_handle")
            await self._evict_if_over_capacity(exclude_account_id=account_id)
            return await self._create_account_context_locked(account_id, profile_dir, proxy)

    async def _create_account_context_locked(self, account_id: int, profile_dir: str, proxy: dict = None):
        profile_path = Path(profile_dir)
        profile_path.mkdir(parents=True, exist_ok=True)
        ctx = await self._browser_type.launch_persistent_context(
            user_data_dir=str(profile_path),
            headless=True,
            args=["--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage"],
            proxy={"server": proxy["server"]} if proxy else None,
            viewport={"width": 1920, "height": 1080},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )
        pid = None
        if hasattr(ctx, "_browser") and hasattr(ctx._browser, "_process"):
            pid = ctx._browser._process.pid
            self._persistent_pids[account_id] = pid
        state = PersistentContextState(context=ctx, profile_dir=str(profile_path), pid=pid)
        state.touch()
        self._persistent_contexts[account_id] = ctx
        self._persistent_context_meta[account_id] = state
        structured_log("info", "persistent_context_created", account_id=account_id, pid=pid, profile_dir=str(profile_path))
        return ctx

    async def get_transient_context(self, profile_dir: str, proxy: dict = None):
        profile_path = Path(profile_dir)
        profile_path.mkdir(parents=True, exist_ok=True)
        return await self._browser_type.launch_persistent_context(
            user_data_dir=str(profile_path),
            headless=True,
            args=["--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage"],
            proxy={"server": proxy["server"]} if proxy else None,
            viewport={"width": 1280, "height": 900},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )

    async def close_account_context(self, account_id: int, reason: str = "manual_close"):
        async with self._lock:
            await self._close_account_context_locked(account_id, reason)

    async def _close_account_context_locked(self, account_id: int, reason: str):
        ctx = self._persistent_contexts.get(account_id)
        state = self._persistent_context_meta.get(account_id)
        pid = self._persistent_pids.get(account_id) or (state.pid if state else None)
        if not ctx:
            self._persistent_context_meta.pop(account_id, None)
            self._persistent_pids.pop(account_id, None)
            return
        try:
            await ctx.close()
        except Exception as exc:
            structured_log("error", "persistent_context_close_failed", account_id=account_id, reason=reason, exception=exc)
        if pid:
            await self._kill_browser_tree(pid)
        self._persistent_contexts.pop(account_id, None)
        self._persistent_context_meta.pop(account_id, None)
        self._persistent_pids.pop(account_id, None)
        structured_log("info", "persistent_context_closed", account_id=account_id, reason=reason, pid=pid)

    async def _forget_persistent_context(self, account_id: int, reason: str):
        self._persistent_contexts.pop(account_id, None)
        self._persistent_context_meta.pop(account_id, None)
        self._persistent_pids.pop(account_id, None)
        structured_log("warning", "persistent_context_forgotten", account_id=account_id, reason=reason)

    async def prune_persistent_contexts(self, reason: str = "scheduled", exclude_account_id: int | None = None) -> dict[str, int]:
        now = datetime.utcnow()
        closed = 0
        scanned = 0
        for account_id, state in list(self._persistent_context_meta.items()):
            if exclude_account_id is not None and account_id == exclude_account_id:
                continue
            scanned += 1
            if await self._context_has_open_pages(state.context):
                continue
            age = now - state.created_at
            idle = now - state.last_used_at
            if age >= self.persistent_context_ttl:
                await self._close_account_context_locked(account_id, reason="ttl")
                closed += 1
            elif idle >= self.persistent_context_idle:
                await self._close_account_context_locked(account_id, reason="idle")
                closed += 1
        if closed:
            structured_log("info", "persistent_contexts_pruned", reason=reason, scanned=scanned, closed=closed)
        return {"scanned": scanned, "closed": closed, "active": len(self._persistent_context_meta)}

    async def _evict_if_over_capacity(self, exclude_account_id: int | None = None):
        while len(self._persistent_context_meta) >= self.max_persistent_contexts:
            candidates = [
                (account_id, state)
                for account_id, state in self._persistent_context_meta.items()
                if account_id != exclude_account_id and not await self._context_has_open_pages(state.context)
            ]
            if not candidates:
                structured_log(
                    "warning",
                    "persistent_context_capacity_reached",
                    active=len(self._persistent_context_meta),
                    max_contexts=self.max_persistent_contexts,
                )
                return
            account_id, _ = min(candidates, key=lambda item: item[1].last_used_at)
            await self._close_account_context_locked(account_id, reason="capacity")

    async def _context_alive(self, ctx) -> bool:
        try:
            _ = ctx.pages
            return True
        except Exception:
            return False

    async def _context_has_open_pages(self, ctx) -> bool:
        try:
            return len(ctx.pages) > 0
        except Exception:
            return False

    async def context_reaper_loop(self, shutdown_event: asyncio.Event, interval_seconds: int = 60):
        while not shutdown_event.is_set():
            try:
                async with self._lock:
                    stats = await self.prune_persistent_contexts(reason="scheduled")
                    structured_log(
                        "info",
                        "browser_pool_context_metrics",
                        active_persistent_contexts=stats["active"],
                        total_memory_mb=round(self._current_memory_mb(), 2),
                        per_account_memory=self.account_context_memory_snapshot(),
                    )
            except asyncio.CancelledError:
                break
            except Exception as exc:
                structured_log("error", "context_reaper_error", exception=exc)
            await asyncio.sleep(max(5, interval_seconds))

    def account_context_memory_snapshot(self) -> dict[str, float]:
        snapshot: dict[str, float] = {}
        for account_id, pid in list(self._persistent_pids.items()):
            snapshot[str(account_id)] = round(self._memory_mb_for_pid(pid), 2)
        return snapshot

    def _memory_mb_for_pid(self, pid: int | None) -> float:
        if not pid:
            return 0.0
        try:
            total = 0
            parent = psutil.Process(pid)
            total += parent.memory_info().rss
            for child in parent.children(recursive=True):
                total += child.memory_info().rss
            return total / (1024 * 1024)
        except psutil.NoSuchProcess:
            return 0.0
        except Exception:
            return 0.0

    def _current_memory_mb(self):
        try:
            total = 0
            seen_pids = set()
            all_pids = list(self._browser_pids.values()) + list(self._persistent_pids.values())
            for pid in all_pids:
                if not pid or pid in seen_pids:
                    continue
                seen_pids.add(pid)
                total += self._memory_mb_for_pid(pid) * 1024 * 1024
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
        except Exception as exc:
            structured_log("error", "browser_tree_kill_failed", pid=pid, exception=exc)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default
