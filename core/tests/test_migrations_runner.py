import base64
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode())
os.environ.setdefault("UPDATE_SECRET", "test-secret")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")

from app.migrations_runner import (  # noqa: E402
    MIGRATIONS_DIR,
    discover_migrations,
    parse_version,
    pending_migrations,
    split_statements,
)


class ParseVersionTests(unittest.TestCase):
    def test_valid_names(self):
        self.assertEqual(parse_version("0001_baseline.sql"), "0001")
        self.assertEqual(parse_version("0042_add_index.sql"), "0042")

    def test_invalid_names(self):
        self.assertIsNone(parse_version("baseline.sql"))
        self.assertIsNone(parse_version("001_too_short.sql"))
        self.assertIsNone(parse_version("0001_baseline.txt"))
        self.assertIsNone(parse_version("README.md"))


class DiscoverAndPendingTests(unittest.TestCase):
    def test_discovers_in_version_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "0002_second.sql").write_text("SELECT 1;")
            (tmp / "0001_first.sql").write_text("SELECT 1;")
            (tmp / "notes.txt").write_text("ignore me")
            found = discover_migrations(tmp)
            self.assertEqual([v for v, _ in found], ["0001", "0002"])

    def test_duplicate_versions_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "0001_a.sql").write_text("SELECT 1;")
            (tmp / "0001_b.sql").write_text("SELECT 1;")
            with self.assertRaises(ValueError):
                discover_migrations(tmp)

    def test_missing_dir_is_empty(self):
        self.assertEqual(discover_migrations(Path("/no/such/dir")), [])

    def test_pending_excludes_applied(self):
        migrations = [("0001", Path("a")), ("0002", Path("b")), ("0003", Path("c"))]
        pending = pending_migrations(migrations, applied={"0001", "0002"})
        self.assertEqual([v for v, _ in pending], ["0003"])

    def test_pending_empty_when_all_applied(self):
        migrations = [("0001", Path("a"))]
        self.assertEqual(pending_migrations(migrations, applied={"0001"}), [])


class SplitStatementsTests(unittest.TestCase):
    def test_splits_and_strips_comments(self):
        sql = """
        -- a comment
        CREATE TABLE x (id INT);
        ALTER TABLE x ADD COLUMN y INT;
        """
        statements = split_statements(sql)
        self.assertEqual(len(statements), 2)
        self.assertTrue(statements[0].startswith("CREATE TABLE x"))
        self.assertTrue(statements[1].startswith("ALTER TABLE x"))

    def test_blank_and_comment_only_yield_nothing(self):
        self.assertEqual(split_statements("-- just a comment\n\n   "), [])


class RealBaselineTests(unittest.TestCase):
    def test_baseline_migration_present_and_named(self):
        found = discover_migrations(MIGRATIONS_DIR)
        versions = [v for v, _ in found]
        self.assertIn("0001", versions)

    def test_baseline_statements_parse(self):
        found = dict(discover_migrations(MIGRATIONS_DIR))
        statements = split_statements(Path(found["0001"]).read_text(encoding="utf-8"))
        self.assertTrue(statements)
        self.assertTrue(all(stmt.strip() for stmt in statements))


if __name__ == "__main__":
    unittest.main()
