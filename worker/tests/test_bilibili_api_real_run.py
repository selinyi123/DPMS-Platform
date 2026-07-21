import asyncio
import base64
import os
import sys
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("ENCRYPTION_KEY", base64.b64encode(b"0" * 32).decode())
os.environ.setdefault("DATABASE_URL", "mysql+aiomysql://u:p@localhost:3306/lottery")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from bilibili_dry_run_harness import (  # noqa: E402
    FakeDatabase,
    build_reviewed_bilibili_action_plan,
    build_task_message,
    stub_httpx,
    stub_playwright,
    stub_worker_runtime_dependencies,
)

stub_playwright()
stub_httpx()
stub_worker_runtime_dependencies()


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


class FakeForwardBiliClient(FakeBiliClient):
    async def get_dynamic_detail(self, dynamic_id):
        return {
            "code": 0,
            "data": {
                "item": {
                    "id_str": dynamic_id,
                    "type": "DYNAMIC_TYPE_FORWARD",
                    "basic": {"comment_id_str": "wrapper-comment"},
                    "modules": {
                        "module_author": {"mid": 42, "name": "wrapper", "pub_ts": 1700000000},
                        "module_dynamic": {"desc": {"rich_text_nodes": []}},
                    },
                    "orig": {
                        "id_str": "987654321098765432",
                        "type": "DYNAMIC_TYPE_DRAW",
                        "basic": {"comment_id_str": "origin-comment"},
                        "modules": {
                            "module_author": {"mid": 7, "name": "origin", "pub_ts": 1699990000},
                            "module_dynamic": {"desc": {"rich_text_nodes": []}},
                        },
                    },
                }
            },
        }


class FakeBiliExecutor:
    last_actions = None
    attempted_actions = None

    def __init__(
        self,
        client,
        config,
        *,
        before_action=None,
        after_attempt=None,
        on_attempt_error=None,
        after_action=None,
    ):
        self.client = client
        self.config = config
        self.before_action = before_action
        self.after_attempt = after_attempt
        self.on_attempt_error = on_attempt_error
        self.after_action = after_action

    async def participate(self, card, actions, action_payloads=None):
        from app.bilibili.errors import classify

        del action_payloads
        FakeBiliExecutor.last_actions = list(actions)
        FakeBiliExecutor.attempted_actions = []
        results = {}
        for action in actions:
            FakeBiliExecutor.attempted_actions.append(action)
            if self.before_action is not None:
                await self.before_action(action)
            action_result = classify(action, 0)
            results[action] = action_result
            if self.after_attempt is not None:
                await self.after_attempt(action, action_result)
            if self.after_action is not None:
                await self.after_action(action, action_result)
        return SimpleNamespace(
            dynamic_id=card.dynamic_id,
            success=True,
            aborted=False,
            abort_reason="",
            actions=results,
        )


class BilibiliApiRealRunTests(unittest.TestCase):
    def test_safety_settlement_survives_repeated_real_task_cancellation(self):
        from app import task_runner

        async def scenario():
            started = asyncio.Event()
            release = asyncio.Event()

            async def settlement():
                started.set()
                await release.wait()
                return "settled"

            waiter = asyncio.create_task(
                task_runner.await_safety_settlement(settlement())
            )
            await started.wait()
            waiter.cancel()
            await asyncio.sleep(0)
            waiter.cancel()
            await asyncio.sleep(0)
            release.set()
            return await waiter

        self.assertEqual(asyncio.run(scenario()), "settled")

    def test_completed_api_phases_are_loaded_from_exact_action_ledger(self):
        from app import task_runner

        fake_db = FakeDatabase(task_mode="real_run")
        fake_db.bilibili_action_ledger = [
            {
                "task_id": "resume-task",
                "account_id": 9001,
                "lottery_id": 7001,
                "dynamic_id": "123456789012",
                "action": "repost",
                "phase": "reposted",
                "outcome": "ok",
                "ok": 1,
            },
            {
                "task_id": "another-task",
                "account_id": 9001,
                "lottery_id": 7001,
                "dynamic_id": "123456789012",
                "action": "comment",
                "phase": "commented",
                "outcome": "ok",
                "ok": 1,
            },
        ]

        with patch.object(task_runner, "database", fake_db):
            completed = asyncio.run(
                task_runner.get_completed_bilibili_phases(
                    "resume-task",
                    account_id=9001,
                    lottery_id=7001,
                    dynamic_id="123456789012",
                )
            )

        self.assertEqual(completed, {"reposted"})

    def test_completed_api_phase_with_wrong_binding_requires_reconciliation(self):
        from app import task_runner

        fake_db = FakeDatabase(task_mode="real_run")
        fake_db.bilibili_action_ledger = [{
            "task_id": "resume-task",
            "account_id": 9999,
            "lottery_id": 7001,
            "dynamic_id": "123456789012",
            "action": "repost",
            "phase": "reposted",
            "outcome": "ok",
            "ok": 1,
        }]

        with patch.object(task_runner, "database", fake_db):
            with self.assertRaisesRegex(
                RuntimeError,
                "bilibili_action_ledger_binding_invalid",
            ):
                asyncio.run(task_runner.get_completed_bilibili_phases(
                    "resume-task",
                    account_id=9001,
                    lottery_id=7001,
                    dynamic_id="123456789012",
                ))

    def test_completed_api_phase_with_malformed_binding_requires_reconciliation(self):
        from app import task_runner

        fake_db = FakeDatabase(task_mode="real_run")
        fake_db.bilibili_action_ledger = [{
            "task_id": "resume-task",
            "account_id": "not-an-account-id",
            "lottery_id": 7001,
            "dynamic_id": "123456789012",
            "action": "repost",
            "phase": "reposted",
            "outcome": "ok",
            "ok": 1,
        }]

        with patch.object(task_runner, "database", fake_db):
            with self.assertRaisesRegex(
                RuntimeError,
                "bilibili_action_ledger_binding_invalid",
            ):
                asyncio.run(task_runner.get_completed_bilibili_phases(
                    "resume-task",
                    account_id=9001,
                    lottery_id=7001,
                    dynamic_id="123456789012",
                ))

    def test_legacy_latest_phase_without_action_ledger_requires_reconciliation(self):
        from app import task_runner

        fake_db = FakeDatabase(task_mode="real_run")
        message = build_task_message(
            task_id="legacy-resume",
            account_id=9001,
            lottery_id=7001,
            platform="bilibili",
            raw_url="https://t.bilibili.com/123456789012",
            canonical_url="https://t.bilibili.com/123456789012",
            task_mode="real_run",
            dry_run=False,
            platform_selectors={},
            action_plan=build_reviewed_bilibili_action_plan(
                ["commented", "reposted"]
            ),
        )

        async def old_phase(_task_id):
            return "reposted"

        with (
            patch.object(task_runner, "database", fake_db),
            patch.object(task_runner, "get_latest_phase", old_phase),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "bilibili_legacy_phase_requires_reconciliation",
            ):
                asyncio.run(task_runner.execute_bilibili_api_real_task(message))

    def test_forwarded_wrapper_is_blocked_before_executor_changes_target(self):
        import app.db as db
        from app import task_runner

        fake_db = FakeDatabase(task_mode="real_run")
        fake_redis = FakeRedis()
        db.database = fake_db
        task_runner.database = fake_db
        task_runner.redis = fake_redis
        message = build_task_message(
            task_id="forward-wrapper-review",
            account_id=9001,
            lottery_id=7001,
            platform="bilibili",
            raw_url="https://t.bilibili.com/123456789012",
            canonical_url="https://t.bilibili.com/123456789012",
            task_mode="real_run",
            dry_run=False,
            platform_selectors={},
            action_plan=build_reviewed_bilibili_action_plan(
                ["followed", "commented", "reposted"],
                follow_handle="@wrapper",
            ),
        )

        original_client = task_runner.BilibiliApiClient
        original_executor = task_runner.BilibiliApiExecutor
        task_runner.BilibiliApiClient = FakeForwardBiliClient
        task_runner.BilibiliApiExecutor = FakeBiliExecutor
        FakeBiliExecutor.last_actions = None
        try:
            with self.assertRaisesRegex(
                task_runner.BilibiliForwardedTargetRequiresReview,
                "bilibili_forwarded_origin_requires_review",
            ):
                asyncio.run(task_runner.execute_bilibili_api_real_task(message))
        finally:
            task_runner.BilibiliApiClient = original_client
            task_runner.BilibiliApiExecutor = original_executor

        self.assertIsNone(FakeBiliExecutor.last_actions)
        self.assertEqual(fake_db.bilibili_action_ledger, [])

    def test_real_run_uses_api_engine_without_selector_adapter(self):
        import app.db as db

        fake_db = FakeDatabase(task_mode="real_run")
        fake_redis = FakeRedis()
        db.database = fake_db

        from app import safety, task_runner
        from app.adapters.registry import get_adapter
        from app.event_store import service as event_service

        original_client = task_runner.BilibiliApiClient
        original_executor = task_runner.BilibiliApiExecutor
        original_gate = task_runner.enforce_task_real_run_gate
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
                action_plan=build_reviewed_bilibili_action_plan(follow_handle="@up"),
            )
            adapter = get_adapter("bilibili", {})
            self.assertFalse(adapter.REAL_ACTIONS)

            original_ensure = task_runner.ensure_account_can_run
            safety_calls = []
            gate_calls = []

            async def record_gate(task, *, require_running=False):
                from app.action_plan import validate_action_plan_v2

                gate_calls.append((task["task_id"], require_running))
                return SimpleNamespace(
                    action_plan=validate_action_plan_v2(
                        task["action_plan"], reject_media=True
                    )
                )

            async def record_safety_window(account_id, platform=None):
                safety_calls.append((account_id, platform))
                await original_ensure(account_id, platform)

            task_runner.enforce_task_real_run_gate = record_gate
            task_runner.ensure_account_can_run = record_safety_window
            try:
                ok = asyncio.run(task_runner.execute_task_with_phases(message, adapter, pool=None))
            finally:
                task_runner.ensure_account_can_run = original_ensure

            self.assertTrue(ok)
            self.assertEqual(safety_calls, [(9001, "bilibili")])
            self.assertEqual(
                gate_calls,
                [(message["task_id"], False)]
                + [(message["task_id"], True)] * 8,
            )
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
            task_runner.enforce_task_real_run_gate = original_gate

    def test_confirmed_action_settlement_failure_opens_breaker_and_quarantines_account(self):
        import app.db as db
        from app import task_runner

        fake_db = FakeDatabase(task_mode="real_run")
        fake_db.task_id = "task-settlement"
        fake_db.task_status = "running"
        fake_redis = FakeRedis()
        db.database = fake_db
        task_runner.database = fake_db
        task_runner.redis = fake_redis
        fake_db.task_worker_id = task_runner.WORKER_ID

        message = build_task_message(
            task_id="task-settlement",
            account_id=9001,
            lottery_id=7001,
            platform="bilibili",
            raw_url="https://t.bilibili.com/123456789012",
            canonical_url="https://t.bilibili.com/123456789012",
            task_mode="real_run",
            dry_run=False,
            platform_selectors={},
            action_plan=build_reviewed_bilibili_action_plan(
                ["followed"], follow_handle="@up"
            ),
        )

        originals = {
            "client": task_runner.BilibiliApiClient,
            "executor": task_runner.BilibiliApiExecutor,
            "gate": task_runner.enforce_task_real_run_gate,
            "persist": task_runner.persist_bilibili_action_result,
            "breaker": task_runner.open_unknown_outcome_breaker,
            "ledger": task_runner.save_bilibili_action_ledger,
            "event": task_runner.record_event,
            "status": task_runner.set_account_status,
        }
        breaker_calls = []
        ledger_calls = []
        status_calls = []

        async def allow_gate(task, **_kwargs):
            from app.action_plan import validate_action_plan_v2

            return SimpleNamespace(
                action_plan=validate_action_plan_v2(
                    task["action_plan"], reject_media=True
                )
            )

        async def fail_settlement(**_kwargs):
            raise RuntimeError("audit database unavailable")

        async def record_breaker(**kwargs):
            breaker_calls.append(kwargs)

        async def record_ledger(**kwargs):
            ledger_calls.append(kwargs)

        async def record_event(**_kwargs):
            return "event-1"

        async def record_status(*args):
            status_calls.append(args)

        task_runner.BilibiliApiClient = FakeBiliClient
        task_runner.BilibiliApiExecutor = FakeBiliExecutor
        task_runner.enforce_task_real_run_gate = allow_gate
        task_runner.persist_bilibili_action_result = fail_settlement
        task_runner.open_unknown_outcome_breaker = record_breaker
        task_runner.save_bilibili_action_ledger = record_ledger
        task_runner.record_event = record_event
        task_runner.set_account_status = record_status
        try:
            with self.assertRaises(task_runner.BilibiliActionSettlementFailed):
                asyncio.run(task_runner.execute_bilibili_api_real_task(message))
        finally:
            task_runner.BilibiliApiClient = originals["client"]
            task_runner.BilibiliApiExecutor = originals["executor"]
            task_runner.enforce_task_real_run_gate = originals["gate"]
            task_runner.persist_bilibili_action_result = originals["persist"]
            task_runner.open_unknown_outcome_breaker = originals["breaker"]
            task_runner.save_bilibili_action_ledger = originals["ledger"]
            task_runner.record_event = originals["event"]
            task_runner.set_account_status = originals["status"]

        self.assertEqual(len(breaker_calls), 1)
        self.assertEqual(breaker_calls[0]["platform"], "bilibili")
        self.assertEqual(len(ledger_calls), 1)
        self.assertEqual(ledger_calls[0]["outcome"], "ok")
        self.assertTrue(ledger_calls[0]["ok"])
        self.assertEqual(status_calls, [(9001, "cooling", "bilibili_follow_settlement_failed")])

    def test_uncommitted_quarantine_status_does_not_fall_back_to_generic_cleanup(self):
        import app.db as db
        from app import task_runner

        fake_db = FakeDatabase(task_mode="real_run")
        fake_db.task_id = "task-quarantine-status"
        fake_db.task_status = "running"
        fake_db.task_worker_id = task_runner.WORKER_ID
        db.database = fake_db
        task_runner.database = fake_db
        task_runner.redis = FakeRedis()

        message = build_task_message(
            task_id="task-quarantine-status",
            account_id=9001,
            lottery_id=7001,
            platform="bilibili",
            raw_url="https://t.bilibili.com/123456789012",
            canonical_url="https://t.bilibili.com/123456789012",
            task_mode="real_run",
            dry_run=False,
            platform_selectors={},
            action_plan=build_reviewed_bilibili_action_plan(
                ["followed", "liked"], follow_handle="@up"
            ),
        )
        breaker_calls = []
        ledger_calls = []

        async def allow_gate(task, **_kwargs):
            from app.action_plan import validate_action_plan_v2

            return SimpleNamespace(
                action_plan=validate_action_plan_v2(
                    task["action_plan"], reject_media=True
                )
            )

        async def fail_action_settlement(**_kwargs):
            raise RuntimeError("action audit unavailable")

        async def record_breaker(**kwargs):
            breaker_calls.append(kwargs)

        async def record_ledger(**kwargs):
            ledger_calls.append(kwargs)

        async def record_event(**_kwargs):
            return "event-1"

        async def fail_account_status(account_id, status, reason):
            raise task_runner.AccountStatusPersistenceFailed(
                account_id,
                status,
                reason,
                RuntimeError("risk audit unavailable"),
            )

        with (
            patch.object(task_runner, "BilibiliApiClient", FakeBiliClient),
            patch.object(task_runner, "BilibiliApiExecutor", FakeBiliExecutor),
            patch.object(task_runner, "enforce_task_real_run_gate", allow_gate),
            patch.object(task_runner, "persist_bilibili_action_result", fail_action_settlement),
            patch.object(task_runner, "open_unknown_outcome_breaker", record_breaker),
            patch.object(task_runner, "save_bilibili_action_ledger", record_ledger),
            patch.object(task_runner, "record_event", record_event),
            patch.object(task_runner, "set_account_status", fail_account_status),
        ):
            with self.assertRaises(task_runner.AccountStatusPersistenceFailed):
                asyncio.run(task_runner.execute_bilibili_api_real_task(message))

        self.assertEqual(FakeBiliExecutor.attempted_actions, ["follow"])
        self.assertEqual(len(breaker_calls), 1)
        self.assertEqual(len(ledger_calls), 1)
        self.assertEqual(ledger_calls[0]["outcome"], "ok")

    def test_cancelled_confirmed_action_settlement_uses_quarantine_path(self):
        import app.db as db
        from app import task_runner

        fake_db = FakeDatabase(task_mode="real_run")
        fake_db.task_id = "task-cancelled-settlement"
        fake_db.task_status = "running"
        fake_db.task_worker_id = task_runner.WORKER_ID
        db.database = fake_db
        task_runner.database = fake_db
        task_runner.redis = FakeRedis()

        message = build_task_message(
            task_id="task-cancelled-settlement",
            account_id=9001,
            lottery_id=7001,
            platform="bilibili",
            raw_url="https://t.bilibili.com/123456789012",
            canonical_url="https://t.bilibili.com/123456789012",
            task_mode="real_run",
            dry_run=False,
            platform_selectors={},
            action_plan=build_reviewed_bilibili_action_plan(
                ["followed", "liked"], follow_handle="@up"
            ),
        )
        breaker_calls = []
        ledger_calls = []
        status_calls = []

        async def allow_gate(task, **_kwargs):
            from app.action_plan import validate_action_plan_v2

            return SimpleNamespace(
                action_plan=validate_action_plan_v2(
                    task["action_plan"], reject_media=True
                )
            )

        async def cancel_settlement(**_kwargs):
            raise asyncio.CancelledError()

        async def record_breaker(**kwargs):
            breaker_calls.append(kwargs)

        async def record_ledger(**kwargs):
            ledger_calls.append(kwargs)

        async def record_event(**_kwargs):
            return "event-1"

        async def record_status(*args):
            status_calls.append(args)

        with (
            patch.object(task_runner, "BilibiliApiClient", FakeBiliClient),
            patch.object(task_runner, "BilibiliApiExecutor", FakeBiliExecutor),
            patch.object(task_runner, "enforce_task_real_run_gate", allow_gate),
            patch.object(task_runner, "persist_bilibili_action_result", cancel_settlement),
            patch.object(task_runner, "open_unknown_outcome_breaker", record_breaker),
            patch.object(task_runner, "save_bilibili_action_ledger", record_ledger),
            patch.object(task_runner, "record_event", record_event),
            patch.object(task_runner, "set_account_status", record_status),
        ):
            with self.assertRaises(task_runner.BilibiliActionSettlementFailed) as caught:
                asyncio.run(task_runner.execute_bilibili_api_real_task(message))

        self.assertIsInstance(caught.exception.__cause__, asyncio.CancelledError)
        self.assertEqual(FakeBiliExecutor.attempted_actions, ["follow"])
        self.assertEqual(len(breaker_calls), 1)
        self.assertEqual(breaker_calls[0]["action"], "follow")
        self.assertEqual(len(ledger_calls), 1)
        self.assertEqual(ledger_calls[0]["action"], "follow")
        self.assertEqual(ledger_calls[0]["outcome"], "ok")
        self.assertTrue(ledger_calls[0]["ok"])
        self.assertEqual(
            status_calls,
            [(9001, "cooling", "bilibili_follow_settlement_failed")],
        )

    def test_breaker_write_failure_invokes_emergency_stop_and_remains_unsettled(self):
        import app.db as db
        from app import task_runner

        fake_db = FakeDatabase(task_mode="real_run")
        fake_db.task_id = "task-breaker-failure"
        fake_db.task_status = "running"
        fake_db.task_worker_id = task_runner.WORKER_ID
        db.database = fake_db
        task_runner.database = fake_db
        task_runner.redis = FakeRedis()

        message = build_task_message(
            task_id="task-breaker-failure",
            account_id=9001,
            lottery_id=7001,
            platform="bilibili",
            raw_url="https://t.bilibili.com/123456789012",
            canonical_url="https://t.bilibili.com/123456789012",
            task_mode="real_run",
            dry_run=False,
            platform_selectors={},
            action_plan=build_reviewed_bilibili_action_plan(
                ["followed"], follow_handle="@up"
            ),
        )

        originals = {
            "client": task_runner.BilibiliApiClient,
            "executor": task_runner.BilibiliApiExecutor,
            "gate": task_runner.enforce_task_real_run_gate,
            "persist": task_runner.persist_bilibili_action_result,
            "breaker": task_runner.open_unknown_outcome_breaker,
            "emergency": task_runner.emergency_stop_real_runs_and_revoke_lease,
            "ledger": task_runner.save_bilibili_action_ledger,
            "event": task_runner.record_event,
            "status": task_runner.set_account_status,
        }
        emergency_calls = []

        async def no_op(*_args, **_kwargs):
            return None

        async def allow_gate(task, **_kwargs):
            from app.action_plan import validate_action_plan_v2

            return SimpleNamespace(
                action_plan=validate_action_plan_v2(
                    task["action_plan"], reject_media=True
                )
            )

        async def fail_settlement(**_kwargs):
            raise RuntimeError("audit database unavailable")

        async def fail_breaker(**_kwargs):
            raise RuntimeError("platform breaker unavailable")

        async def record_emergency(**kwargs):
            emergency_calls.append(kwargs)
            return "global_breaker"

        async def record_event(**_kwargs):
            return "event-1"

        task_runner.BilibiliApiClient = FakeBiliClient
        task_runner.BilibiliApiExecutor = FakeBiliExecutor
        task_runner.enforce_task_real_run_gate = allow_gate
        task_runner.persist_bilibili_action_result = fail_settlement
        task_runner.open_unknown_outcome_breaker = fail_breaker
        task_runner.emergency_stop_real_runs_and_revoke_lease = record_emergency
        task_runner.save_bilibili_action_ledger = no_op
        task_runner.record_event = record_event
        task_runner.set_account_status = no_op
        try:
            with self.assertRaises(task_runner.TaskSettlementUnconfirmed):
                asyncio.run(task_runner.execute_bilibili_api_real_task(message))
        finally:
            task_runner.BilibiliApiClient = originals["client"]
            task_runner.BilibiliApiExecutor = originals["executor"]
            task_runner.enforce_task_real_run_gate = originals["gate"]
            task_runner.persist_bilibili_action_result = originals["persist"]
            task_runner.open_unknown_outcome_breaker = originals["breaker"]
            task_runner.emergency_stop_real_runs_and_revoke_lease = originals["emergency"]
            task_runner.save_bilibili_action_ledger = originals["ledger"]
            task_runner.record_event = originals["event"]
            task_runner.set_account_status = originals["status"]

        self.assertEqual(len(emergency_calls), 1)
        self.assertEqual(emergency_calls[0]["task_id"], "task-breaker-failure")
        self.assertEqual(emergency_calls[0]["platform"], "bilibili")
        self.assertEqual(emergency_calls[0]["action"], "follow")


if __name__ == "__main__":
    unittest.main()
