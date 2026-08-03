from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

from app import migrations_runner
from app.services.account_profile_cleanup import (
    enqueue_account_profile_cleanup,
    enqueue_login_profile_cleanup,
    normalize_cleanup_identity,
    normalize_login_session_id,
)


CORE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = CORE_ROOT.parent


class AccountProfileCleanupProducerTests(unittest.IsolatedAsyncioTestCase):
    def test_identity_is_exact_and_rejects_path_like_platforms(self):
        self.assertEqual(
            normalize_cleanup_identity(7, " WeiBo "),
            (7, "weibo"),
        )
        for account_id, platform in (
            (True, "weibo"),
            (0, "weibo"),
            (7, "../weibo"),
            (7, "unknown"),
        ):
            with self.subTest(
                account_id=account_id,
                platform=platform,
            ), self.assertRaises(ValueError):
                normalize_cleanup_identity(account_id, platform)

    async def test_enqueue_persists_identity_without_a_path(self):
        database = AsyncMock()
        with patch(
            "app.services.account_profile_cleanup.database",
            database,
        ):
            await enqueue_account_profile_cleanup(7, "weibo")

        query = database.execute.await_args.args[0]
        values = database.execute.await_args.args[1]
        self.assertIn("account_profile_cleanup_intents", query)
        self.assertNotIn("profile_path", query)
        self.assertEqual(
            values,
            {"account_id": 7, "platform": "weibo"},
        )

    async def test_terminal_login_cleanup_is_uuid_bound_and_idempotent(self):
        session_id = "60dc9ca4-a25f-4ec0-89c1-29e3702a22a6"
        self.assertEqual(normalize_login_session_id(session_id), session_id)
        database = AsyncMock()
        with patch(
            "app.services.account_profile_cleanup.database",
            database,
        ):
            await enqueue_login_profile_cleanup(session_id)

        query = database.execute.await_args.args[0]
        self.assertIn("login_profile_cleanup_intents", query)
        self.assertIn("ON DUPLICATE KEY UPDATE", query)
        self.assertNotIn("profile_path", query)
        self.assertEqual(
            database.execute.await_args.args[1],
            {"session_id": session_id},
        )


class AccountProfileCleanupMigrationContractTests(unittest.TestCase):
    def setUp(self):
        self.migration = (
            CORE_ROOT
            / "migrations"
            / "0024_account_profile_cleanup_intents.sql"
        ).read_text(encoding="utf-8")
        self.rollback = (
            CORE_ROOT
            / "migrations"
            / "rollback"
            / "0024_account_profile_cleanup_intents.down.sql"
        ).read_text(encoding="utf-8")
        self.lease_migration = (
            CORE_ROOT
            / "migrations"
            / "0025_account_profile_context_leases.sql"
        ).read_text(encoding="utf-8")
        self.lease_rollback = (
            CORE_ROOT
            / "migrations"
            / "rollback"
            / "0025_account_profile_context_leases.down.sql"
        ).read_text(encoding="utf-8")

    def test_migration_is_discovered_and_backfills_soft_deleted_accounts(self):
        found = dict(migrations_runner.discover_migrations(
            CORE_ROOT / "migrations"
        ))
        self.assertIn("0024", found)
        self.assertIn(
            "CREATE TABLE IF NOT EXISTS "
            "account_profile_cleanup_intents",
            self.migration,
        )
        self.assertIn(
            "WHERE account.deleted_at IS NOT NULL",
            self.migration,
        )
        self.assertIn(
            "ON DUPLICATE KEY UPDATE",
            self.migration,
        )

    def test_schema_verifier_requires_exact_queue_contract(self):
        table = "account_profile_cleanup_intents"
        self.assertIn(table, migrations_runner.PRODUCTION_REQUIRED_TABLES)
        self.assertEqual(
            migrations_runner.PRODUCTION_REQUIRED_PRIMARY_KEYS[table],
            ("id",),
        )
        self.assertEqual(
            migrations_runner.PRODUCTION_REQUIRED_UNIQUE_INDEXES[
                (table, "uk_account_profile_cleanup_account")
            ],
            ("account_id",),
        )
        self.assertEqual(
            migrations_runner.PRODUCTION_REQUIRED_INDEXES[
                (table, "idx_account_profile_cleanup_pending")
            ],
            ("platform", "status", "next_attempt_at", "id"),
        )
        self.assertEqual(
            migrations_runner.PRODUCTION_REQUIRED_INDEXES[
                (table, "idx_account_profile_cleanup_running")
            ],
            ("platform", "status", "claimed_at", "id"),
        )
        self.assertEqual(
            migrations_runner.PRODUCTION_REQUIRED_FOREIGN_KEYS[
                (table, "fk_account_profile_cleanup_account")
            ],
            (("account_id",), "accounts", ("id",)),
        )
        for constraint in (
            "chk_account_profile_cleanup_platform",
            "chk_account_profile_cleanup_status",
            "chk_account_profile_cleanup_attempts",
            "chk_account_profile_cleanup_lifecycle",
        ):
            key = (table, constraint)
            self.assertIn(
                key,
                migrations_runner.PRODUCTION_REQUIRED_CHECK_CONSTRAINTS,
            )
            self.assertIn(
                key,
                migrations_runner.PRODUCTION_REQUIRED_EXACT_CHECK_CLAUSES,
            )

    def test_rollback_refuses_to_drop_incomplete_cleanup(self):
        self.assertIn(
            "chk_0024_no_incomplete_profile_cleanup",
            self.rollback,
        )
        self.assertIn(
            "WHERE status <> ''succeeded''",
            self.rollback,
        )
        self.assertLess(
            self.rollback.index(
                "chk_0024_no_incomplete_profile_cleanup"
            ),
            self.rollback.index(
                "DROP TABLE IF EXISTS account_profile_cleanup_intents"
            ),
        )
        self.assertIn(
            "DELETE FROM schema_migrations WHERE version = '0024'",
            self.rollback,
        )

    def test_init_schema_matches_migration_table_name(self):
        init_sql = (REPO_ROOT / "init.sql").read_text(encoding="utf-8")
        self.assertIn(
            "CREATE TABLE IF NOT EXISTS "
            "`account_profile_cleanup_intents`",
            init_sql,
        )

    def test_login_cleanup_is_backfilled_and_schema_verified(self):
        table = "login_profile_cleanup_intents"
        self.assertIn(
            "WHERE login_session.status IN "
            "('confirmed', 'failed', 'expired')",
            self.migration,
        )
        self.assertIn(table, migrations_runner.PRODUCTION_REQUIRED_TABLES)
        self.assertEqual(
            migrations_runner.PRODUCTION_REQUIRED_PRIMARY_KEYS[table],
            ("id",),
        )
        self.assertIn(
            (table, "chk_login_profile_cleanup_lifecycle"),
            migrations_runner.PRODUCTION_REQUIRED_EXACT_CHECK_CLAUSES,
        )

    def test_0025_profile_owner_lease_is_exactly_verified(self):
        table = "account_profile_context_leases"
        found = dict(
            migrations_runner.discover_migrations(
                CORE_ROOT / "migrations"
            )
        )
        self.assertIn("0025", found)
        self.assertIn(
            "CREATE TABLE IF NOT EXISTS "
            "account_profile_context_leases",
            self.lease_migration,
        )
        self.assertIn(table, migrations_runner.PRODUCTION_REQUIRED_TABLES)
        self.assertEqual(
            migrations_runner.PRODUCTION_REQUIRED_PRIMARY_KEYS[table],
            ("account_id",),
        )
        self.assertEqual(
            migrations_runner.PRODUCTION_REQUIRED_UNIQUE_INDEXES[
                (table, "uk_account_profile_context_lease_token")
            ],
            ("lease_token",),
        )
        self.assertEqual(
            migrations_runner.PRODUCTION_REQUIRED_INDEXES[
                (table, "idx_account_profile_context_lease_expiry")
            ],
            ("platform", "lease_expires_at", "account_id"),
        )
        self.assertIn(
            "chk_0025_no_active_profile_context_lease",
            self.lease_rollback,
        )
        self.assertIn(
            "WHERE lease_expires_at > NOW(6)",
            self.lease_rollback,
        )
        init_sql = (REPO_ROOT / "init.sql").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "CREATE TABLE IF NOT EXISTS "
            "`account_profile_context_leases`",
            init_sql,
        )


if __name__ == "__main__":
    unittest.main()
