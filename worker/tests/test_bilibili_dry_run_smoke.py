"""Guards the Bilibili lottery task's first end-to-end path (dry-run).

Drives a real Bilibili task through the genuine worker executor
(`execute_task_with_phases` in dry-run mode), asserting the adapter is
selected, real actions are enabled by the shipped example selectors, every
phase runs in order, and the task completes.

Shares its fake DB and dispatch-message builder with
tools/smoke_bilibili_dry_run.py via tools/bilibili_dry_run_harness.py, so the
two can't quietly drift apart. Redis is faked here (unlike the manual
script, which talks to a real local Redis) so this suite never depends on a
running Redis.
"""

import asyncio
import base64
import os
import sys
import unittest
import uuid
from pathlib import Path

# crypto.cookie_vault validates this at import; a throwaway 32-byte key is fine
# since dry-run never decrypts a credential.
os.environ.setdefault("ENCRYPTION_KEY", base64.b64encode(b"0" * 32).decode())
os.environ.setdefault("DATABASE_URL", "mysql+aiomysql://u:p@localhost:3306/lottery")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from bilibili_dry_run_harness import (  # noqa: E402
    COMPLETED_PHASES,
    FakeDatabase,
    build_task_message,
    load_bilibili_example_selectors,
    stub_playwright,
)

stub_playwright()


class FakePipeline:
    async def execute(self):
        return [0, 0, 1, 0]  # zadd, zremrangebyscore, zcard -> 1, expire

    def __getattr__(self, _name):
        return lambda *a, **k: self


class FakeRedis:
    def pipeline(self):
        return FakePipeline()

    async def xadd(self, *a, **k):
        return "1-0"

    async def delete(self, *a, **k):
        return 0


class BilibiliDryRunSmokeTests(unittest.TestCase):
    def test_first_task_runs_all_phases_to_completion(self):
        import app.db as db

        fake_db = FakeDatabase()
        fake_redis = FakeRedis()
        db.database = fake_db

        from app import task_runner, safety
        from app.event_store import service as event_service

        task_runner.database = fake_db
        task_runner.redis = fake_redis
        safety.database = fake_db
        safety.redis = fake_redis
        event_service.database = fake_db

        from app.adapters.registry import get_adapter

        selectors = load_bilibili_example_selectors()
        message = build_task_message(
            task_id=str(uuid.uuid4()),
            account_id=9001,
            lottery_id=7001,
            platform="bilibili",
            raw_url="https://t.bilibili.com/123456789",
            canonical_url="https://t.bilibili.com/123456789",
            task_mode="dry_run",
            dry_run=True,
            platform_selectors=selectors,
            action_plan={"required_actions": COMPLETED_PHASES[:-1]},
        )

        selector_config = task_runner.parse_json_field(message["selector_config"])
        adapter = get_adapter("bilibili", selector_config)
        self.assertEqual(type(adapter).__name__, "BilibiliAdapter")
        self.assertTrue(adapter.REAL_ACTIONS)
        self.assertEqual(adapter.STATUS, "configured")

        ok = asyncio.run(task_runner.execute_task_with_phases(message, adapter, pool=None))

        self.assertTrue(ok)
        self.assertEqual(fake_db.phases, COMPLETED_PHASES)


if __name__ == "__main__":
    unittest.main()
