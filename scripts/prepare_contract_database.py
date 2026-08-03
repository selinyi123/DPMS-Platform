"""Prepare a strictly local, disposable MySQL schema for real contract tests.

This script intentionally refuses ordinary application database names and
hosts outside loopback or an explicitly named disposable container. It is a
CI/test bootstrap, not a production migration entrypoint.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parent.parent
CORE_ROOT = ROOT / "core"
sys.path.insert(0, str(CORE_ROOT))
sys.path.insert(0, str(ROOT))

DISPOSABLE_CONTAINER_HOST_RE = re.compile(
    # The fixed prefix is 20 characters; keep the whole DNS label within 63.
    r"^dpms-contract-mysql-[a-z0-9](?:[a-z0-9-]{0,41}[a-z0-9])?$"
)


def _validated_database_url() -> str:
    value = os.environ.get("DATABASE_URL", "").strip()
    if os.environ.get("DPMS_MYSQL_INTEGRATION") != "1":
        raise SystemExit("DPMS_MYSQL_INTEGRATION=1 is required")
    if os.environ.get("DPMS_CONTRACT_DATABASE_BOOTSTRAP") != "1":
        raise SystemExit("DPMS_CONTRACT_DATABASE_BOOTSTRAP=1 is required")
    parsed = urlsplit(value)
    database_name = parsed.path.lstrip("/").split("/", 1)[0]
    if parsed.scheme != "mysql+aiomysql":
        raise SystemExit("contract database must use mysql+aiomysql")
    host = (parsed.hostname or "").casefold()
    if (
        host not in {"127.0.0.1", "localhost", "::1"}
        and not DISPOSABLE_CONTAINER_HOST_RE.fullmatch(host)
    ):
        raise SystemExit(
            "contract database must be loopback or a single-label "
            "dpms-contract-mysql-* container"
        )
    if not database_name.startswith("dpms_contract_"):
        raise SystemExit("contract database name must start with dpms_contract_")
    return value


def _validated_admin_database_url(application_url: str) -> str | None:
    """Validate the optional, disposable-only connection used for trigger DDL."""

    value = os.environ.get(
        "DPMS_CONTRACT_DATABASE_ADMIN_URL",
        "",
    ).strip()
    if not value:
        return None
    application = urlsplit(application_url)
    admin = urlsplit(value)
    if admin.scheme != "mysql+aiomysql":
        raise SystemExit("contract admin database must use mysql+aiomysql")
    application_host = (application.hostname or "").casefold()
    admin_host = (admin.hostname or "").casefold()
    if admin_host != application_host:
        raise SystemExit(
            "contract admin database must use the validated application host"
        )
    if (admin.port or 3306) != (application.port or 3306):
        raise SystemExit(
            "contract admin database must use the validated application port"
        )
    if admin.path.lstrip("/").split("/", 1)[0] != "mysql":
        raise SystemExit("contract admin database must be the mysql system schema")
    if not admin.username:
        raise SystemExit("contract admin database user is required")
    return value


async def _enable_disposable_trigger_ddl(admin_url: str | None) -> None:
    if admin_url is None:
        return

    # MySQL 8 enables binary logging by default. The official test service
    # therefore rejects CREATE TRIGGER from the schema-scoped application user
    # unless this server-wide switch is enabled first. The URL above is
    # constrained to the same already-validated disposable host and port.
    from databases import Database

    admin_database = Database(admin_url)
    await admin_database.connect()
    try:
        await admin_database.execute(
            "SET GLOBAL log_bin_trust_function_creators = 1"
        )
        enabled = await admin_database.fetch_val(
            "SELECT @@GLOBAL.log_bin_trust_function_creators"
        )
        if int(enabled or 0) != 1:
            raise RuntimeError(
                "disposable MySQL refused trigger-DDL test configuration"
            )
    finally:
        await admin_database.disconnect()


async def _prepare() -> None:
    application_url = _validated_database_url()
    admin_url = _validated_admin_database_url(application_url)
    await _enable_disposable_trigger_ddl(admin_url)

    # Import only after validating the environment because Settings captures
    # DATABASE_URL at module import time.
    from app.db import database
    from app.migrations_runner import (
        run_migrations,
        split_statements,
        verify_production_schema,
    )
    from app.runtime_schema import ensure_runtime_schema

    bootstrap_sql = (
        ROOT / "docker" / "mysql" / "001-bootstrap.sql"
    ).read_text(encoding="utf-8")
    await database.connect()
    try:
        for statement in split_statements(bootstrap_sql):
            await database.execute(statement)
        await ensure_runtime_schema()
        await run_migrations()
        await verify_production_schema()
    finally:
        await database.disconnect()


if __name__ == "__main__":
    asyncio.run(_prepare())
