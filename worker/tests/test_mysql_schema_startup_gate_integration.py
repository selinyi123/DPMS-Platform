"""Real-MySQL contract for the Worker's exact release-ledger startup gate."""

from __future__ import annotations

import os
import unittest

from app.db import database
from app.event_store.service import verify_event_schema


@unittest.skipUnless(
    os.getenv("DPMS_MYSQL_WORKER_GATE_INTEGRATION") == "1",
    "requires the disposable real-MySQL contract database",
)
class WorkerSchemaStartupGateIntegrationTests(
    unittest.IsolatedAsyncioTestCase
):
    async def asyncSetUp(self) -> None:
        await database.connect()

    async def asyncTearDown(self) -> None:
        await database.disconnect()

    async def test_runtime_role_accepts_the_exact_shipped_migration_ledger(
        self,
    ) -> None:
        await verify_event_schema()


if __name__ == "__main__":
    unittest.main()
