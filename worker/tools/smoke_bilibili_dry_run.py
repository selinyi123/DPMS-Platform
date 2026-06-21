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

Infra: talks to a real local Redis (so the risk-window/notify-stream side
effects are genuine) and an in-memory stand-in for MySQL, so it runs anywhere
without docker. A live Redis at REDIS_URL is required.

Run:
    ENCRYPTION_KEY=$(python -c "import base64,os;print(base64.b64encode(os.urandom(32)).decode())") \
    REDIS_URL=redis://localhost:6379/0 \
    python tools/smoke_bilibili_dry_run.py
"""

import asyncio
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # worker/ -> `app` importable

from bilibili_dry_run_harness import (  # noqa: E402
    COMPLETED_PHASES,
    FakeDatabase,
    build_task_message,
    load_bilibili_example_selectors,
    stub_playwright,
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

    # Real local Redis (shared singleton) for the risk window + notify stream.
    await db.redis.initialize()
    try:
        await db.redis.delete("risk_window:9001")
    except Exception:
        pass

    from app.adapters.registry import get_adapter

    # Build the dispatch message exactly as core's dispatcher would, for a
    # Bilibili lottery, using the shipped example selectors + a reviewed plan.
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

    if ok and fake.phases == COMPLETED_PHASES:
        print("第一次抽奖任务管线打通：派发 → 入队消息 → bilibili 适配器 → 四个阶段 → 完成。")
        print("（这是演练模式，未对真实 B 站做任何操作。真实参与还需账号校准+探针+影子跑+开关。）")
        return 0
    print("Smoke run did not complete as expected.")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
