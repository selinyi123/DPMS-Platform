"""Shared building blocks for the Bilibili dry-run and shadow-run smoke checks.

Used by the manual smoke scripts (smoke_bilibili_dry_run.py,
smoke_bilibili_shadow_run.py) and their CI regression tests
(tests/test_bilibili_dry_run_smoke.py, tests/test_bilibili_shadow_run_smoke.py),
so the dispatch message shape and the fakes can't quietly drift into
different things that happen to share a name.

Redis is deliberately NOT shared here: the manual scripts talk to a real
local Redis (so a human can see the risk-window/notify-stream side effects),
while the CI tests fake it (so the suite never depends on a running Redis).
That split is intentional, not an oversight.

The fake account credential below is a single throwaway cookie (not an
empty list): dry-run never reads it, but shadow-run's
`prepare_account_login` -> `inject_account_cookies` rejects an empty cookie
list, so it has to be non-empty for both paths to share one FakeDatabase.
"""

import json
import sys
import types
from pathlib import Path

WORKER_ROOT = Path(__file__).resolve().parents[1]
CORE_ROOT = WORKER_ROOT.parent / "core"

DRY_RUN_PHASES = ["followed", "liked", "commented", "reposted"]
COMPLETED_PHASES = DRY_RUN_PHASES + ["completed"]

FAKE_ACCOUNT_CREDENTIAL = json.dumps(
    [{"name": "SESSDATA", "value": "fake-session", "domain": ".bilibili.com"}]
)
BENIGN_PAGE_BODY = "欢迎参与本次抽奖活动，关注+点赞+转发+评论即可参与。"


def stub_playwright() -> None:
    """Make `app.browser_pool` importable without the real Playwright package.

    browser_pool imports playwright at module load; dry-run never opens a
    browser, so a stub is enough to pull in the real worker executor.
    """
    if "playwright" in sys.modules:
        return
    pw = types.ModuleType("playwright")
    aio = types.ModuleType("playwright.async_api")
    aio.Page = object
    aio.async_playwright = lambda *a, **k: None
    pw.async_api = aio
    sys.modules["playwright"] = pw
    sys.modules["playwright.async_api"] = aio


def load_bilibili_example_selectors() -> dict:
    config = json.loads((CORE_ROOT / "adapter_selectors.example.json").read_text(encoding="utf-8"))
    return config["bilibili"]


def build_task_message(*, task_id, account_id, lottery_id, platform, raw_url,
                        canonical_url, task_mode, dry_run, platform_selectors, action_plan):
    """Mirror of core/app/services/outbox.py:build_lottery_task_message.

    Reproduced here rather than imported because core and worker each expose
    their own `app` package; importing core's would shadow the worker's on
    sys.path. Every value is coerced to the str the Redis stream requires.
    """
    selectors = platform_selectors if isinstance(platform_selectors, dict) else {}
    plan = action_plan if isinstance(action_plan, (dict, list)) else {}
    return {
        "task_id": str(task_id),
        "account_id": str(account_id),
        "lottery_id": str(lottery_id),
        "platform": platform or "",
        "raw_url": raw_url or "",
        "canonical_url": canonical_url or "",
        "dry_run": "0" if not dry_run else "1",
        "mode": task_mode,
        "selector_config": json.dumps(selectors, ensure_ascii=False),
        "action_plan": json.dumps(plan, ensure_ascii=False),
    }


class FakeDatabase:
    """Just enough of `databases.Database` for the dry-run and shadow-run paths.

    Both executors only *read* the account row (to confirm it is ready and,
    for shadow-run, to get a cookie to inject); every other statement is a
    write we simply record. Phase inserts are picked out into `.phases` so
    callers can assert the task progressed.
    """

    def __init__(self):
        self.phases: list[str] = []
        self.writes: int = 0

    async def fetch_one(self, query, values=None):
        if "FROM accounts" in query:
            return {"status": "ready", "daily_task_count": 0, "encrypted_credential": FAKE_ACCOUNT_CREDENTIAL}
        if "FROM task_runs" in query:
            return {"screenshot_path": None}
        return None

    async def fetch_all(self, query, values=None):
        return []

    async def execute(self, query, values=None):
        self.writes += 1
        if "INSERT INTO task_phases" in query and values:
            self.phases.append(values.get("phase"))
        return None

    def transaction(self):
        db = self

        class _Tx:
            async def __aenter__(self):
                return db

            async def __aexit__(self, *exc):
                return False

        return _Tx()


def visible_selectors_for_all_phases(platform_selectors: dict) -> set[str]:
    """One real selector per phase from the example config, treated as visible.

    Lets the shadow-run fake page report a plausible mix of visible/missing
    selectors instead of either "everything visible" or "nothing visible",
    which would never exercise `first_visible_selector`'s loop-and-fall-through.
    """
    visible = set()
    for phase in ("followed", "liked", "reposted"):
        candidates = platform_selectors.get(phase)
        if isinstance(candidates, list) and candidates:
            visible.add(candidates[0])
    commented = platform_selectors.get("commented")
    if isinstance(commented, dict):
        for key in ("input", "submit"):
            candidates = commented.get(key)
            if isinstance(candidates, list) and candidates:
                visible.add(candidates[0])
    return visible


class FakeLocator:
    """Just enough of Playwright's `Locator` for shadow-run's read-only probing.

    Used both as `page.locator(sel).first` (selector visibility probing in
    task_runner.first_visible_selector) and as the bare `page.locator("body")`
    safety.detect_page_risk calls directly without `.first`, so both
    `is_visible` and `text_content` live on the same object.
    """

    def __init__(self, selector: str, visible_selectors: set[str], body_text: str):
        self.selector = selector
        self._visible_selectors = visible_selectors
        self._body_text = body_text

    @property
    def first(self):
        return self

    async def is_visible(self, timeout=None) -> bool:
        return self.selector in self._visible_selectors

    async def text_content(self, timeout=None) -> str:
        return self._body_text


class FakePage:
    """Just enough of Playwright's `Page` for the shadow-run smoke path.

    Covers `goto`/`wait_for_timeout`/`locator`/`screenshot`/`close` -- the
    only Page surface execute_shadow_run and detect_page_risk touch.
    """

    def __init__(self, visible_selectors: set[str], body_text: str = BENIGN_PAGE_BODY):
        self.url = ""
        self._visible_selectors = visible_selectors
        self._body_text = body_text

    async def goto(self, url, wait_until=None, timeout=None):
        self.url = url

    async def wait_for_timeout(self, ms):
        return None

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(selector, self._visible_selectors, self._body_text)

    async def screenshot(self, path=None, full_page=None):
        # capture_shadow_screenshot reads the file back to hash it, so this
        # has to actually land on disk, not just record the call.
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"\x89PNG\r\n\x1a\nfake-shadow-run-screenshot")

    async def close(self):
        return None


class FakeContext:
    """Just enough of Playwright's `BrowserContext` for cookie injection + page creation."""

    def __init__(self, page: FakePage):
        self._page = page
        self.cookies_added: list[dict] = []

    async def add_cookies(self, cookies):
        self.cookies_added.extend(cookies)

    async def new_page(self) -> FakePage:
        return self._page


class FakePool:
    """Stand-in for `BrowserPool`: hands back one fixed fake context per account."""

    def __init__(self, page: FakePage):
        self._context = FakeContext(page)

    async def get_account_context(self, account_id, profile_dir, proxy=None) -> FakeContext:
        return self._context
