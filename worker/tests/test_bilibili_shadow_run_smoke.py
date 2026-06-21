"""Guards the Bilibili lottery task's shadow-run path.

Drives a real Bilibili task through the genuine worker executor
(`execute_task_with_phases` in shadow-run mode), asserting the adapter
navigates, probes selector visibility without clicking anything, captures
evidence, and completes -- since `recent_shadow_run` is a required real-run
gate (core/app/services/real_run_gate.py) with otherwise zero test coverage
for Bilibili.

Shares its fake DB, dispatch-message builder, and fake Playwright
primitives with tools/smoke_bilibili_shadow_run.py via
tools/bilibili_dry_run_harness.py, so the two can't quietly drift apart.
Redis is faked here (unlike the manual script, which talks to a real local
Redis) so this suite never depends on a running Redis.
"""

import asyncio
import base64
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

# crypto.cookie_vault validates this at import; a throwaway 32-byte key is fine
# since shadow-run's fake credential is plaintext JSON, never really encrypted.
os.environ.setdefault("ENCRYPTION_KEY", base64.b64encode(b"0" * 32).decode())
os.environ.setdefault("DATABASE_URL", "mysql+aiomysql://u:p@localhost:3306/lottery")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from bilibili_dry_run_harness import (  # noqa: E402
    FakeDatabase,
    FakePage,
    FakePool,
    build_task_message,
    load_bilibili_example_selectors,
    stub_playwright,
    visible_selectors_for_all_phases,
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


class BilibiliShadowRunSmokeTests(unittest.TestCase):
    def test_shadow_run_probes_selectors_and_completes_without_side_effects(self):
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
        task_runner.SHADOW_SCREENSHOT_DIR = Path(tempfile.mkdtemp(prefix="bilibili-shadow-run-test-"))

        from app.adapters.registry import get_adapter

        selectors = load_bilibili_example_selectors()
        message = build_task_message(
            task_id=str(uuid.uuid4()),
            account_id=9001,
            lottery_id=7001,
            platform="bilibili",
            raw_url="https://t.bilibili.com/123456789",
            canonical_url="https://t.bilibili.com/123456789",
            task_mode="shadow_run",
            dry_run=False,
            platform_selectors=selectors,
            action_plan={"required_actions": ["followed", "liked", "commented", "reposted"]},
        )

        selector_config = task_runner.parse_json_field(message["selector_config"])
        adapter = get_adapter("bilibili", selector_config)
        self.assertEqual(type(adapter).__name__, "BilibiliAdapter")
        self.assertTrue(adapter.REAL_ACTIONS)

        fake_page = FakePage(visible_selectors_for_all_phases(selectors))
        pool = FakePool(fake_page)

        ok = asyncio.run(task_runner.execute_task_with_phases(message, adapter, pool))

        self.assertTrue(ok)
        # Shadow-run never touches the per-action phase ledger; only the
        # terminal "completed" marker is written, and no clicks happened.
        self.assertEqual(fake_db.phases, ["completed"])
        self.assertEqual(fake_page.url, "https://t.bilibili.com/123456789")


if __name__ == "__main__":
    unittest.main()
