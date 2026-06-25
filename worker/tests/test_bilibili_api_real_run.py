import asyncio
import base64
import os
import sys
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("ENCRYPTION_KEY", base64.b64encode(b"0" * 32).decode())
os.environ.setdefault("DATABASE_URL", "mysql+aiomysql://u:p@localhost:3306/lottery")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from bilibili_dry_run_harness import FakeDatabase, build_task_message, stub_playwright  # noqa: E402

stub_playwright()


class FakePipeline:
    async def execute(self):
        return [0, 0, 1, 0]

    def __getattr__(self, _name):
        return lambda *a, **k: self


class FakeRedis:
    def pipeline(self):
        return FakePipeline()

    async def xadd(self, *a, **k):
        return "1-0"


class FakeBiliClient:
    def __init__(self, cookie, config=None):
        self.cookie = cookie
        self.config = config

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def check_login(self):
        return True

    async def get_dynamic_detail(self, dynamic_id):
        return {
            "code": 0,
            "data": {
                "item": {
                    "id_str": dynamic_id,
                    "type": "DYNAMIC_TYPE_DRAW",
                    "basic": {"comment_id_str": "987654321"},
                    "modules": {
                        "module_author": {"mid": 42, "name": "up", "pub_ts": 1700000000},
                        "module_dynamic": {"desc": {"rich_text_nodes": []}},
                    },
                }
            },
        }


class FakeBiliExecutor:
    last_actions = None

    def __init__(self, client, config):
        self.client = client
        self.config = config

    async def participate(self, card, actions):
        from app.bilibili.errors import classify

        FakeBiliExecutor.last_actions = list(actions)
        return SimpleNamespace(
            dynamic_id=card.dynamic_id,
            success=True,
            aborted=False,
            abort_reason="",
            actions={action: classify(action, 0) for action in actions},
        )


class BilibiliApiRealRunTests(unittest.TestCase):
    def test_real_run_uses_api_engine_without_selector_adapter(self):
        import app.db as db

        fake_db = FakeDatabase()
        fake_redis = FakeRedis()
        db.database = fake_db

        from app import safety, task_runner
        from app.adapters.registry import get_adapter
        from app.event_store import service as event_service

        original_client = task_runner.BilibiliApiClient
        original_executor = task_runner.BilibiliApiExecutor
        task_runner.BilibiliApiClient = FakeBiliClient
        task_runner.BilibiliApiExecutor = FakeBiliExecutor
        task_runner.database = fake_db
        task_runner.redis = fake_redis
        safety.database = fake_db
        safety.redis = fake_redis
        event_service.database = fake_db
        try:
            message = build_task_message(
                task_id=str(uuid.uuid4()),
                account_id=9001,
                lottery_id=7001,
                platform="bilibili",
                raw_url="https://t.bilibili.com/123456789012",
                canonical_url="https://t.bilibili.com/123456789012",
                task_mode="real_run",
                dry_run=False,
                platform_selectors={},
                action_plan={"required_actions": ["followed", "liked", "commented", "reposted"]},
            )
            adapter = get_adapter("bilibili", {})
            self.assertFalse(adapter.REAL_ACTIONS)

            original_ensure = task_runner.ensure_account_can_run
            safety_calls = []

            async def record_safety_window(account_id, platform=None):
                safety_calls.append((account_id, platform))
                await original_ensure(account_id, platform)

            task_runner.ensure_account_can_run = record_safety_window
            try:
                ok = asyncio.run(task_runner.execute_task_with_phases(message, adapter, pool=None))
            finally:
                task_runner.ensure_account_can_run = original_ensure

            self.assertTrue(ok)
            self.assertEqual(safety_calls, [(9001, "bilibili")])
            self.assertEqual(FakeBiliExecutor.last_actions, ["follow", "like", "comment", "repost"])
            self.assertEqual(fake_db.phases, ["followed", "liked", "commented", "reposted", "completed"])
            self.assertEqual(
                [entry["action"] for entry in fake_db.bilibili_action_ledger],
                ["follow", "like", "comment", "repost"],
            )
            self.assertTrue(all(entry["ok"] == 1 for entry in fake_db.bilibili_action_ledger))
            self.assertEqual(fake_db.bilibili_action_ledger[1]["phase"], "liked")
        finally:
            task_runner.BilibiliApiClient = original_client
            task_runner.BilibiliApiExecutor = original_executor


if __name__ == "__main__":
    unittest.main()
