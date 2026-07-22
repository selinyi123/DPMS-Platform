import base64
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("ENCRYPTION_KEY", base64.b64encode(b"0" * 32).decode())
os.environ.setdefault("UPDATE_SECRET", "test-secret")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")

from app.api.lotteries import (  # noqa: E402
    create_lottery,
    dispatch_lottery,
    dispatch_lottery_repair,
    import_lottery_targets,
)
from fastapi import HTTPException  # noqa: E402
from app.models.schemas import (  # noqa: E402
    DispatchTaskRequest,
    LotteryCreate,
    LotteryTargetImport,
)


class DuplicateEntryError(Exception):
    pass


class CommandDatabase:
    def __init__(self, *, execute_result=7, execute_error=None, existing=None):
        self.execute_result = execute_result
        self.execute_error = execute_error
        self.existing = existing
        self.fetch_count = 0
        self.execute_count = 0

    async def execute(self, query, values=None):
        self.execute_count += 1
        if self.execute_error:
            raise self.execute_error
        return self.execute_result

    async def fetch_one(self, query, values=None):
        self.fetch_count += 1
        return self.existing


class RecordingTransaction:
    async def __aenter__(self):
        return None

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class DispatchDatabase:
    def __init__(self, lottery):
        self.lottery = lottery
        self.fetch_count = 0
        self.executions = []

    def transaction(self):
        return RecordingTransaction()

    async def fetch_one(self, query, values=None):
        self.fetch_count += 1
        if self.fetch_count == 1:
            return self.lottery
        return {
            key: self.lottery.get(key)
            for key in (
                "status",
                "execution_lock",
                "platform",
                "raw_url",
                "canonical_url",
                "rule_text",
                "action_plan",
            )
        }

    async def execute(self, query, values=None):
        self.executions.append((query, dict(values or {})))
        return 1


def valid_target():
    return SimpleNamespace(valid=True, reason=None, kind="dynamic")


def imported_row():
    return {
        "line": 1,
        "raw": "https://t.bilibili.com/123",
        "platform": "bilibili",
        "raw_url": "https://t.bilibili.com/123",
        "value_score": 50,
        "expires_at": None,
    }


class CreateLotteryCommitTests(unittest.IsolatedAsyncioTestCase):
    async def test_committed_create_returns_created_when_event_writer_raises(self):
        database = CommandDatabase(execute_result=7)
        event_writer = AsyncMock(side_effect=RuntimeError("event store unavailable"))
        data = LotteryCreate(raw_url="https://t.bilibili.com/123")

        with (
            patch("app.api.lotteries.database", database),
            patch("app.api.lotteries.require_min_role", return_value={"actor_id": "operator-1"}),
            patch("app.api.lotteries.get_platform", return_value={"label": "Bilibili"}),
            patch("app.api.lotteries.validate_lottery_target", return_value=valid_target()),
            patch("app.api.lotteries.canonicalize_lottery_url", new=AsyncMock(return_value="canonical://bilibili/dynamic/123")),
            patch("app.api.lotteries.record_event", new=event_writer),
            patch("app.api.lotteries.structured_log"),
        ):
            result = await create_lottery(data, object())

        self.assertEqual({"status": "created", "id": 7}, result)
        event_writer.assert_awaited_once()

    async def test_non_duplicate_insert_failure_is_not_reported_as_existing(self):
        database = CommandDatabase(
            execute_error=RuntimeError("database write timed out"),
            existing={"id": 99},
        )
        data = LotteryCreate(raw_url="https://t.bilibili.com/123")

        with (
            patch("app.api.lotteries.database", database),
            patch("app.api.lotteries.require_min_role", return_value={"actor_id": "operator-1"}),
            patch("app.api.lotteries.get_platform", return_value={"label": "Bilibili"}),
            patch("app.api.lotteries.validate_lottery_target", return_value=valid_target()),
            patch("app.api.lotteries.canonicalize_lottery_url", new=AsyncMock(return_value="canonical://bilibili/dynamic/123")),
        ):
            with self.assertRaisesRegex(RuntimeError, "database write timed out"):
                await create_lottery(data, object())

        self.assertEqual(0, database.fetch_count)

    async def test_mysql_1062_is_confirmed_by_canonical_row(self):
        database = CommandDatabase(
            execute_error=DuplicateEntryError(1062, "Duplicate entry"),
            existing={"id": 99},
        )
        data = LotteryCreate(raw_url="https://t.bilibili.com/123")

        with (
            patch("app.api.lotteries.database", database),
            patch("app.api.lotteries.require_min_role", return_value={"actor_id": "operator-1"}),
            patch("app.api.lotteries.get_platform", return_value={"label": "Bilibili"}),
            patch("app.api.lotteries.validate_lottery_target", return_value=valid_target()),
            patch("app.api.lotteries.canonicalize_lottery_url", new=AsyncMock(return_value="canonical://bilibili/dynamic/123")),
        ):
            result = await create_lottery(data, object())

        self.assertEqual({"status": "exists", "id": 99}, result)
        self.assertEqual(1, database.fetch_count)


class ImportLotteryCommitTests(unittest.IsolatedAsyncioTestCase):
    async def _import(self, database, *, event_error=None):
        event_writer = AsyncMock(side_effect=event_error) if event_error else AsyncMock(return_value="event-1")
        data = LotteryTargetImport(content="https://t.bilibili.com/123")
        with (
            patch("app.api.lotteries.database", database),
            patch("app.api.lotteries.require_min_role", return_value={"actor_id": "operator-1"}),
            patch("app.api.lotteries.get_platform", return_value={"label": "Bilibili"}),
            patch("app.api.lotteries.parse_target_lines", return_value=[imported_row()]),
            patch("app.api.lotteries.validate_lottery_target", return_value=valid_target()),
            patch("app.api.lotteries.canonicalize_lottery_url", new=AsyncMock(return_value="canonical://bilibili/dynamic/123")),
            patch("app.api.lotteries.record_event", new=event_writer),
            patch("app.api.lotteries.structured_log"),
        ):
            return await import_lottery_targets(data, object()), event_writer

    async def test_event_failure_does_not_reclassify_created_row(self):
        result, event_writer = await self._import(
            CommandDatabase(execute_result=7),
            event_error=RuntimeError("event store unavailable"),
        )

        self.assertEqual(1, result["created_count"])
        self.assertEqual(0, result["duplicate_count"])
        self.assertEqual(0, result["invalid_count"])
        self.assertEqual(2, event_writer.await_count)

    async def test_non_duplicate_insert_failure_stays_invalid_even_if_row_exists(self):
        database = CommandDatabase(
            execute_error=RuntimeError("database write timed out"),
            existing={"id": 99},
        )
        result, _ = await self._import(database)

        self.assertEqual(0, result["created_count"])
        self.assertEqual(0, result["duplicate_count"])
        self.assertEqual(1, result["invalid_count"])
        self.assertEqual(0, database.fetch_count)

    async def test_mysql_1062_with_matching_row_is_duplicate(self):
        database = CommandDatabase(
            execute_error=DuplicateEntryError(1062, "Duplicate entry"),
            existing={"id": 99},
        )
        result, _ = await self._import(database)

        self.assertEqual(0, result["created_count"])
        self.assertEqual(1, result["duplicate_count"])
        self.assertEqual(0, result["invalid_count"])
        self.assertEqual(1, database.fetch_count)


class DispatchCommitTests(unittest.IsolatedAsyncioTestCase):
    async def test_repair_dispatch_is_blocked_before_claim_until_intent_is_bound(self):
        database = CommandDatabase(existing={"id": 7, "platform": "bilibili"})
        repair_plan = {"eligible": True, "missing_actions": ["commented"]}

        with (
            patch("app.api.lotteries.database", database),
            patch("app.api.lotteries.require_min_role", return_value={"actor_id": "operator-1"}),
            patch("app.api.lotteries.build_lottery_repair_plan", new=AsyncMock(return_value=repair_plan)),
        ):
            with self.assertRaises(HTTPException) as caught:
                await dispatch_lottery_repair(
                    7,
                    DispatchTaskRequest(dry_run=False, confirm=True),
                    object(),
                )

        self.assertEqual(503, caught.exception.status_code)
        self.assertEqual(
            "repair_intent_binding_not_implemented",
            caught.exception.detail["code"],
        )
        self.assertEqual(0, database.execute_count)

    async def test_committed_dispatch_returns_task_when_both_event_writes_raise(self):
        lottery = {
            "id": 7,
            "platform": "bilibili",
            "raw_url": "https://t.bilibili.com/123",
            "canonical_url": "canonical://bilibili/dynamic/123",
            "rule_text": "抽奖：点赞",
            "action_plan": "{}",
            "status": "pending",
            "execution_lock": None,
        }
        database = DispatchDatabase(lottery)
        event_writer = AsyncMock(side_effect=RuntimeError("event store unavailable"))
        enqueue = AsyncMock()
        account_lease = SimpleNamespace(lease_id="lease-1", generation=1)
        acquire_lease = AsyncMock(return_value=account_lease)
        bind_lease = AsyncMock()

        with (
            patch("app.api.lotteries.database", database),
            patch("app.api.lotteries.require_min_role", return_value={"actor_id": "operator-1"}),
            patch("app.api.lotteries.get_platform", return_value={"action_adapter": False}),
            patch("app.api.lotteries.load_runtime_selector_config", new=AsyncMock(return_value={})),
            patch("app.api.lotteries.validate_lottery_target", return_value=valid_target()),
            patch(
                "app.api.lotteries.pick_account",
                new=AsyncMock(return_value={"id": 11, "execution_revision": 1}),
            ),
            patch(
                "app.api.lotteries.acquire_account_operation_lease",
                new=acquire_lease,
            ),
            patch("app.api.lotteries.bind_lease_to_task", new=bind_lease),
            patch("app.api.lotteries.build_lottery_task_message", return_value={"task_id": "task-1"}),
            patch("app.api.lotteries.enqueue_outbox", new=enqueue),
            patch("app.api.lotteries.try_flush_dedup", new=AsyncMock()),
            patch("app.api.lotteries.record_event", new=event_writer),
            patch("app.api.lotteries.structured_log"),
            patch("app.api.lotteries.uuid.uuid4", return_value="task-1"),
        ):
            result = await dispatch_lottery(7, DispatchTaskRequest(dry_run=True), object())

        self.assertEqual("queued", result["status"])
        self.assertEqual("task-1", result["task_id"])
        self.assertEqual(2, len(database.executions))
        acquire_lease.assert_awaited_once_with(
            11,
            operation_kind="dry_run",
            owner_id="task-1",
            expected_execution_revision=1,
            expected_platform="bilibili",
            db=database,
        )
        bind_lease.assert_awaited_once_with(account_lease, "task-1", db=database)
        enqueue.assert_awaited_once()
        self.assertEqual(2, event_writer.await_count)


if __name__ == "__main__":
    unittest.main()
