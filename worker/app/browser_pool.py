import asyncio
import os
import psutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright

from .browser_lifecycle import BrowserLifecycle
from app.account_profile_context_lease import (
    PROFILE_CONTEXT_LEASE_TTL_SECONDS,
    acquire_account_profile_context_lease,
    normalize_account_profile_context_identity,
    release_account_profile_context_lease,
    renew_account_profile_context_lease,
)
from app.account_profile_lock import (
    AccountProfileLock,
    acquire_account_profile_lock,
)
from app.utils.log import structured_log


CHROMIUM_SANDBOX_ARGS = ["--disable-gpu", "--disable-dev-shm-usage"]
# Playwright 1.44 adds --no-sandbox to its Chromium defaults even for a
# non-root process. Production runs as pwuser with the repository-pinned
# Playwright seccomp profile, so explicitly remove that unsafe default.
CHROMIUM_SANDBOX_IGNORED_DEFAULT_ARGS = ["--no-sandbox"]


@dataclass
class PersistentContextState:
    context: Any
    profile_dir: str
    platform: str
    lease_token: str
    profile_lock: AccountProfileLock
    pid: int | None
    created_at: datetime = field(default_factory=datetime.utcnow)
    last_used_at: datetime = field(default_factory=datetime.utcnow)
    use_count: int = 0

    def touch(self):
        self.last_used_at = datetime.utcnow()
        self.use_count += 1


class BrowserPoolCapacityExceeded(RuntimeError):
    pass


class BrowserPool:
    def __init__(self, max_browsers=2, *, profiles_root: str | Path = "/profiles"):
        self.max_browsers = max_browsers
        self._browsers = {}
        self._lifecycles = {}
        self._playwright = None
        self._browser_type = None
        self._initialization_lock = asyncio.Lock()
        self._shared_browsers_initialized = False
        self._closed = False
        self._lock = asyncio.Lock()
        self._persistent_contexts = {}
        self._persistent_context_meta: dict[int, PersistentContextState] = {}
        self._persistent_pids = {}
        self._transient_contexts: dict[int, Any] = {}
        self._transient_profile_dirs: dict[int, str] = {}
        self._quarantined_profile_locks: dict[
            tuple[int, str], AccountProfileLock
        ] = {}
        self._browser_pids = {}
        self._rr_index = 0
        self.profiles_root = Path(profiles_root)
        self.persistent_context_ttl = timedelta(seconds=_env_int("WORKER_PERSISTENT_CONTEXT_TTL_SECONDS", 21600))
        self.persistent_context_idle = timedelta(seconds=_env_int("WORKER_PERSISTENT_CONTEXT_IDLE_SECONDS", 1800))
        self.max_persistent_contexts = _env_int("WORKER_MAX_PERSISTENT_CONTEXTS", 20)

    async def init(self):
        """Arm the pool without starting Playwright or any Chromium process.

        Worker deployment is role-split. Eagerly launching the configured
        browser count in the control process and all four platform processes
        silently multiplies the previous resource budget. Actual browser
        runtime is therefore initialized only by a browser-consuming method.
        """

        if self.max_browsers < 1:
            raise ValueError("browser_pool_capacity_invalid")
        if self.max_persistent_contexts < 1:
            raise ValueError(
                "browser_pool_persistent_capacity_invalid"
            )
        if not self.profiles_root.is_absolute():
            raise ValueError("browser_pool_profiles_root_not_absolute")
        if self.profiles_root.is_symlink():
            raise ValueError("browser_pool_profiles_root_unsafe")
        if self._closed:
            raise RuntimeError("browser_pool_closed")

    async def _ensure_playwright(self):
        async with self._initialization_lock:
            await self._ensure_playwright_locked()

    async def _ensure_playwright_locked(self):
        if self._closed:
            raise RuntimeError("browser_pool_closed")
        if self._playwright is not None:
            return
        playwright = await async_playwright().start()
        self._playwright = playwright
        self._browser_type = playwright.chromium

    async def _ensure_shared_browsers(self):
        if self._closed:
            raise RuntimeError("browser_pool_closed")
        if self._shared_browsers_initialized:
            return
        async with self._initialization_lock:
            if self._closed:
                raise RuntimeError("browser_pool_closed")
            await self._ensure_playwright_locked()
            for index in range(self.max_browsers):
                browser_id = f"browser-{index}"
                if browser_id not in self._browsers:
                    await self._create_browser(browser_id)
            self._shared_browsers_initialized = True

    async def create_browser(self, browser_id: str):
        async with self._initialization_lock:
            await self._ensure_playwright_locked()
            await self._create_browser(browser_id)

    async def _create_browser(self, browser_id: str):
        browser = await self._browser_type.launch(
            headless=True,
            args=CHROMIUM_SANDBOX_ARGS,
            ignore_default_args=CHROMIUM_SANDBOX_IGNORED_DEFAULT_ARGS,
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
        await self._ensure_shared_browsers()
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

    def _account_profile_binding(
        self,
        account_id: int,
        profile_dir: str,
        platform: str | None,
    ) -> tuple[int, str, Path]:
        candidate = Path(profile_dir)
        derived_platform = (
            str(platform or "").strip().casefold()
            or candidate.parent.name.strip().casefold()
        )
        normalized_account_id, normalized_platform = (
            normalize_account_profile_context_identity(
                account_id,
                derived_platform,
            )
        )
        expected = (
            self.profiles_root
            / normalized_platform
            / f"account_{normalized_account_id}"
        )
        if (
            not candidate.is_absolute()
            or os.path.normcase(os.path.abspath(candidate))
            != os.path.normcase(os.path.abspath(expected))
        ):
            raise ValueError("account_profile_context_path_mismatch")
        # Resolve existing ancestors as a second fence. This catches a
        # platform/account symlink that is lexically inside /profiles but
        # actually redirects Chromium into another namespace.
        resolved_root = self.profiles_root.resolve(strict=False)
        resolved_candidate = candidate.resolve(strict=False)
        try:
            resolved_candidate.relative_to(resolved_root)
        except ValueError as exc:
            raise ValueError(
                "account_profile_context_path_outside_root"
            ) from exc
        if resolved_candidate != (
            resolved_root
            / normalized_platform
            / f"account_{normalized_account_id}"
        ):
            raise ValueError("account_profile_context_path_unsafe")
        return normalized_account_id, normalized_platform, candidate

    async def get_account_context(
        self,
        account_id: int,
        profile_dir: str,
        proxy: dict = None,
        *,
        platform: str | None = None,
    ):
        if self._closed:
            raise RuntimeError("browser_pool_closed")
        account_id, platform, profile_path = self._account_profile_binding(
            account_id,
            profile_dir,
            platform,
        )
        # Resource creation and ``close`` share the initialization lock.
        # A shutdown waits for an in-flight launch to be registered before it
        # snapshots resources, while a launch queued behind shutdown observes
        # ``_closed`` and cannot target an already-stopped Playwright runtime.
        # The global order is ``_lock`` then ``_initialization_lock``. Browser
        # recycling already holds ``_lock`` while creating a replacement; the
        # same order here prevents an account-context launch racing recycle
        # from deadlocking AB-BA.
        async with self._lock:
            async with self._initialization_lock:
                await self._ensure_playwright_locked()
                await self.prune_persistent_contexts(reason="before_get_account_context", exclude_account_id=account_id)
                state = self._persistent_context_meta.get(account_id)
                if state:
                    if (
                        state.platform != platform
                        or os.path.normcase(
                            os.path.abspath(state.profile_dir)
                        )
                        != os.path.normcase(os.path.abspath(profile_path))
                    ):
                        raise RuntimeError(
                            "account_profile_context_binding_changed"
                        )
                    if await self._context_alive(state.context):
                        try:
                            await renew_account_profile_context_lease(
                                account_id,
                                platform,
                                state.lease_token,
                            )
                        except Exception:
                            await self._close_account_context_locked(
                                account_id,
                                reason="lease_renew_failed",
                            )
                            raise
                        state.touch()
                        structured_log(
                            "info",
                            "persistent_context_reused",
                            account_id=account_id,
                            use_count=state.use_count,
                            age_seconds=int((datetime.utcnow() - state.created_at).total_seconds()),
                        )
                        return state.context
                    closed = await self._close_account_context_locked(
                        account_id,
                        reason="stale_handle",
                    )
                    if not closed:
                        raise RuntimeError(
                            "persistent_context_stale_close_unconfirmed"
                        )
                await self._evict_if_over_capacity(exclude_account_id=account_id)
                return await self._create_account_context_locked(
                    account_id,
                    platform,
                    profile_path,
                    proxy,
                )

    async def _create_account_context_locked(
        self,
        account_id: int,
        platform: str,
        profile_path: Path,
        proxy: dict = None,
    ):
        lease_token = await acquire_account_profile_context_lease(
            account_id,
            platform,
        )
        profile_lock = None
        ctx = None
        pid = None
        launch_started = False
        try:
            # Database lease expiry is not evidence that an old Chromium
            # process stopped. The advisory lock is held for the actual
            # process/context lifetime and independently fences cleanup.
            profile_lock = acquire_account_profile_lock(
                account_id,
                platform,
                profiles_root=self.profiles_root,
            )
            profile_path.mkdir(parents=True, exist_ok=True)
            # Re-evaluate after mkdir so a concurrent boundary substitution
            # cannot silently redirect the persistent profile.
            self._account_profile_binding(
                account_id,
                str(profile_path),
                platform,
            )
            launch_started = True
            ctx = await self._browser_type.launch_persistent_context(
                user_data_dir=str(profile_path),
                headless=True,
                args=CHROMIUM_SANDBOX_ARGS,
                ignore_default_args=CHROMIUM_SANDBOX_IGNORED_DEFAULT_ARGS,
                proxy={"server": proxy["server"]} if proxy else None,
                viewport={"width": 1920, "height": 1080},
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
            )
            if hasattr(ctx, "_browser") and hasattr(ctx._browser, "_process"):
                pid = ctx._browser._process.pid
                self._persistent_pids[account_id] = pid
            state = PersistentContextState(
                context=ctx,
                profile_dir=str(profile_path),
                platform=platform,
                lease_token=lease_token,
                profile_lock=profile_lock,
                pid=pid,
            )
            state.touch()
            self._persistent_contexts[account_id] = ctx
            self._persistent_context_meta[account_id] = state
            structured_log(
                "info",
                "persistent_context_created",
                account_id=account_id,
                platform=platform,
                pid=pid,
                profile_dir=str(profile_path),
            )
            return ctx
        except BaseException:
            # A launch coroutine raising or being cancelled after invocation
            # does not prove that Chromium never started. In that case retain
            # the flock until Playwright shutdown succeeds or the OS closes
            # this Worker process.
            context_close_confirmed = (
                ctx is None and not launch_started
            )
            if ctx is not None:
                try:
                    await ctx.close()
                    context_close_confirmed = True
                except Exception as close_exc:
                    structured_log(
                        "error",
                        "persistent_context_launch_cleanup_failed",
                        account_id=account_id,
                        platform=platform,
                        exception=close_exc,
                    )
                    if pid:
                        context_close_confirmed = (
                            await self._kill_browser_tree(pid)
                        )
            if context_close_confirmed:
                try:
                    await release_account_profile_context_lease(
                        account_id,
                        platform,
                        lease_token,
                    )
                except Exception as release_exc:
                    structured_log(
                        "error",
                        "persistent_context_launch_lease_release_failed",
                        account_id=account_id,
                        platform=platform,
                        exception=release_exc,
                    )
                if profile_lock is not None:
                    try:
                        profile_lock.release()
                    except Exception as release_exc:
                        structured_log(
                            "error",
                            "persistent_context_profile_lock_release_failed",
                            account_id=account_id,
                            platform=platform,
                            exception=release_exc,
                        )
            elif profile_lock is not None:
                self._quarantined_profile_locks[
                    (account_id, lease_token)
                ] = profile_lock
                structured_log(
                    "error",
                    "persistent_context_profile_lock_quarantined",
                    account_id=account_id,
                    platform=platform,
                )
            raise

    async def get_transient_context(self, profile_dir: str, proxy: dict = None):
        async with self._initialization_lock:
            await self._ensure_playwright_locked()
            profile_path = Path(profile_dir)
            if not profile_path.is_absolute():
                raise ValueError("transient_context_profile_not_absolute")
            profile_path.mkdir(parents=True, exist_ok=True)
            context = await self._browser_type.launch_persistent_context(
                user_data_dir=str(profile_path),
                headless=True,
                args=CHROMIUM_SANDBOX_ARGS,
                ignore_default_args=CHROMIUM_SANDBOX_IGNORED_DEFAULT_ARGS,
                proxy={"server": proxy["server"]} if proxy else None,
                viewport={"width": 1280, "height": 900},
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
            )
            self._transient_contexts[id(context)] = context
            self._transient_profile_dirs[id(context)] = os.path.normcase(
                os.path.abspath(profile_path)
            )
            return context

    async def _close_transient_context_locked(
        self,
        context,
        *,
        reason: str,
    ) -> bool:
        context_id = id(context)
        tracked = self._transient_contexts.get(context_id)
        if tracked is None:
            return True
        try:
            await tracked.close()
        except Exception as exc:
            # Keep both the handle and its path binding. Cleanup must not delete
            # the profile or ACK the login message while close is unconfirmed.
            structured_log(
                "error",
                "transient_context_close_failed",
                reason=reason,
                exception=exc,
            )
            return False
        self._transient_contexts.pop(context_id, None)
        self._transient_profile_dirs.pop(context_id, None)
        return True

    async def close_transient_context(
        self,
        context,
        *,
        reason: str = "consumer_release",
    ) -> bool:
        """Close and forget a tracked short-lived context exactly once."""

        async with self._initialization_lock:
            return await self._close_transient_context_locked(
                context,
                reason=reason,
            )

    async def close_transient_contexts_for_profile(
        self,
        profile_dir: str,
        *,
        reason: str = "profile_cleanup",
    ) -> bool:
        """Close every locally tracked owner of one exact transient profile."""

        target = Path(profile_dir)
        if not target.is_absolute():
            raise ValueError("transient_context_profile_not_absolute")
        normalized_target = os.path.normcase(os.path.abspath(target))
        async with self._initialization_lock:
            contexts = [
                context
                for context_id, context in self._transient_contexts.items()
                if self._transient_profile_dirs.get(context_id)
                == normalized_target
            ]
            all_closed = True
            for context in contexts:
                if not await self._close_transient_context_locked(
                    context,
                    reason=reason,
                ):
                    all_closed = False
            return all_closed

    async def close_account_context(self, account_id: int, reason: str = "manual_close"):
        async with self._lock:
            return await self._close_account_context_locked(
                account_id,
                reason,
            )

    async def _close_account_context_locked(self, account_id: int, reason: str):
        ctx = self._persistent_contexts.get(account_id)
        state = self._persistent_context_meta.get(account_id)
        if ctx is None and state is not None:
            ctx = state.context
        pid = self._persistent_pids.get(account_id) or (state.pid if state else None)
        if not ctx:
            if state is not None:
                try:
                    await release_account_profile_context_lease(
                        account_id,
                        state.platform,
                        state.lease_token,
                    )
                except Exception as exc:
                    structured_log(
                        "error",
                        "persistent_context_lease_release_failed",
                        account_id=account_id,
                        platform=state.platform,
                        reason=reason,
                        exception=exc,
                    )
                try:
                    state.profile_lock.release()
                except Exception as exc:
                    structured_log(
                        "error",
                        "persistent_context_profile_lock_release_failed",
                        account_id=account_id,
                        platform=state.platform,
                        reason=reason,
                        exception=exc,
                    )
            self._persistent_context_meta.pop(account_id, None)
            self._persistent_pids.pop(account_id, None)
            return True
        context_closed = False
        try:
            await ctx.close()
            context_closed = True
        except Exception as exc:
            structured_log("error", "persistent_context_close_failed", account_id=account_id, reason=reason, exception=exc)
        process_stopped = False
        if pid:
            process_stopped = await self._kill_browser_tree(pid)
        if not context_closed and not process_stopped:
            structured_log(
                "error",
                "persistent_context_close_unconfirmed",
                account_id=account_id,
                reason=reason,
                pid=pid,
            )
            return False
        self._persistent_contexts.pop(account_id, None)
        self._persistent_context_meta.pop(account_id, None)
        self._persistent_pids.pop(account_id, None)
        if state is not None:
            try:
                await release_account_profile_context_lease(
                    account_id,
                    state.platform,
                    state.lease_token,
                )
            except Exception as exc:
                # The browser is already confirmed closed. Leaving the durable
                # lease to expire is safe and intentionally delays cleanup.
                structured_log(
                    "error",
                    "persistent_context_lease_release_failed",
                    account_id=account_id,
                    platform=state.platform,
                    reason=reason,
                    exception=exc,
                )
            try:
                state.profile_lock.release()
            except Exception as exc:
                structured_log(
                    "error",
                    "persistent_context_profile_lock_release_failed",
                    account_id=account_id,
                    platform=state.platform,
                    reason=reason,
                    exception=exc,
                )
        structured_log(
            "info",
            "persistent_context_closed",
            account_id=account_id,
            platform=state.platform if state is not None else None,
            reason=reason,
            pid=pid,
        )
        return True

    async def _forget_persistent_context(self, account_id: int, reason: str):
        closed = await self._close_account_context_locked(
            account_id,
            reason,
        )
        if not closed:
            raise RuntimeError(
                "persistent_context_forget_close_unconfirmed"
            )

    async def prune_persistent_contexts(self, reason: str = "scheduled", exclude_account_id: int | None = None) -> dict[str, int]:
        now = datetime.utcnow()
        closed = 0
        scanned = 0
        for account_id, state in list(self._persistent_context_meta.items()):
            if exclude_account_id is not None and account_id == exclude_account_id:
                continue
            scanned += 1
            age = now - state.created_at
            idle = now - state.last_used_at
            has_open_pages = await self._context_has_open_pages(
                state.context
            )
            close_reason = None
            if not has_open_pages and age >= self.persistent_context_ttl:
                close_reason = "ttl"
            elif (
                not has_open_pages
                and idle >= self.persistent_context_idle
            ):
                close_reason = "idle"
            if close_reason is not None:
                if await self._close_account_context_locked(
                    account_id,
                    reason=close_reason,
                ):
                    closed += 1
                continue
            try:
                await renew_account_profile_context_lease(
                    account_id,
                    state.platform,
                    state.lease_token,
                )
            except Exception as exc:
                structured_log(
                    "error",
                    "persistent_context_lease_renew_failed",
                    account_id=account_id,
                    platform=state.platform,
                    reason=reason,
                    exception=exc,
                )
                if await self._close_account_context_locked(
                    account_id,
                    reason="lease_renew_failed",
                ):
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
                raise BrowserPoolCapacityExceeded(
                    "persistent_context_capacity_exhausted"
                )
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
        renew_interval = min(
            max(5, int(interval_seconds)),
            max(5, PROFILE_CONTEXT_LEASE_TTL_SECONDS // 3),
        )
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
            try:
                await asyncio.wait_for(
                    shutdown_event.wait(),
                    timeout=renew_interval,
                )
            except asyncio.TimeoutError:
                pass

    async def close(self):
        """Close only resources that were actually initialized."""

        async with self._initialization_lock:
            if self._closed:
                return
            self._closed = True
        for browser_id in list(self._browsers):
            await self.close_browser(browser_id)
        for account_id in list(self._persistent_contexts):
            await self.close_account_context(
                account_id,
                reason="worker_shutdown",
            )
        for context in list(self._transient_contexts.values()):
            await self.close_transient_context(
                context,
                reason="worker_shutdown",
            )
        playwright_stopped = self._playwright is None
        try:
            if self._playwright is not None:
                await self._playwright.stop()
                playwright_stopped = True
        except Exception as exc:
            # Playwright transport teardown can fail after the browser process
            # has already exited.  The Worker still must release DB/Redis and
            # must never retain a half-live runtime handle for a second close.
            structured_log(
                "error",
                "playwright_stop_failed",
                exception=exc,
            )
        finally:
            self._playwright = None
            self._browser_type = None
            self._shared_browsers_initialized = False
        if playwright_stopped:
            for profile_lock in tuple(
                self._quarantined_profile_locks.values()
            ):
                try:
                    profile_lock.release()
                except Exception as exc:
                    structured_log(
                        "error",
                        "quarantined_profile_lock_release_failed",
                        account_id=profile_lock.account_id,
                        platform=profile_lock.platform,
                        exception=exc,
                    )
            self._quarantined_profile_locks.clear()
        elif self._quarantined_profile_locks:
            structured_log(
                "error",
                "quarantined_profile_locks_retained",
                count=len(self._quarantined_profile_locks),
            )

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
        return await asyncio.to_thread(
            self._kill_browser_tree_sync,
            pid,
        )

    def _kill_browser_tree_sync(self, pid: int) -> bool:
        try:
            parent = psutil.Process(pid)
            children = parent.children(recursive=True)
            processes = [*children, parent]
            for process in processes:
                try:
                    process.kill()
                except psutil.NoSuchProcess:
                    continue
                except Exception:
                    return False
            _gone, alive = psutil.wait_procs(processes, timeout=5)
            return not alive
        except psutil.NoSuchProcess:
            return True
        except Exception as exc:
            structured_log("error", "browser_tree_kill_failed", pid=pid, exception=exc)
            return False


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default
