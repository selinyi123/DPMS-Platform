"""Shared building blocks for the Bilibili dry-run smoke check.

Used by both the manual smoke script (smoke_bilibili_dry_run.py) and its CI
regression test (tests/test_bilibili_dry_run_smoke.py), so the dispatch
message shape and the fake DB can't quietly drift into two different things
that happen to share a name.

Redis is deliberately NOT shared here: the manual script talks to a real
local Redis (so a human can see the risk-window/notify-stream side effects),
while the CI test fakes it (so the suite never depends on a running Redis).
That split is intentional, not an oversight.
"""

import json
import sys
import types
from pathlib import Path

WORKER_ROOT = Path(__file__).resolve().parents[1]
CORE_ROOT = WORKER_ROOT.parent / "core"

DRY_RUN_PHASES = ["followed", "liked", "commented", "reposted"]
COMPLETED_PHASES = DRY_RUN_PHASES + ["completed"]


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
    config = json.loads((CORE_ROOT / "adapter_selectors.example.json").read_text())
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
    """Just enough of `databases.Database` for the dry-run path.

    The dry-run executor only *reads* the account row (to confirm it is
    ready); every other statement is a write we simply record. Phase inserts
    are picked out into `.phases` so callers can assert the task progressed.
    """

    def __init__(self):
        self.phases: list[str] = []
        self.writes: int = 0

    async def fetch_one(self, query, values=None):
        if "FROM accounts" in query:
            return {"status": "ready", "daily_task_count": 0, "encrypted_credential": "[]"}
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
