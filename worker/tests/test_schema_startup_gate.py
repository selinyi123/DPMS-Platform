import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from app.event_store.service import (
    expected_migration_checksums,
    verify_event_schema,
)


class WorkerSchemaStartupGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_current_schema_uses_only_read_queries(self):
        database = AsyncMock()
        database.fetch_one.return_value = {"required_columns": 11}
        database.fetch_all.return_value = [
            {"version": "0001", "checksum": "a" * 64},
            {"version": "0002", "checksum": "b" * 64},
        ]
        with patch(
            "app.event_store.service.database",
            database,
        ), patch(
            "app.event_store.service.expected_migration_checksums",
            return_value={"0001": "a" * 64, "0002": "b" * 64},
        ):
            await verify_event_schema()

        database.fetch_one.assert_awaited_once()
        database.fetch_all.assert_awaited_once()
        database.execute.assert_not_awaited()

    async def test_missing_event_columns_fail_closed(self):
        database = AsyncMock()
        database.fetch_one.return_value = {
            "required_columns": 10,
        }
        with patch(
            "app.event_store.service.database",
            database,
        ), self.assertRaisesRegex(
            RuntimeError,
            "worker_event_schema_not_current",
        ):
            await verify_event_schema()

    async def test_missing_required_migration_fails_closed(self):
        database = AsyncMock()
        database.fetch_one.return_value = {"required_columns": 11}
        database.fetch_all.return_value = []
        with patch(
            "app.event_store.service.database",
            database,
        ), patch(
            "app.event_store.service.expected_migration_checksums",
            return_value={"0001": "a" * 64},
        ), self.assertRaisesRegex(
            RuntimeError,
            "worker_schema_migration_ledger_not_current",
        ):
            await verify_event_schema()

    async def test_checksum_mismatch_fails_closed(self):
        database = AsyncMock()
        database.fetch_one.return_value = {"required_columns": 11}
        database.fetch_all.return_value = [
            {"version": "0001", "checksum": "b" * 64},
        ]
        with patch(
            "app.event_store.service.database",
            database,
        ), patch(
            "app.event_store.service.expected_migration_checksums",
            return_value={"0001": "a" * 64},
        ), self.assertRaisesRegex(
            RuntimeError,
            "worker_schema_migration_ledger_not_current",
        ):
            await verify_event_schema()

    async def test_unknown_applied_migration_fails_closed(self):
        database = AsyncMock()
        database.fetch_one.return_value = {"required_columns": 11}
        database.fetch_all.return_value = [
            {"version": "0001", "checksum": "a" * 64},
            {"version": "0002", "checksum": "b" * 64},
        ]
        with patch(
            "app.event_store.service.database",
            database,
        ), patch(
            "app.event_store.service.expected_migration_checksums",
            return_value={"0001": "a" * 64},
        ), self.assertRaisesRegex(
            RuntimeError,
            "worker_schema_migration_ledger_not_current",
        ):
            await verify_event_schema()


class WorkerMigrationManifestTests(unittest.TestCase):
    def test_manifest_hashes_exact_bytes_and_ignores_non_migrations(self):
        with TemporaryDirectory() as tmp:
            migrations_dir = Path(tmp)
            (migrations_dir / "0001_first.sql").write_bytes(b"SELECT 1;\n")
            (migrations_dir / "notes.txt").write_text("ignored", encoding="utf-8")
            first = expected_migration_checksums(migrations_dir)
            (migrations_dir / "0001_first.sql").write_bytes(b"SELECT 2;\n")
            second = expected_migration_checksums(migrations_dir)

        self.assertEqual(set(first), {"0001"})
        self.assertNotEqual(first, second)

    def test_duplicate_versions_are_rejected(self):
        with TemporaryDirectory() as tmp:
            migrations_dir = Path(tmp)
            (migrations_dir / "0001_first.sql").write_text(
                "SELECT 1;",
                encoding="utf-8",
            )
            (migrations_dir / "0001_second.sql").write_text(
                "SELECT 2;",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                RuntimeError,
                "worker_schema_migration_manifest_duplicate_version",
            ):
                expected_migration_checksums(migrations_dir)


if __name__ == "__main__":
    unittest.main()
