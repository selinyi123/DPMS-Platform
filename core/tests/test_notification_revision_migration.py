import unittest
from pathlib import Path

from app import migrations_runner


CORE_ROOT = Path(__file__).resolve().parents[1]
MIGRATION = (
    CORE_ROOT
    / "migrations"
    / "0029_notification_config_revision.sql"
)
ROLLBACK = (
    CORE_ROOT
    / "migrations"
    / "rollback"
    / "0029_notification_config_revision.down.sql"
)


class NotificationRevisionMigrationTests(unittest.TestCase):
    def test_migration_is_additive_and_preserves_legacy_logs(self):
        discovered = dict(
            migrations_runner.discover_migrations(
                CORE_ROOT / "migrations"
            )
        )
        self.assertEqual(MIGRATION, discovered["0029"])
        sql = " ".join(MIGRATION.read_text(encoding="utf-8").split())
        self.assertIn(
            "CREATE TABLE IF NOT EXISTS notification_channel_revisions",
            sql,
        )
        self.assertIn(
            "ADD COLUMN config_revision VARCHAR(96) NULL",
            sql,
        )
        self.assertNotIn("UPDATE notify_logs SET", sql)

    def test_rollback_refuses_to_drop_bound_delivery_evidence(self):
        sql = " ".join(ROLLBACK.read_text(encoding="utf-8").split())
        self.assertIn("CHECK (revision_bound_logs = 0) ENFORCED", sql)
        self.assertIn("WHERE config_revision IS NOT NULL", sql)
        self.assertTrue(
            sql.endswith(
                "DELETE FROM schema_migrations WHERE version = '0029';"
            )
        )

    def test_production_schema_verifier_requires_revision_contract(self):
        self.assertIn(
            "notification_channel_revisions",
            migrations_runner.PRODUCTION_REQUIRED_TABLES,
        )
        self.assertIn("notify_logs", migrations_runner.PRODUCTION_REQUIRED_TABLES)
        self.assertTrue({
            "channel",
            "revision",
            "updated_at",
        } <= migrations_runner.PRODUCTION_REQUIRED_COLUMNS[
            "notification_channel_revisions"
        ])
        self.assertIn(
            "config_revision",
            migrations_runner.PRODUCTION_REQUIRED_COLUMNS["notify_logs"],
        )
        self.assertEqual(
            migrations_runner.PRODUCTION_REQUIRED_INDEXES[
                ("notify_logs", "idx_notify_delivery_revision")
            ],
            ("channel", "config_revision", "success", "created_at", "id"),
        )


if __name__ == "__main__":
    unittest.main()
