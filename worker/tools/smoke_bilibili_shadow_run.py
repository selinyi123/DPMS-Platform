"""End-to-end shadow-run smoke test for the Bilibili lottery task.

Runs a *real* Bilibili lottery task through the genuine worker executor
(`execute_task_with_phases` in shadow-run mode) so you can watch the
dispatch -> adapter -> browser-navigation -> selector-visibility-probe ->
evidence-capture pipeline run end to end -- WITHOUT touching the live
Bilibili site or needing a real account/browser.

Why shadow-run matters here: `recent_shadow_run` is one of the real-run
gate's required checks (core/app/services/real_run_gate.py), so until this
path is exercised at least once, Bilibili can never legitimately clear the
gate to real-run -- regardless of how complete its selector config is.

Shadow-run still performs zero page actions (no clicks, no comments); it
only *observes* which configured selectors are visible and records that as
evidence (screenshot + EvidenceCaptured event), exactly like the real
worker would against a genuine page -- here played by a fake Page/Context
/Pool instead of a real browser.

Infra: talks to a real local Redis (so the risk-window/notify-stream side
effects are genuine) and in-memory stand-ins for MySQL + Playwright, so it
runs anywhere without docker. A live Redis at REDIS_URL is required.

Run:
    ENCRYPTION_KEY=$(python -c "import base64,os;print(base64.b64encode(os.urandom(32)).decode())") \
    REDIS_URL=redis://localhost:6379/0 \
    python tools/smoke_bilibili_shadow_run.py
"""

import asyncio
import os
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # worker/ -> `app` importable

from bilibili_dry_run_harness import (  # noqa: E402
    COMPLETED_PHASES,
    FakeDatabase,
    FakePage,
    FakePool,
    build_task_message,
    load_bilibili_example_selectors,
    stub_playwright,
    visible_selectors_for_all_phases,
)

stub_playwright()

os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("DATABASE_URL", "mysql+aiomysql://u:p@localhost:3306/lottery")


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
    # Real screenshots go under /profiles/shadow-runs; redirect to a tmp dir
    # so this script doesn't need write access to /profiles.
    task_runner.SHADOW_SCREENSHOT_DIR = Path(tempfile.mkdtemp(prefix="bilibili-shadow-run-"))

    # Real local Redis (shared singleton) for the risk window + notify stream.
    await db.redis.initialize()
    try:
        await db.redis.delete("risk_window:9001")
    except Exception:
        pass

    from app.adapters.registry import get_adapter

    selectors = load_bilibili_example_selectors()
    action_plan = {"required_actions": COMPLETED_PHASES[:-1]}
    task_id = str(uuid.uuid4())
    message = build_task_message(
        task_id=task_id,
        account_id=9001,
        lottery_id=7001,
        platform="bilibili",
        raw_url="https://t.bilibili.com/123456789",
        canonical_url="https://t.bilibili.com/123456789",
        task_mode="shadow_run",
        dry_run=False,
        platform_selectors=selectors,
        action_plan=action_plan,
    )

    print("=" * 64)
    print("Bilibili 抽奖任务 · 影子跑 (shadow-run)")
    print("=" * 64)
    print(f"task_id   : {task_id}")
    print(f"platform  : {message['platform']}")
    print(f"mode      : {message['mode']}")
    print(f"target    : {message['raw_url']}")
    print(f"plan      : {action_plan['required_actions']}")

    selector_config = task_runner.parse_json_field(message.get("selector_config")) or {}
    adapter = get_adapter(message["platform"], selector_config)
    print(f"adapter   : {type(adapter).__name__}  REAL_ACTIONS={adapter.REAL_ACTIONS}  STATUS={adapter.STATUS}")
    print("-" * 64)

    page = FakePage(visible_selectors_for_all_phases(selectors))
    pool = FakePool(page)
    ok = await task_runner.execute_task_with_phases(message, adapter, pool=pool)

    print("-" * 64)
    print(f"phases run     : {fake.phases}")
    print(f"db writes      : {fake.writes}")
    print(f"page navigated : {page.url}")
    print(f"result         : {'SUCCESS ✅' if ok else 'FAILED ❌'}")
    print("=" * 64)

    await db.redis.close()

    if ok and fake.phases == ["completed"]:
        print("影子跑管线打通：派发 → bilibili 适配器 → 浏览器导航 → 选择器可见性探测 → 截图证据 → 完成。")
        print("（影子跑零副作用，未点击/未评论。real-run 网关的 recent_shadow_run 这一项现在有真实路径可验证了。）")
        return 0
    print("Smoke run did not complete as expected.")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
