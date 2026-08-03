import base64
from contextlib import asynccontextmanager
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault(
    "ENCRYPTION_KEY",
    base64.b64encode(b"0123456789abcdef0123456789abcdef").decode(),
)
os.environ.setdefault("UPDATE_SECRET", "test-secret")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")

from app.migrate import migrate_and_verify  # noqa: E402


class MigrationCommandTests(unittest.IsolatedAsyncioTestCase):
    def test_import_does_not_load_api_or_platform_modules(self):
        core_root = Path(__file__).resolve().parents[1]
        probe = """
import sys

import app.migrate

forbidden = sorted(
    module_name
    for module_name in sys.modules
    if module_name in {"app.main", "app.routers"}
    or module_name.startswith("app.api.")
    or module_name.startswith("app.platform_modules.")
)
if forbidden:
    raise SystemExit("unexpected migration imports: " + ", ".join(forbidden))
"""
        result = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=core_root,
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(
            result.returncode,
            0,
            msg=f"stdout={result.stdout}\nstderr={result.stderr}",
        )

    async def test_full_bootstrap_and_versioned_upgrade_share_outer_lock(self):
        events = []

        @asynccontextmanager
        async def process_lock():
            events.append("lock_enter")
            try:
                yield
            finally:
                events.append("lock_exit")

        async def record(name, result=None):
            events.append(name)
            return result

        async def connect():
            await record("connect")

        async def disconnect():
            await record("disconnect")

        async def baseline():
            await record("baseline")

        async def migrations():
            return await record("migrations", ["0024"])

        async def verify():
            await record("verify")

        with (
            patch(
                "app.migrate.database.connect",
                new=AsyncMock(side_effect=connect),
            ),
            patch(
                "app.migrate.database.disconnect",
                new=AsyncMock(side_effect=disconnect),
            ),
            patch(
                "app.migrate.schema_upgrade_process_lock",
                side_effect=process_lock,
            ),
            patch(
                "app.migrate.ensure_runtime_schema",
                new=AsyncMock(side_effect=baseline),
            ),
            patch(
                "app.migrate.run_migrations",
                new=AsyncMock(side_effect=migrations),
            ),
            patch(
                "app.migrate.verify_migrations_current",
                new=AsyncMock(side_effect=verify),
            ),
            patch("app.migrate.structured_log"),
        ):
            self.assertEqual(await migrate_and_verify(), ["0024"])

        self.assertEqual(
            events,
            [
                "connect",
                "lock_enter",
                "baseline",
                "migrations",
                "verify",
                "lock_exit",
                "disconnect",
            ],
        )


if __name__ == "__main__":
    unittest.main()
