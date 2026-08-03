import base64
import os
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


os.environ.setdefault(
    "ENCRYPTION_KEY",
    base64.b64encode(os.urandom(32)).decode(),
)
os.environ.setdefault("UPDATE_SECRET", "test-secret")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")

from app.api import metrics  # noqa: E402
from app.migrations_runner import (  # noqa: E402
    MIGRATIONS_DIR,
    PRODUCTION_REQUIRED_INDEXES,
    discover_migrations,
)
from app.services.recovery_daemon import (  # noqa: E402
    STALE_RUNNING_SCAN_LIMIT,
    TaskRecoveryBlocked,
    _recover_stale_running_task_from_database,
    _recover_stale_running_tasks_for_platform,
)


class FakeStaleRunningDatabase:
    def __init__(
        self,
        *,
        task_mode="shadow_run",
        task_lease_active=False,
        account_lease_active=True,
        account_status="ready",
    ):
        self.task = {
            "task_id": "task-stale",
            "account_id": 11,
            "lottery_id": 7,
            "status": "running",
            "worker_id": "worker-lost",
            "task_mode": task_mode,
            "reconciliation_required": 0,
            "account_lease_id": "lease-stale",
            "account_lease_generation": 5,
            "lease_active": 1 if task_lease_active else 0,
        }
        self.lottery = {
            "id": 7,
            "platform": "bilibili",
            "status": "running",
            "execution_lock": "task-stale",
        }
        self.account = {"id": 11, "status": account_status}
        self.outbox = {
            "id": 41,
            "stream_key": "lottery_tasks:bilibili",
            "payload": "{}",
            "status": "sent",
            "attempts": 2,
            "dedup_key": "task-stale",
            "sent_at": "sent",
            "redis_delivery_epoch": "epoch",
            "last_error": None,
        }
        self.account_lease = {
            "lease_id": "lease-stale",
            "account_id": 11,
            "generation": 5,
            "operation_kind": task_mode,
            "owner_id": "task-stale",
            "task_id": "task-stale",
            "lease_active": 1 if account_lease_active else 0,
            "lease_unreleased": 1,
            "lease_latest_generation": 1,
            "active_account_lease_count": 1 if account_lease_active else 0,
            "released": False,
            "renewed": False,
        }
        self.breaker_status = "closed"
        self.intent_unknown = False
        self.read_order = []
        self.executions = []
        self.transaction_depth = 0

    def transaction(self):
        database = self

        class Transaction:
            async def __aenter__(self_inner):
                database.transaction_depth += 1
                return database

            async def __aexit__(self_inner, *exc):
                database.transaction_depth -= 1
                return False

        return Transaction()

    async def fetch_one(self, query, values=None):
        if "FROM outbox_events" in query:
            self.read_order.append("outbox")
            return dict(self.outbox) if self.outbox else None
        if "FROM task_runs" in query:
            self.read_order.append("task")
            return dict(self.task)
        if "FROM lotteries" in query:
            self.read_order.append("lottery")
            return dict(self.lottery)
        if "FROM accounts" in query:
            self.read_order.append("account")
            return dict(self.account)
        if "FROM account_operation_leases" in query:
            self.read_order.append("account_lease")
            return dict(self.account_lease)
        if "FROM circuit_breakers" in query:
            self.read_order.append("breaker")
            return {"status": self.breaker_status}
        raise AssertionError(f"unexpected query: {query}")

    async def execute(self, query, values=None):
        if self.transaction_depth <= 0:
            raise AssertionError("recovery write escaped transaction")
        values = dict(values or {})
        self.executions.append((query, values))
        if "UPDATE task_runs" in query and "SET status = 'queued'" in query:
            self.task.update(
                {
                    "status": "queued",
                    "worker_id": None,
                    "lease_active": 0,
                }
            )
        elif (
            "UPDATE task_runs" in query
            and "reconciliation_required = 1" in query
        ):
            self.task.update(
                {
                    "status": "failed",
                    "reconciliation_required": 1,
                    "lease_active": 0,
                }
            )
        elif "UPDATE task_runs" in query and "SET status = 'failed'" in query:
            self.task.update(
                {
                    "status": "failed",
                    "worker_id": None,
                    "lease_active": 0,
                }
            )
        elif "UPDATE lotteries" in query and "SET status = 'claimed'" in query:
            self.lottery["status"] = "claimed"
        elif "UPDATE lotteries" in query and "SET status = 'pending'" in query:
            self.lottery.update(
                {"status": "pending", "execution_lock": None}
            )
        elif (
            "UPDATE account_operation_leases" in query
            and "SET expires_at" in query
        ):
            self.account_lease["renewed"] = True
        elif (
            "UPDATE account_operation_leases" in query
            and "SET released_at" in query
        ):
            self.account_lease.update(
                {
                    "released": True,
                    "lease_unreleased": 0,
                    "lease_active": 0,
                    "active_account_lease_count": 0,
                }
            )
        elif "UPDATE outbox_events" in query and "status = 'pending'" in query:
            self.outbox.update(
                {
                    "status": "pending",
                    "attempts": 0,
                    "sent_at": None,
                    "redis_delivery_epoch": None,
                }
            )
        elif "UPDATE outbox_events" in query and "status = 'failed'" in query:
            self.outbox["status"] = "failed"
        elif "UPDATE external_action_intents" in query:
            self.intent_unknown = True
        elif "UPDATE accounts" in query and "status = 'cooling'" in query:
            if self.account["status"] == "executing":
                self.account["status"] = "cooling"
        elif "INSERT INTO circuit_breakers" in query:
            self.breaker_status = "open"
        return 1


class StaleRunningRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_scan_is_platform_scoped_bounded_and_summarized(self):
        database = AsyncMock()
        database.fetch_all.return_value = [
            {"task_id": "task-1"},
            {"task_id": "task-2"},
            {"task_id": "task-3"},
            {"task_id": "task-4"},
        ]
        recover = AsyncMock(
            side_effect=[
                "requeued_safe",
                "real_run_reconciliation_required",
                "safe_mode_recovery_failed",
                "skip_changed_task",
            ]
        )
        with patch(
            "app.services.recovery_daemon.database",
            database,
        ), patch(
            "app.services.recovery_daemon._recover_stale_running_task_from_database",
            new=recover,
        ):
            summary = await _recover_stale_running_tasks_for_platform(
                "bilibili"
            )

        query = database.fetch_all.await_args.args[0]
        values = database.fetch_all.await_args.args[1]
        self.assertIn(
            "FORCE INDEX (idx_lottery_platform_recovery)",
            query,
        )
        self.assertIn("STRAIGHT_JOIN task_runs", query)
        self.assertIn("FORCE INDEX (idx_task_run_lottery_stale)", query)
        self.assertIn("l.platform = :platform", query)
        self.assertIn("l.status = 'running'", query)
        self.assertIn("LIMIT :limit", query)
        self.assertLess(
            query.index("FROM lotteries"),
            query.index("STRAIGHT_JOIN task_runs"),
        )
        self.assertNotIn("idx_task_run_stale_running", query)
        self.assertEqual(values["platform"], "bilibili")
        self.assertEqual(values["limit"], STALE_RUNNING_SCAN_LIMIT)
        self.assertEqual(
            summary,
            {
                "examined": 4,
                "requeued_safe": 1,
                "quarantined_real": 1,
                "failed_safe": 1,
                "skipped_race": 1,
                "errors": 0,
            },
        )

    async def test_shadow_requeue_is_atomic_outbox_first_and_idempotent(self):
        database = FakeStaleRunningDatabase(task_mode="shadow_run")
        rebuild = AsyncMock(return_value={"task_id": "task-stale"})
        with patch(
            "app.services.recovery_daemon.database",
            database,
        ), patch(
            "app.services.recovery_daemon._rebuild_task_payload",
            new=rebuild,
        ):
            first = await _recover_stale_running_task_from_database(
                "task-stale",
                expected_platform="bilibili",
            )
            second = await _recover_stale_running_task_from_database(
                "task-stale",
                expected_platform="bilibili",
            )

        self.assertEqual(first, "requeued_safe")
        self.assertEqual(second, "skip_changed_task")
        self.assertEqual(database.read_order[:4], [
            "outbox",
            "task",
            "lottery",
            "account",
        ])
        self.assertEqual(database.task["status"], "queued")
        self.assertIsNone(database.task["worker_id"])
        self.assertEqual(database.lottery["status"], "claimed")
        self.assertEqual(database.outbox["status"], "pending")
        self.assertEqual(database.outbox["attempts"], 0)
        self.assertIsNone(database.outbox["redis_delivery_epoch"])
        self.assertTrue(database.account_lease["renewed"])
        rebuild.assert_awaited_once_with(
            "task-stale",
            stream_key="lottery_tasks:bilibili",
            claimed_fields={},
        )

    async def test_active_worker_lease_wins_the_scan_race(self):
        database = FakeStaleRunningDatabase(task_lease_active=True)
        rebuild = AsyncMock()
        with patch(
            "app.services.recovery_daemon.database",
            database,
        ), patch(
            "app.services.recovery_daemon._rebuild_task_payload",
            new=rebuild,
        ):
            outcome = await _recover_stale_running_task_from_database(
                "task-stale",
                expected_platform="bilibili",
            )

        self.assertEqual(outcome, "skip_owned_running_task")
        self.assertEqual(database.task["status"], "running")
        self.assertEqual(database.executions, [])
        rebuild.assert_not_awaited()

    async def test_real_run_is_quarantined_without_outbox_replay(self):
        database = FakeStaleRunningDatabase(
            task_mode="real_run",
            account_status="executing",
        )
        with patch(
            "app.services.recovery_daemon.database",
            database,
        ):
            outcome = await _recover_stale_running_task_from_database(
                "task-stale",
                expected_platform="bilibili",
            )

        self.assertEqual(outcome, "real_run_reconciliation_required")
        self.assertEqual(database.task["status"], "failed")
        self.assertEqual(database.task["reconciliation_required"], 1)
        self.assertEqual(database.lottery["status"], "running")
        self.assertEqual(database.lottery["execution_lock"], "task-stale")
        self.assertEqual(database.account["status"], "cooling")
        self.assertEqual(database.outbox["status"], "sent")
        self.assertTrue(database.intent_unknown)
        self.assertEqual(database.breaker_status, "open")
        self.assertFalse(database.account_lease["released"])
        self.assertFalse(
            any(
                "SET status = 'pending'" in query
                for query, _values in database.executions
            )
        )

    async def test_repair_run_lease_is_quarantined_and_never_remapped(self):
        database = FakeStaleRunningDatabase(
            task_mode="real_run",
            account_status="executing",
        )
        database.account_lease["operation_kind"] = "repair_run"
        database.outbox["stream_key"] = "lottery_repair_tasks:v1:bilibili"
        with patch(
            "app.services.recovery_daemon.database",
            database,
        ):
            outcome = await _recover_stale_running_task_from_database(
                "task-stale",
                expected_platform="bilibili",
            )

        self.assertEqual(outcome, "real_run_reconciliation_required")
        self.assertEqual(database.task["reconciliation_required"], 1)
        self.assertEqual(database.outbox["status"], "sent")
        self.assertFalse(database.account_lease["released"])
        self.assertFalse(database.account_lease["renewed"])

    async def test_expired_account_lease_fails_safe_mode_and_releases_claim(self):
        database = FakeStaleRunningDatabase(
            task_mode="shadow_run",
            account_lease_active=False,
        )
        with patch(
            "app.services.recovery_daemon.database",
            database,
        ), patch(
            "app.services.recovery_daemon._rebuild_task_payload",
            new=AsyncMock(return_value={"task_id": "task-stale"}),
        ):
            outcome = await _recover_stale_running_task_from_database(
                "task-stale",
                expected_platform="bilibili",
            )

        self.assertEqual(outcome, "safe_mode_recovery_failed")
        self.assertEqual(database.task["status"], "failed")
        self.assertEqual(database.lottery["status"], "pending")
        self.assertIsNone(database.lottery["execution_lock"])
        self.assertEqual(database.outbox["status"], "failed")
        self.assertTrue(database.account_lease["released"])
        self.assertFalse(database.account_lease["renewed"])

    async def test_invalid_immutable_payload_fails_instead_of_rebuilding(self):
        database = FakeStaleRunningDatabase(task_mode="dry_run")
        with patch(
            "app.services.recovery_daemon.database",
            database,
        ), patch(
            "app.services.recovery_daemon._rebuild_task_payload",
            new=AsyncMock(
                side_effect=TaskRecoveryBlocked(
                    "immutable_task_payload_binding_mismatch"
                )
            ),
        ):
            outcome = await _recover_stale_running_task_from_database(
                "task-stale",
                expected_platform="bilibili",
            )

        self.assertEqual(outcome, "safe_mode_recovery_failed")
        self.assertEqual(database.task["status"], "failed")
        self.assertEqual(database.outbox["status"], "failed")

    async def test_corrupt_lease_generation_is_settled_not_scan_poison(self):
        database = FakeStaleRunningDatabase(task_mode="shadow_run")
        database.account_lease["generation"] = "corrupt"
        with patch(
            "app.services.recovery_daemon.database",
            database,
        ), patch(
            "app.services.recovery_daemon._rebuild_task_payload",
            new=AsyncMock(return_value={"task_id": "task-stale"}),
        ):
            outcome = await _recover_stale_running_task_from_database(
                "task-stale",
                expected_platform="bilibili",
            )

        self.assertEqual(outcome, "safe_mode_recovery_failed")
        self.assertEqual(database.task["status"], "failed")
        self.assertEqual(database.outbox["status"], "failed")


class StaleRunningMetricsTests(unittest.IsolatedAsyncioTestCase):
    async def test_metrics_are_mode_and_platform_scoped(self):
        database = AsyncMock()
        database.fetch_all.return_value = [
            {"platform": "bilibili", "task_mode": "shadow_run", "cnt": 2},
            {"platform": "weibo", "task_mode": "real_run", "cnt": 1},
            {"platform": "douyin", "task_mode": "corrupt", "cnt": 3},
        ]
        with patch.object(metrics, "database", database):
            observed = await metrics._stale_running_task_observation()

        self.assertTrue(observed["available"])
        self.assertEqual(observed["total"], 6)
        self.assertEqual(
            observed["by_platform"]["bilibili"]["shadow_run"],
            2,
        )
        self.assertEqual(
            observed["by_platform"]["weibo"]["real_run"],
            1,
        )
        self.assertEqual(
            observed["by_platform"]["douyin"]["real_run"],
            3,
        )
        self.assertEqual(
            observed["by_platform"]["xiaohongshu"]["total"],
            0,
        )

    async def test_metrics_fail_closed_when_database_is_unavailable(self):
        database = AsyncMock()
        database.fetch_all.side_effect = RuntimeError("db unavailable")
        with patch.object(metrics, "database", database), patch.object(
            metrics,
            "structured_log",
        ):
            observed = await metrics._stale_running_task_observation()

        self.assertFalse(observed["available"])
        self.assertIsNone(observed["total"])
        self.assertTrue(
            all(
                item["total"] is None
                for item in observed["by_platform"].values()
            )
        )


class StaleRunningMigrationTests(unittest.TestCase):
    def test_recovery_index_is_migrated_and_verified(self):
        found = dict(discover_migrations(MIGRATIONS_DIR))
        self.assertIn("0017", found)
        sql = Path(found["0017"]).read_text(encoding="utf-8").lower()
        compact = " ".join(sql.split())
        self.assertIn(
            "idx_task_run_stale_running (status, lease_expires_at, task_id)",
            compact,
        )
        self.assertIn("information_schema.statistics", compact)
        self.assertIn("is_visible", compact)
        self.assertIn("drop index idx_task_run_stale_running", compact)
        self.assertEqual(
            PRODUCTION_REQUIRED_INDEXES[
                ("task_runs", "idx_task_run_stale_running")
            ],
            ("status", "lease_expires_at", "task_id"),
        )
        self.assertEqual(
            PRODUCTION_REQUIRED_INDEXES[
                ("lotteries", "idx_lottery_platform_recovery")
            ],
            ("platform", "status", "id"),
        )
        self.assertEqual(
            PRODUCTION_REQUIRED_INDEXES[
                ("task_runs", "idx_task_run_lottery_stale")
            ],
            ("lottery_id", "status", "lease_expires_at", "task_id"),
        )
        self.assertIn(
            "idx_task_run_lottery_stale "
            "(lottery_id, status, lease_expires_at, task_id)",
            compact,
        )
        self.assertIn(
            "idx_lottery_platform_recovery (platform, status, id)",
            compact,
        )
        rollback = (
            MIGRATIONS_DIR
            / "rollback"
            / "0017_stale_running_recovery_index.down.sql"
        )
        self.assertTrue(rollback.is_file())
        rollback_sql = rollback.read_text(encoding="utf-8").lower()
        self.assertIn(
            "drop index idx_lottery_platform_recovery",
            rollback_sql,
        )
        self.assertIn(
            "delete from schema_migrations where version = '0017'",
            rollback_sql,
        )
        self.assertGreater(
            rollback_sql.rindex(
                "delete from schema_migrations where version = '0017'"
            ),
            rollback_sql.rindex("drop index"),
        )


if __name__ == "__main__":
    unittest.main()
