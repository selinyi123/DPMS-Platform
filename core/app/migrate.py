"""Explicit production schema migration and verification entry point."""

from __future__ import annotations

import asyncio
import warnings

from app.db import allow_schema_writes, database
from app.config import settings
from app.migrations_runner import (
    run_migrations,
    schema_upgrade_process_lock,
    verify_migrations_current,
)
from app.runtime_schema import ensure_runtime_schema
from app.utils.log import structured_log
from shared.database_credentials import require_production_database_credentials


async def migrate_and_verify() -> list[str]:
    """Apply the ordered schema contract, then perform read-only verification."""

    require_production_database_credentials(
        settings.database_url,
        deployment_mode=settings.deployment_mode,
        role="migration",
        expected_username=settings.mysql_migration_user,
    )
    await database.connect()
    try:
        async with schema_upgrade_process_lock():
            # The historical baseline still depends on idempotent bootstrap
            # DDL. The outer advisory lock covers both this phase and the
            # versioned runner so concurrent migration commands cannot race.
            with warnings.catch_warnings():
                warnings.filterwarnings(
                    "ignore",
                    message=r"Table '.*' already exists",
                )
                with allow_schema_writes():
                    await ensure_runtime_schema()
            applied = await run_migrations()
            await verify_migrations_current()
        structured_log(
            "info",
            "schema_migration_verified",
            applied_versions=",".join(applied),
        )
        return applied
    finally:
        await database.disconnect()


def main() -> None:
    asyncio.run(migrate_and_verify())


if __name__ == "__main__":
    main()
