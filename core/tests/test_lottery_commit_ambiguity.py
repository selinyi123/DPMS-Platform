import base64
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("ENCRYPTION_KEY", base64.b64encode(b"0" * 32).decode())
os.environ.setdefault("UPDATE_SECRET", "test-secret")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")

from app.api.lotteries import (  # noqa: E402
    RealRunCompletionAuthority,
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
                "authoritative_rule_snapshot_id",
                "rule_hash",
                "action_plan_hash",
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
            patch(
                "app.api.lotteries.REPAIR_DISPATCH_INTENT_BINDING_READY",
                False,
            ),
            patch("app.api.lotteries.require_min_role", return_value={"actor_id": "operator-1"}),
            patch("app.api.lotteries.build_lottery_repair_plan", new=AsyncMock(return_value=repair_plan)),
            patch(
                "app.api.lotteries._record_repair_rejection",
                new=AsyncMock(),
            ),
        ):
            with self.assertRaises(HTTPException) as caught:
                await dispatch_lottery_repair(
                    7,
                    DispatchTaskRequest(dry_run=False, confirm=True),
                    object(),
                )

        self.assertEqual(503, caught.exception.status_code)
        self.assertEqual(
            "worker_repair_intent_contract_not_ready",
            caught.exception.detail["code"],
        )
        self.assertEqual(0, database.execute_count)

    async def test_enabled_repair_uses_defined_platform_module_and_real_run_policy(self):
        lottery = {
            "id": 7,
            "platform": "weibo",
            "raw_url": "https://weibo.com/123456/PCAGRFqKj",
            "canonical_url": "canonical://weibo/status/PCAGRFqKj",
            "action_plan": {
                "execution_path_id": "weibo_oauth_v1",
                "required_actions": ["followed", "commented"],
            },
        }
        database = CommandDatabase(existing=lottery)
        repair_plan = {
            "eligible": True,
            "missing_actions": ["commented"],
            "repair_action_plan": {},
        }
        policy_calls = []
        frozen_intent = SimpleNamespace(
            source_account_id=41,
            full_action_plan=lottery["action_plan"],
            execution_path_id="weibo_oauth_v1",
            full_required_actions=("followed", "commented"),
        )

        class RecordingPlatformModule:
            real_run_supported = True
            real_run_blocker = None

            def execution_path_blockers(self, _path):
                return []

            def requires_public_ingress(self, **_context):
                return False

            def account_execution_path_for_dispatch(
                self,
                *,
                task_mode,
                stored_execution_path,
                operation_kind,
            ):
                policy_calls.append(
                    (
                        "path",
                        task_mode,
                        operation_kind,
                        stored_execution_path,
                    )
                )
                return stored_execution_path

            def account_required_actions_for_dispatch(
                self,
                *,
                required_actions,
                task_mode,
                operation_kind,
            ):
                policy_calls.append(
                    (
                        "actions",
                        task_mode,
                        operation_kind,
                        tuple(required_actions),
                    )
                )
                return tuple(required_actions)

        picker = AsyncMock(return_value=None)
        with (
            patch("app.api.lotteries.database", database),
            patch(
                "app.api.lotteries.REPAIR_DISPATCH_INTENT_BINDING_READY",
                True,
            ),
            patch(
                "app.api.lotteries.repair_dispatch_workers_ready",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "app.api.lotteries.require_min_role",
                return_value={"actor_id": "operator-1"},
            ),
            patch(
                "app.api.lotteries.build_lottery_repair_plan",
                new=AsyncMock(return_value=repair_plan),
            ),
            patch(
                "app.api.lotteries.load_lottery_execution_intent",
                new=AsyncMock(return_value=frozen_intent),
            ),
            patch(
                "app.api.lotteries.validate_lottery_execution_intent_binding",
            ),
            patch(
                "app.api.lotteries.get_platform_module",
                return_value=RecordingPlatformModule(),
            ),
            patch(
                "app.api.lotteries.get_platform",
                return_value={"action_adapter": True},
            ),
            patch(
                "app.api.lotteries.load_runtime_selector_config",
                new=AsyncMock(return_value={}),
            ),
            patch(
                "app.api.lotteries.validate_lottery_target",
                return_value=SimpleNamespace(valid=True, reason=None),
            ),
            patch("app.api.lotteries.require_confirmation"),
            patch(
                "app.api.lotteries.is_real_run_enabled",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "app.api.lotteries.circuit_breaker_allows",
                new=AsyncMock(return_value=(True, None)),
            ),
            patch(
                "app.api.lotteries.validate_real_run_evidence",
                new=AsyncMock(return_value={"allowed": True}),
            ),
            patch("app.api.lotteries.pick_account", new=picker),
            patch(
                "app.api.lotteries._record_repair_rejection",
                new=AsyncMock(),
            ),
            patch(
                "app.api.lotteries._record_post_commit_event",
                new=AsyncMock(),
            ),
        ):
            with self.assertRaises(HTTPException) as caught:
                await dispatch_lottery_repair(
                    7,
                    DispatchTaskRequest(dry_run=False, confirm=True),
                    object(),
                )

        self.assertEqual(409, caught.exception.status_code)
        self.assertEqual(
            [
                (
                    "path",
                    "real_run",
                    "repair",
                    "weibo_oauth_v1",
                ),
                (
                    "actions",
                    "real_run",
                    "repair",
                    ("commented",),
                ),
            ],
            policy_calls,
        )
        picker.assert_awaited_once_with(
            41,
            "weibo",
            execution_path_id="weibo_oauth_v1",
            required_actions=("commented",),
            require_account_capability=True,
        )

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

    async def test_real_dispatch_locks_account_before_exact_evidence_revalidation(self):
        lottery = {
            "id": 7,
            "platform": "bilibili",
            "raw_url": "https://example.test/lottery/7",
            "canonical_url": "canonical://bilibili/item/7",
            "rule_text": "follow and comment",
            "action_plan": {
                "execution_path_id": "test_v1",
                "required_actions": ["followed", "commented"],
            },
            "status": "pending",
            "execution_lock": None,
            "authoritative_rule_snapshot_id": 3,
            "rule_hash": "a" * 64,
            "action_plan_hash": "b" * 64,
        }
        database = DispatchDatabase(lottery)
        ordering = []

        class RecordingPlatformModule:
            dry_run_supported = True
            real_run_supported = True
            real_run_blocker = None
            requires_exact_real_run_evidence = True

            def non_executable_error(self, _path):
                return None

            def execution_path_blockers(self, _path):
                return []

            def requires_public_ingress(self, **_context):
                return False

            def account_execution_path_for_dispatch(
                self,
                *,
                task_mode,
                stored_execution_path,
            ):
                return stored_execution_path

            def account_required_actions_for_dispatch(self, **context):
                return tuple(context["required_actions"])

            def build_dispatch_plan_binding(self, **_context):
                return {
                    "rule_snapshot_id": 3,
                    "rule_hash": "a" * 64,
                    "action_plan_hash": "b" * 64,
                    "execution_path_id": "test_v1",
                    "target_hash": "c" * 64,
                    "config_hash": "d" * 64,
                    "execution_revision": 1,
                    "required_actions": ("followed", "commented"),
                    "follow_target_handle": "@target",
                    "action_plan": lottery["action_plan"],
                }

            async def revalidate_exact_execution_evidence(self, **_context):
                ordering.append("evidence")

        platform_module = RecordingPlatformModule()
        intent_binding = SimpleNamespace(
            intent_id="intent-1",
            intent_hash="e" * 64,
            binding_hash="f" * 64,
            message_fields=lambda: {},
        )
        persist_intent = AsyncMock(return_value=intent_binding)

        async def acquire_lease(*_args, **_kwargs):
            ordering.append("lease")
            return SimpleNamespace(lease_id="lease-1", generation=1)

        async def completed_actions(*_args, **_kwargs):
            ordering.append("completed-actions")
            return RealRunCompletionAuthority(())

        with (
            patch("app.api.lotteries.database", database),
            patch(
                "app.api.lotteries.require_min_role",
                return_value={"actor_id": "operator-1"},
            ),
            patch(
                "app.api.lotteries.get_platform_module",
                return_value=platform_module,
            ),
            patch(
                "app.api.lotteries.get_platform",
                return_value={"label": "Test", "action_adapter": True},
            ),
            patch(
                "app.api.lotteries.load_runtime_selector_config",
                new=AsyncMock(return_value={}),
            ),
            patch(
                "app.api.lotteries.validate_lottery_target",
                return_value=valid_target(),
            ),
            patch("app.api.lotteries.require_confirmation"),
            patch(
                "app.api.lotteries.is_real_run_enabled",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "app.api.lotteries.circuit_breaker_allows",
                new=AsyncMock(return_value=(True, None)),
            ),
            patch(
                "app.api.lotteries.pick_account",
                new=AsyncMock(
                    return_value={"id": 11, "execution_revision": 1}
                ),
            ),
            patch(
                "app.api.lotteries.evaluate_real_run_decision",
                new=AsyncMock(
                    return_value={
                        "allowed": True,
                        "decision_id": "decision-1",
                        "policy_version": "policy-1",
                        "gate": {"execution_evidence_id": "evidence-1"},
                    }
                ),
            ),
            patch(
                "app.api.lotteries.acquire_account_operation_lease",
                new=AsyncMock(side_effect=acquire_lease),
            ),
            patch(
                "app.api.lotteries.load_real_run_completion_authority",
                new=AsyncMock(side_effect=completed_actions),
            ),
            patch(
                "app.api.lotteries.bind_lease_to_task",
                new=AsyncMock(),
            ),
            patch(
                "app.api.lotteries.persist_full_execution_intent",
                new=persist_intent,
            ),
            patch("app.api.lotteries.enqueue_outbox", new=AsyncMock()),
            patch("app.api.lotteries.try_flush_dedup", new=AsyncMock()),
            patch("app.api.lotteries.audit_event", new=AsyncMock()),
            patch(
                "app.api.lotteries._record_post_commit_event",
                new=AsyncMock(),
            ),
            patch("app.api.lotteries.uuid.uuid4", return_value="task-1"),
        ):
            result = await dispatch_lottery(
                7,
                DispatchTaskRequest(dry_run=False, confirm=True),
                object(),
            )

        self.assertEqual("queued", result["status"])
        self.assertEqual(
            ["lease", "completed-actions", "evidence"],
            ordering,
        )
        self.assertIs(
            persist_intent.await_args.kwargs[
                "allow_current_intent_supersede"
            ],
            True,
        )


if __name__ == "__main__":
    unittest.main()
