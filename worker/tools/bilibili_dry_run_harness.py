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
import hashlib
import importlib.util
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


def build_reviewed_bilibili_action_plan(
    required_actions=None,
    *,
    follow_handle: str = "@ASUS华硕官方UP",
) -> dict:
    """Small valid Action Plan v2 shared by settlement-oriented fakes."""

    actions = list(required_actions or DRY_RUN_PHASES)
    all_payloads = {
        "followed": {"target_handle": follow_handle},
        "liked": {},
        "commented": {
            "text": "reviewed comment",
            "topic_tags": [],
            "mentions": [],
            "media_refs": [],
        },
        "reposted": {
            "text": "reviewed repost",
            "topic_tags": [],
            "mentions": [],
        },
    }
    plan = {
        "version": 2,
        "platform": "bilibili",
        "is_lottery": True,
        "rule_snapshot_id": 101,
        "rule_hash": "a" * 64,
        "execution_path_id": "bilibili_api_v2",
        "required_actions": actions,
        "action_payloads": {action: all_payloads[action] for action in actions},
        "content_requirements": {
            "follow_targets": [follow_handle] if "followed" in actions else [],
            "commented": {"topic_tags": [], "mentions": []},
            "reposted": {"topic_tags": [], "mentions": []},
        },
        "executable": True,
        "review_required": False,
        "reviewed_by": "operator-1",
        "rule_complete_confirmed": True,
    }
    canonical = json.dumps(
        plan,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    plan["plan_hash"] = hashlib.sha256(canonical).hexdigest()
    return plan


def stub_playwright() -> None:
    """Make `app.browser_pool` importable without the real Playwright package.

    browser_pool imports playwright at module load; dry-run never opens a
    browser, so a stub is enough to pull in the real worker executor.
    """
    if "playwright" in sys.modules or _module_available("playwright.async_api"):
        return
    pw = types.ModuleType("playwright")
    aio = types.ModuleType("playwright.async_api")
    aio.Page = object
    aio.async_playwright = lambda *a, **k: None
    pw.async_api = aio
    sys.modules["playwright"] = pw
    sys.modules["playwright.async_api"] = aio


def stub_httpx() -> None:
    """Make Bilibili client types importable for pure tests without httpx."""

    if "httpx" in sys.modules or _module_available("httpx"):
        return
    httpx = types.ModuleType("httpx")
    httpx.AsyncBaseTransport = object
    httpx.AsyncClient = object
    httpx.TransportError = RuntimeError
    sys.modules["httpx"] = httpx


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return name in sys.modules


def stub_worker_runtime_dependencies() -> None:
    """Keep ownership/claim tests pure when optional service packages are absent.

    The tests replace the actual database, Redis, browser pool and credential
    vault before exercising any behavior.  These import-only shims therefore
    expose no network, filesystem or secret side effects.
    """

    if "app.db" not in sys.modules and (
        not _module_available("databases") or not _module_available("redis.asyncio")
    ):
        db_module = types.ModuleType("app.db")
        db_module.database = object()
        db_module.redis = object()

        async def execute_affected_rows(query, values=None, *, db=None):
            target = db or db_module.database
            async with target.transaction():
                await target.execute(query, values)
                row = await target.fetch_one("SELECT ROW_COUNT() AS affected")
            if row is None:
                raise RuntimeError("database_affected_row_count_unavailable")
            affected = int(row["affected"])
            if affected < 0:
                raise RuntimeError("database_affected_row_count_invalid")
            return affected

        db_module.execute_affected_rows = execute_affected_rows
        sys.modules["app.db"] = db_module

    if "app.browser_pool" not in sys.modules and not _module_available("psutil"):
        browser_pool = types.ModuleType("app.browser_pool")
        browser_pool.BrowserPool = object
        sys.modules["app.browser_pool"] = browser_pool

    if "app.utils.crypto" not in sys.modules and (
        not _module_available("cryptography")
        or not _module_available("pydantic_settings")
    ):
        crypto = types.ModuleType("app.utils.crypto")
        crypto.CREDENTIAL_AAD = "dpms:account-credential"

        class ImportOnlyCookieVault:
            @staticmethod
            def decrypt(value, *, aad=None):
                del aad
                return value.decode("utf-8") if isinstance(value, bytes) else str(value)

        crypto.cookie_vault = ImportOnlyCookieVault()
        sys.modules["app.utils.crypto"] = crypto


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

    def __init__(self, task_mode: str = "dry_run"):
        self.phases: list[str] = []
        self.bilibili_action_ledger: list[dict] = []
        self.writes: int = 0
        self.task_id: str | None = None
        self.task_status = "queued"
        self.task_mode = task_mode
        self.task_worker_id: str | None = None
        self.task_reconciliation_required = 0
        self.account_lease_id = "lease-1"
        self.account_lease_generation = 1
        self.account_lease_operation_kind = task_mode
        self.account_lease_released_at = None
        self.external_action_intents: list[dict] = []
        self.task_execution_intent_binding: dict | None = None
        self._last_affected_rows = 1
        self.account_id = 9001
        self.account_status = "ready"
        self.lottery_id = 7001
        self.lottery_status = "claimed"
        self.lottery_execution_lock: str | None = None
        self.lottery_platform = "bilibili"
        self.lottery_raw_url = "https://t.bilibili.com/123456789"
        self.lottery_canonical_url = self.lottery_raw_url
        self.lottery_action_plan = build_reviewed_bilibili_action_plan()
        self.task_action_plan_hash = self.lottery_action_plan["plan_hash"]
        self.selector_config = {}
        self.screenshot_path = None
        self._encrypted_account_credential = None

    async def fetch_one(self, query, values=None):
        values = values or {}
        if "FROM task_execution_intent_bindings" in query:
            return (
                dict(self.task_execution_intent_binding)
                if self.task_execution_intent_binding is not None
                else None
            )
        if "SELECT ROW_COUNT()" in query:
            return {"affected": self._last_affected_rows}
        if "FROM adapter_selector_configs" in query:
            return {"config_json": json.dumps(self.selector_config, ensure_ascii=False)}
        if "FROM accounts" in query:
            if self._encrypted_account_credential is None:
                from app.utils.crypto import CREDENTIAL_AAD, cookie_vault

                self._encrypted_account_credential = cookie_vault.encrypt(
                    FAKE_ACCOUNT_CREDENTIAL,
                    aad=CREDENTIAL_AAD,
                )
            return {
                "id": self.account_id,
                "status": self.account_status,
                "daily_task_count": 0,
                "encrypted_credential": self._encrypted_account_credential,
                "platform": self.lottery_platform,
                "execution_revision": 1,
            }
        if "FROM external_action_intents" in query:
            intent_id = str(values.get("intent_id") or "")
            task_id = str(values.get("task_id") or "")
            action = str(values.get("action") or "")
            for row in self.external_action_intents:
                if intent_id and str(row.get("intent_id") or "") == intent_id:
                    return dict(row)
                if (
                    task_id
                    and action
                    and str(row.get("task_id") or "") == task_id
                    and str(row.get("action") or "") == action
                ):
                    return dict(row)
            return None
        if "FROM task_runs tr" in query:
            task_id = str(values.get("task_id") or self.task_id or "task-1")
            return {
                "task_id": task_id,
                "account_id": self.account_id,
                "lottery_id": self.lottery_id,
                "task_status": self.task_status,
                "worker_id": self.task_worker_id,
                "account_lease_id": self.account_lease_id,
                "account_lease_generation": self.account_lease_generation,
                "reconciliation_required": self.task_reconciliation_required,
                "execution_intent_kind": (
                    self.task_execution_intent_binding.get(
                        "binding_kind"
                    )
                    if self.task_execution_intent_binding
                    else None
                ),
                "lease_id": self.account_lease_id,
                "lease_generation": self.account_lease_generation,
                "operation_kind": self.account_lease_operation_kind,
                "owner_id": task_id,
                "lease_task_id": task_id,
                "lease_active": 1,
                "lease_unreleased": 0 if self.account_lease_released_at else 1,
                "lease_latest_generation": 1,
                "active_account_lease_count": 0 if self.account_lease_released_at else 1,
            }
        if "FROM account_operation_leases" in query:
            return {
                "lease_id": self.account_lease_id,
                "account_id": self.account_id,
                "generation": self.account_lease_generation,
                "operation_kind": self.account_lease_operation_kind,
                "owner_id": self.task_id,
                "task_id": self.task_id,
                "lease_active": 1,
                "lease_unreleased": 0 if self.account_lease_released_at else 1,
                "lease_latest_generation": 1,
                "active_account_lease_count": 0 if self.account_lease_released_at else 1,
                "released_at": self.account_lease_released_at,
            }
        if "FROM lotteries" in query:
            return {
                "id": self.lottery_id,
                "status": self.lottery_status,
                "execution_lock": self.lottery_execution_lock,
                "platform": self.lottery_platform,
                "raw_url": self.lottery_raw_url,
                "canonical_url": self.lottery_canonical_url,
                "action_plan": json.dumps(self.lottery_action_plan, ensure_ascii=False),
                "action_plan_hash": self.lottery_action_plan["plan_hash"],
            }
        if "FROM task_runs" in query:
            if self.task_id is None:
                self.task_id = str(values.get("task_id") or values.get("tid") or "task-1")
                self.lottery_execution_lock = self.task_id
            return {
                "task_id": self.task_id,
                "account_id": self.account_id,
                "lottery_id": self.lottery_id,
                "status": self.task_status,
                "task_mode": self.task_mode,
                "worker_id": self.task_worker_id,
                "screenshot_path": self.screenshot_path,
                "action_plan_hash": self.task_action_plan_hash,
                "account_lease_id": self.account_lease_id,
                "account_lease_generation": self.account_lease_generation,
                "reconciliation_required": self.task_reconciliation_required,
            }
        return None

    async def fetch_all(self, query, values=None):
        if "FROM external_action_intents" in str(query):
            return [dict(row) for row in self.external_action_intents]
        if "FROM bilibili_action_ledger" in str(query):
            task_id = str((values or {}).get("task_id") or "")
            return [
                {
                    key: row.get(key)
                    for key in (
                        "account_id",
                        "lottery_id",
                        "dynamic_id",
                        "action",
                        "phase",
                    )
                }
                for row in self.bilibili_action_ledger
                if str(row.get("task_id") or "") == task_id
                and int(row.get("ok") or 0) == 1
                and str(row.get("outcome") or "") == "ok"
            ]
        return []

    async def execute(self, query, values=None):
        values = values or {}
        self.writes += 1
        self._last_affected_rows = 1
        if "INSERT INTO external_action_intents" in query:
            self.external_action_intents.append(
                {
                    **dict(values),
                    "status": "prepared",
                    "effect_certainty": "not_started",
                    "outcome": None,
                    "reconciliation_note": None,
                }
            )
        elif "UPDATE external_action_intents" in query:
            intent_id = str(values.get("intent_id") or "")
            row = next(
                (
                    item
                    for item in self.external_action_intents
                    if str(item.get("intent_id") or "") == intent_id
                ),
                None,
            )
            if row is None:
                self._last_affected_rows = 0
            elif "SET status = 'started'" in query:
                row.update(status="started", effect_certainty="unknown")
            elif "SET status = 'unknown'" in query:
                row.update(
                    status="unknown",
                    effect_certainty="unknown",
                    outcome="unknown",
                    reconciliation_note=(
                        row.get("reconciliation_note") or values.get("note")
                    ),
                )
            elif "SET status = 'prepared'" in query:
                row.update(
                    status="prepared",
                    effect_certainty="not_started",
                    attempt_no=values.get("attempt_no"),
                    outcome=None,
                    reconciliation_note=None,
                )
            elif "SET status = :status" in query:
                row.update(
                    status=values.get("status"),
                    effect_certainty=values.get("effect_certainty"),
                    outcome=values.get("outcome"),
                    remote_ref=values.get("remote_ref"),
                    error_message=values.get("error_message"),
                )
        if "INSERT INTO task_phases" in query and values:
            self.phases.append(values.get("phase"))
        if "INSERT INTO bilibili_action_ledger" in query and values:
            self.bilibili_action_ledger.append(dict(values))
        if "UPDATE task_runs" in query and "SET status = 'running'" in query:
            self.task_status = "running"
            self.task_worker_id = values.get("worker_id")
        elif "UPDATE task_runs" in query and "SET status = :status" in query:
            self.task_status = values.get("status")
            if values.get("reconciliation_required"):
                self.task_reconciliation_required = 1
            if "worker_id = NULL" in query:
                self.task_worker_id = None
            self.screenshot_path = values.get("screenshot_path")
        elif "UPDATE task_runs" in query and "reconciliation_required = 1" in query:
            self.task_reconciliation_required = 1
        elif "UPDATE task_runs SET screenshot_path = :path" in query:
            self.screenshot_path = values.get("path")
        if "UPDATE lotteries SET status = 'running'" in query:
            self.lottery_status = "running"
        elif "UPDATE lotteries SET status = :status" in query:
            self.lottery_status = values.get("status")
            self.lottery_execution_lock = None
        if (
            "UPDATE accounts SET status = :account_status" in query
            and self.account_status == values.get("expected_account_status")
        ):
            self.account_status = values.get("account_status")
        elif "UPDATE accounts SET status = :account_status" in query:
            self._last_affected_rows = 0
        elif "UPDATE accounts SET status = 'cooling'" in query and self.account_status == "executing":
            self.account_status = "cooling"
        elif "UPDATE accounts" in query and "SET status = 'executing'" in query:
            self.account_status = "executing"
        elif "UPDATE accounts SET status = 'ready'" in query and self.account_status == "executing":
            self.account_status = "ready"
        if "UPDATE account_operation_leases" in query and "SET released_at = NOW()" in query:
            if (
                values.get("lease_id") == self.account_lease_id
                and int(values.get("generation") or 0) == self.account_lease_generation
                and values.get("operation_kind")
                == self.account_lease_operation_kind
            ):
                self.account_lease_released_at = "now"
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
        self.main_frame = object()
        self.route_handler = None
        self._visible_selectors = visible_selectors
        self._body_text = body_text
        self.last_screenshot_options = None

    async def route(self, pattern, handler):
        self.route_handler = handler

    async def goto(self, url, wait_until=None, timeout=None):
        self.url = url

    async def wait_for_timeout(self, ms):
        return None

    async def evaluate(self, script):
        return {"width": 1280, "height": 2400}

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(selector, self._visible_selectors, self._body_text)

    async def screenshot(self, path=None, full_page=None, clip=None):
        # capture_shadow_screenshot reads the file back to hash it, so this
        # has to actually land on disk, not just record the call.
        self.last_screenshot_options = {
            "path": path,
            "full_page": full_page,
            "clip": clip,
        }
        payload = b"\x89PNG\r\n\x1a\nfake-shadow-run-screenshot"
        if path is not None:
            target = Path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
        return payload

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

    async def get_account_context(
        self,
        account_id,
        profile_dir,
        proxy=None,
        *,
        platform=None,
    ) -> FakeContext:
        return self._context
