"""Real-MySQL contract for the runtime/migration privilege boundary."""

from __future__ import annotations

import os
import unittest
from urllib.parse import urlsplit
from uuid import uuid4

import databases


def _validated_contract_url(variable: str) -> str:
    value = str(os.getenv(variable) or "").strip()
    if not value:
        raise unittest.SkipTest(f"{variable} is not configured")
    parsed = urlsplit(value)
    host = str(parsed.hostname or "").casefold()
    database_name = parsed.path.lstrip("/").split("/", 1)[0]
    safe_host = host in {"127.0.0.1", "localhost"} or (
        host.startswith("dpms-contract-mysql-")
        and "." not in host
    )
    if not safe_host or not database_name.startswith("dpms_contract_"):
        raise RuntimeError(f"{variable} is not a disposable contract database")
    return value


@unittest.skipUnless(
    os.getenv("DPMS_MYSQL_ROLE_INTEGRATION") == "1",
    "real MySQL role contract is opt-in",
)
class MySQLRuntimeRoleContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.runtime = databases.Database(
            _validated_contract_url("DPMS_RUNTIME_DATABASE_URL")
        )
        self.migration = databases.Database(
            _validated_contract_url("DPMS_MIGRATION_DATABASE_URL")
        )
        await self.runtime.connect()
        await self.migration.connect()

    async def asyncTearDown(self):
        await self.runtime.disconnect()
        await self.migration.disconnect()

    async def test_runtime_can_mutate_rows_but_cannot_mutate_schema(self):
        setting_key = f"role_contract_{uuid4().hex}"
        await self.runtime.execute(
            """INSERT INTO runtime_settings (setting_key, setting_value)
               VALUES (:key, '1')""",
            {"key": setting_key},
        )
        row = await self.runtime.fetch_one(
            """SELECT setting_value FROM runtime_settings
               WHERE setting_key = :key""",
            {"key": setting_key},
        )
        self.assertEqual(str(row["setting_value"]), "1")
        await self.runtime.execute(
            "DELETE FROM runtime_settings WHERE setting_key = :key",
            {"key": setting_key},
        )

        table_name = f"runtime_ddl_forbidden_{uuid4().hex[:16]}"
        with self.assertRaises(Exception) as denied:
            await self.runtime.execute(
                f"CREATE TABLE `{table_name}` (id INT PRIMARY KEY)"
            )
        self.assertRegex(
            str(denied.exception).casefold(),
            r"(create command denied|access denied|1142)",
        )
        table = await self.migration.fetch_one(
            """SELECT TABLE_NAME
               FROM information_schema.TABLES
               WHERE TABLE_SCHEMA = DATABASE()
                 AND TABLE_NAME = :table_name""",
            {"table_name": table_name},
        )
        self.assertIsNone(table)

    async def test_migration_role_can_apply_and_remove_schema_changes(self):
        table_name = f"migration_ddl_allowed_{uuid4().hex[:16]}"
        try:
            await self.migration.execute(
                f"CREATE TABLE `{table_name}` (id INT PRIMARY KEY)"
            )
            table = await self.runtime.fetch_one(
                """SELECT TABLE_NAME
                   FROM information_schema.TABLES
                   WHERE TABLE_SCHEMA = DATABASE()
                     AND TABLE_NAME = :table_name""",
                {"table_name": table_name},
            )
            self.assertEqual(str(table["TABLE_NAME"]), table_name)
        finally:
            await self.migration.execute(
                f"DROP TABLE IF EXISTS `{table_name}`"
            )

    async def test_runtime_can_read_required_trigger_metadata_without_trigger_ddl(self):
        rows = await self.runtime.fetch_all(
            "CALL dpms_required_trigger_metadata()"
        )
        self.assertTrue(rows)
        self.assertEqual(
            {
                str(row["CONTRACT_VERSION"])
                for row in rows
            },
            {"dpms-trigger-metadata-v1"},
        )
        self.assertTrue(
            {
                "trg_risk_events_active_state",
                "trg_task_runs_terminal_outbox",
            }.issubset(
                {str(row["TRIGGER_NAME"]) for row in rows}
            )
        )


if __name__ == "__main__":
    unittest.main()
