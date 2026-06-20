"""Guards the Bilibili lottery task's first end-to-end path (dry-run).

Drives a real Bilibili task through the genuine worker executor
(`execute_task_with_phases` in dry-run mode) with an in-memory DB and a fake
Redis, asserting the adapter is selected, real actions are enabled by the
shipped example selectors, every phase runs in order, and the task completes.

This is the regression net behind tools/smoke_bilibili_dry_run.py.
"""

import asyncio
import base64
import json
import os
import sys
import types
import unittest
import uuid
from pathlib import Path

# crypto.cookie_vault validates this at import; a throwaway 32-byte key is fine
# since dry-run never decrypts a credential.
os.environ.setdefault("ENCRYPTION_KEY", base64.b64encode(b"0" * 32).decode())
os.environ.setdefault("DATABASE_URL", "mysql+aiomysql://u:p@localhost:3306/lottery")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

# browser_pool imports playwright at module load; dry-run never opens a browser.
_pw = types.ModuleType("playwright")
_aio = types.ModuleType("playwright.async_api")
_aio.Page = object
_aio.async_playwright = lambda *a, **k: None
_pw.async_api = _aio
sys.modules.setdefault("playwright", _pw)
sys.modules.setdefault("playwright.async_api", _aio)

CORE = Path(__file__).resolve().parents[2] / "core"
PHASES = ["followed", "liked", "commented", "reposted", "completed"]


class FakeDatabase:
    def __init__(self):
        self.phases = []

    async def fetch_one(self, query, values=None):
        if "FROM accounts" in query:
            return {"status": "ready", "daily_task_count": 0, "encrypted_credential": "[]"}
        if "FROM task_runs" in query:
            return {"screenshot_path": None}
        return None

    async def fetch_all(self, query, values=None):
        return []

    async def execute(self, query, values=None):
        if "INSERT INTO task_phases" in query and values:
            self.phases.append(values.get("phase"))

    def transaction(self):
        class _Tx:
            async def __aenter__(self_):
                return self

            async def __aexit__(self_, *exc):
                return False

        return _Tx()


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

        selectors = json.loads((CORE / "adapter_selectors.example.json").read_text())["bilibili"]
        message = {
            "task_id": str(uuid.uuid4()),
            "account_id": "9001",
            "lottery_id": "7001",
            "platform": "bilibili",
            "raw_url": "https://t.bilibili.com/123456789",
            "canonical_url": "https://t.bilibili.com/123456789",
            "dry_run": "1",
            "mode": "dry_run",
            "selector_config": json.dumps(selectors, ensure_ascii=False),
            "action_plan": json.dumps({"required_actions": PHASES[:-1]}),
        }

        adapter = get_adapter("bilibili", json.loads(message["selector_config"]))
        self.assertEqual(type(adapter).__name__, "BilibiliAdapter")
        self.assertTrue(adapter.REAL_ACTIONS)
        self.assertEqual(adapter.STATUS, "configured")

        ok = asyncio.run(task_runner.execute_task_with_phases(message, adapter, pool=None))

        self.assertTrue(ok)
        self.assertEqual(fake_db.phases, PHASES)


if __name__ == "__main__":
    unittest.main()
