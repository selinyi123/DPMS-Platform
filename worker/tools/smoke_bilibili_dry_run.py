"""End-to-end dry-run smoke test for the Bilibili lottery task.

Runs a *real* Bilibili lottery task through the genuine worker executor
(`execute_task_with_phases` in dry-run mode) so you can watch the first task
flow through the whole pipeline — dispatch message -> queue shape -> adapter
selection -> per-phase execution -> completion — WITHOUT touching the live
Bilibili site or needing a real account.

Why dry-run: dry-run exercises the same dispatch/queue/worker/phase machinery
as a real run but `execute_dry_run` performs no browser actions, so it is the
safe "try it first" path. Real participation additionally needs a calibrated
account, a recent probe + shadow-run, a reviewed action plan, and the global
real-run switch — none of which this harness fakes.

Infra: uses a real local Redis (the risk window + notify stream are genuine)
and an in-memory stand-in for MySQL, so it runs anywhere without docker.

Run:
    ENCRYPTION_KEY=$(python -c "import base64,os;print(base64.b64encode(os.urandom(32)).decode())") \
    REDIS_URL=redis://localhost:6379/0 \
    python tools/smoke_bilibili_dry_run.py
"""

import asyncio
import json
import os
import sys
import types
import uuid
from pathlib import Path

# --- Make the worker package importable and stub Playwright -----------------
# browser_pool imports playwright at module load; dry-run never uses a browser,
# so a stub is enough to import the executor.
# Only the worker package goes on the path. Core and worker both expose an
# `app` package, so importing core here would shadow the worker's `app`. The
# one thing we need from core — the dispatch message shape — is reproduced
# inline by `build_task_message` below (kept byte-for-byte in step with
# core/app/services/outbox.py:build_lottery_task_message).
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
CORE = ROOT.parent / "core"

_pw = types.ModuleType("playwright")
_aio = types.ModuleType("playwright.async_api")
_aio.Page = object
_aio.async_playwright = lambda *a, **k: None
_pw.async_api = _aio
sys.modules.setdefault("playwright", _pw)
sys.modules.setdefault("playwright.async_api", _aio)

os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("DATABASE_URL", "mysql+aiomysql://u:p@localhost:3306/lottery")


# --- In-memory stand-in for the MySQL `database` ----------------------------
class FakeDatabase:
    """Just enough of `databases.Database` for the dry-run path.

    The dry-run executor only *reads* the account row (to confirm it is ready);
    every other statement is a write we simply record. Phase inserts are picked
    out so we can show the task progressing.
    """

    def __init__(self):
        self.phases: list[str] = []
        self.writes: int = 0

    async def fetch_one(self, query, values=None):
        if "FROM accounts" in query:
            # A ready, credentialed account well under the daily limit.
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


def build_task_message(*, task_id, account_id, lottery_id, platform, raw_url,
                       canonical_url, task_mode, dry_run, platform_selectors, action_plan):
    """Mirror of core/app/services/outbox.py:build_lottery_task_message.

    Reproduced here so the harness doesn't have to import the core `app`
    package (which would shadow the worker's). Every value is coerced to the
    str/int the Redis stream requires.
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


async def main() -> int:
    # Wire the fake DB into every module that bound `database` at import time.
    import app.db as db

    fake = FakeDatabase()
    db.database = fake

    from app import task_runner, safety
    from app.event_store import service as event_service

    task_runner.database = fake
    safety.database = fake
    event_service.database = fake

    # Real local Redis (shared singleton) for the risk window + notify stream.
    await db.redis.initialize()
    try:
        await db.redis.delete("risk_window:9001")
    except Exception:
        pass

    from app.adapters.registry import get_adapter

    # Build the dispatch message exactly as core's dispatcher would, for a
    # Bilibili lottery, using the shipped example selectors + a reviewed plan.
    selectors = json.loads((CORE / "adapter_selectors.example.json").read_text())["bilibili"]
    action_plan = {"required_actions": ["followed", "liked", "commented", "reposted"]}
    task_id = str(uuid.uuid4())
    message = build_task_message(
        task_id=task_id,
        account_id=9001,
        lottery_id=7001,
        platform="bilibili",
        raw_url="https://t.bilibili.com/123456789",
        canonical_url="https://t.bilibili.com/123456789",
        task_mode="dry_run",
        dry_run=True,
        platform_selectors=selectors,
        action_plan=action_plan,
    )

    print("=" * 64)
    print("Bilibili 抽奖任务 · 第一次试跑 (dry-run)")
    print("=" * 64)
    print(f"task_id   : {task_id}")
    print(f"platform  : {message['platform']}")
    print(f"mode      : {message['mode']}  (dry_run={message['dry_run']})")
    print(f"target    : {message['raw_url']}")
    print(f"plan      : {action_plan['required_actions']}")

    selector_config = task_runner.parse_json_field(message.get("selector_config")) or {}
    adapter = get_adapter(message["platform"], selector_config)
    print(f"adapter   : {type(adapter).__name__}  REAL_ACTIONS={adapter.REAL_ACTIONS}  STATUS={adapter.STATUS}")
    print("-" * 64)

    ok = await task_runner.execute_task_with_phases(message, adapter, pool=None)

    print("-" * 64)
    print(f"phases run: {fake.phases}")
    print(f"db writes : {fake.writes}")
    print(f"result    : {'SUCCESS ✅' if ok else 'FAILED ❌'}")
    print("=" * 64)

    await db.redis.close()

    expected = ["followed", "liked", "commented", "reposted", "completed"]
    if ok and fake.phases == expected:
        print("第一次抽奖任务管线打通：派发 → 入队消息 → bilibili 适配器 → 四个阶段 → 完成。")
        print("（这是演练模式，未对真实 B 站做任何操作。真实参与还需账号校准+探针+影子跑+开关。）")
        return 0
    print("Smoke run did not complete as expected.")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
