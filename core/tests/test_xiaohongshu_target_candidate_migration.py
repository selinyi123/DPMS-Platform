import base64
import os
import unittest
from pathlib import Path


os.environ.setdefault(
    "ENCRYPTION_KEY",
    base64.b64encode(b"0" * 32).decode(),
)
os.environ.setdefault("UPDATE_SECRET", "test-secret")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")

from app import migrations_runner  # noqa: E402


CORE_ROOT = Path(__file__).resolve().parents[1]
MIGRATION = (
    CORE_ROOT / "migrations" / "0026_xiaohongshu_target_candidates.sql"
)
RETENTION_MIGRATION = (
    CORE_ROOT
    / "migrations"
    / "0027_xiaohongshu_target_evidence_retention.sql"
)
ROLLBACK = (
    CORE_ROOT
    / "migrations"
    / "rollback"
    / "0026_xiaohongshu_target_candidates.down.sql"
)
RETENTION_ROLLBACK = (
    CORE_ROOT
    / "migrations"
    / "rollback"
    / "0027_xiaohongshu_target_evidence_retention.down.sql"
)
INIT_SQL = CORE_ROOT.parent / "init.sql"


class XiaohongshuTargetCandidateMigrationTests(unittest.TestCase):
    def test_0026_is_discoverable_and_defines_dedicated_projection(self):
        discovered = dict(
            migrations_runner.discover_migrations(CORE_ROOT / "migrations")
        )
        self.assertEqual(MIGRATION, discovered["0026"])

        sql = MIGRATION.read_text(encoding="utf-8")
        statements = migrations_runner.split_statements(sql)
        self.assertEqual(3, len(statements))
        for table in (
            "xiaohongshu_target_sources",
            "xiaohongshu_target_candidates",
            "xiaohongshu_target_candidate_source_hits",
        ):
            self.assertIn(f"CREATE TABLE IF NOT EXISTS {table}", sql)
        self.assertNotIn("INSERT INTO tracked_sources", sql)
        self.assertNotIn("run_discovery", "\n".join(statements))
        self.assertIn(
            "decision_status IN ('pending', 'accepted', 'skipped', "
            "'needs_review')",
            " ".join(sql.split()),
        )
        self.assertIn(
            "UNIQUE KEY uk_xhs_target_candidate_url_hash (url_hash)",
            " ".join(sql.split()),
        )

    def test_rollback_refuses_to_discard_any_authoritative_rows(self):
        sql = ROLLBACK.read_text(encoding="utf-8")
        compact = " ".join(sql.split())
        self.assertIn(
            "CHECK (persisted_rows = 0) ENFORCED",
            compact,
        )
        for table in (
            "xiaohongshu_target_candidate_source_hits",
            "xiaohongshu_target_candidates",
            "xiaohongshu_target_sources",
        ):
            self.assertIn(f"SELECT COUNT(*) FROM {table}", compact)
        statements = migrations_runner.split_statements(sql)
        self.assertEqual(
            "DELETE FROM schema_migrations WHERE version = '0026'",
            statements[-1],
        )

    def test_0027_makes_source_hit_evidence_restrictive(self):
        discovered = dict(
            migrations_runner.discover_migrations(CORE_ROOT / "migrations")
        )
        self.assertEqual(RETENTION_MIGRATION, discovered["0027"])

        sql = RETENTION_MIGRATION.read_text(encoding="utf-8")
        statements = migrations_runner.split_statements(sql)
        drop_statement = next(
            statement
            for statement in statements
            if "DROP FOREIGN KEY fk_xhs_target_hit_candidate" in statement
        )
        retention_statement = " ".join(statements[-1].split())
        self.assertIn(
            "DROP FOREIGN KEY fk_xhs_target_hit_candidate",
            drop_statement,
        )
        self.assertIn("ON DELETE RESTRICT", retention_statement)
        self.assertIn("ON UPDATE RESTRICT", retention_statement)
        self.assertNotIn("ON DELETE CASCADE", retention_statement)

        rollback_sql = RETENTION_ROLLBACK.read_text(encoding="utf-8")
        rollback_statements = migrations_runner.split_statements(rollback_sql)
        self.assertIn("ON DELETE CASCADE", " ".join(rollback_sql.split()))
        self.assertEqual(
            "DELETE FROM schema_migrations WHERE version = '0027'",
            rollback_statements[-1],
        )

    def test_production_schema_contract_covers_projection(self):
        expected_tables = {
            "xiaohongshu_target_sources",
            "xiaohongshu_target_candidates",
            "xiaohongshu_target_candidate_source_hits",
        }
        self.assertTrue(
            expected_tables.issubset(
                migrations_runner.PRODUCTION_REQUIRED_TABLES
            )
        )
        self.assertTrue(
            {
                "evidence",
                "rule",
                "classification",
                "decision_status",
                "accepted_lottery_id",
                "version",
            }.issubset(
                migrations_runner.PRODUCTION_REQUIRED_COLUMNS[
                    "xiaohongshu_target_candidates"
                ]
            )
        )
        self.assertEqual(
            ("url_hash",),
            migrations_runner.PRODUCTION_REQUIRED_UNIQUE_INDEXES[
                (
                    "xiaohongshu_target_candidates",
                    "uk_xhs_target_candidate_url_hash",
                )
            ],
        )
        self.assertEqual(
            (
                ("accepted_lottery_id",),
                "lotteries",
                ("id",),
            ),
            migrations_runner.PRODUCTION_REQUIRED_FOREIGN_KEYS[
                (
                    "xiaohongshu_target_candidates",
                    "fk_xhs_target_candidate_lottery",
                )
            ],
        )
        self.assertIn(
            (
                "xiaohongshu_target_candidates",
                "chk_xhs_target_candidate_accept_binding",
            ),
            migrations_runner.PRODUCTION_REQUIRED_CHECK_CONSTRAINTS,
        )

    def test_baseline_init_contains_the_same_projection(self):
        sql = INIT_SQL.read_text(encoding="utf-8")
        for table in (
            "xiaohongshu_target_sources",
            "xiaohongshu_target_candidates",
            "xiaohongshu_target_candidate_source_hits",
        ):
            self.assertIn(f"CREATE TABLE IF NOT EXISTS `{table}`", sql)
        source_hit_table = sql.split(
            "CREATE TABLE IF NOT EXISTS "
            "`xiaohongshu_target_candidate_source_hits`",
            maxsplit=1,
        )[1].split(") ENGINE=InnoDB;", maxsplit=1)[0]
        self.assertIn("ON DELETE RESTRICT", source_hit_table)
        self.assertNotIn("ON DELETE CASCADE", source_hit_table)


if __name__ == "__main__":
    unittest.main()
