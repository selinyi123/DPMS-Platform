"""Versioned SQL migration runner (Phase 4 / ops baseline).

Replaces ad-hoc, untracked schema evolution with an ordered, recorded sequence.
Migration files live in ``core/migrations/NNNN_name.sql`` and are applied once
each, in version order, with every applied version recorded in
``schema_migrations``.

Production schema writes must go through this runner. Runtime self-heal schema
hooks are blocked by ``app.db.GuardedDatabase`` when DEPLOYMENT_MODE=production.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.config import settings
from app.db import allow_schema_writes, database
from app.utils.log import structured_log

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
VERSION_RE = re.compile(r"^(\d{4})_.+\.sql$")

PRODUCTION_REQUIRED_TABLES = {
    "schema_migrations",
    "task_runs",
    "lotteries",
    "accounts",
    "events",
    "outbox_events",
    "task_outbox_events",
    "failed_task_messages",
    "worker_heartbeats",
    "bilibili_action_ledger",
}

PRODUCTION_REQUIRED_COLUMNS = {
    "task_runs": {"task_id", "status", "task_mode", "worker_id", "stream_message_id", "lease_expires_at"},
    "lotteries": {"id", "status", "execution_lock", "locked_at"},
    "accounts": {"id", "status"},
    "task_outbox_events": {"event_kind", "status", "dedup_key", "payload"},
    "failed_task_messages": {"stream_key", "message_id", "reason", "payload"},
    "bilibili_action_ledger": {"task_id", "account_id", "lottery_id", "action", "outcome", "ok"},
}

PRODUCTION_REQUIRED_TRIGGERS = {"trg_task_runs_terminal_outbox"}


class FatalMigrationError(BaseException):
    """Bypass broad ``except Exception`` startup handlers in production."""


def parse_version(filename: str) -> str | None:
    """Return the 4-digit version prefix of a migration filename, or None."""
    match = VERSION_RE.match(filename)
    return match.group(1) if match else None


def discover_migrations(dir_path: Path) -> list[tuple[str, Path]]:
    """All migration files under ``dir_path`` as (version, path), version-ordered."""
    items: list[tuple[str, Path]] = []
    if not Path(dir_path).is_dir():
        return items
    for path in Path(dir_path).glob("*.sql"):
        version = parse_version(path.name)
        if version is not None:
            items.append((version, path))
    items.sort(key=lambda item: item[0])
    duplicates = _duplicate_versions([v for v, _ in items])
    if duplicates:
        raise ValueError(f"Duplicate migration versions: {sorted(duplicates)}")
    return items


def _duplicate_versions(versions: list[str]) -> set[str]:
    seen: set[str] = set()
    dupes: set[str] = set()
    for version in versions:
        if version in seen:
            dupes.add(version)
        seen.add(version)
    return dupes


def pending_migrations(all_migrations: list[tuple[str, Path]], applied: set[str]) -> list[tuple[str, Path]]:
    """Migrations not yet recorded as applied, preserving version order."""
    return [(version, path) for version, path in all_migrations if version not in applied]


def split_statements(sql: str) -> list[str]:
    """Split a migration file into individual statements.

    Drops ``--`` line comments and blank statements. Migration files are
    author-controlled, so a simple ``;`` split (no ``;`` inside string literals)
    is sufficient and keeps the runner dependency-free.
    """
    statements: list[str] = []
    uncommented = "\n".join(line for line in sql.splitlines() if not line.strip().startswith("--"))
    for chunk in uncommented.split(";"):
        cleaned = chunk.strip()
        if cleaned:
            statements.append(cleaned)
    return statements


def _production_mode() -> bool:
    return str(settings.deployment_mode or "").strip().lower() == "production"


def _handle_migration_error(exc: Exception) -> None:
    if _production_mode():
        raise FatalMigrationError(f"Refusing to start in production after migration failure: {exc}") from exc
    raise exc


async def run_migrations(dir_path: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply pending migrations in order; return the versions applied this run."""
    try:
        with allow_schema_writes():
            await database.execute(
                """CREATE TABLE IF NOT EXISTS schema_migrations (
                  version VARCHAR(16) PRIMARY KEY,
                  applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                ) ENGINE=InnoDB"""
            )
            rows = await database.fetch_all("SELECT version FROM schema_migrations")
            applied = {row["version"] for row in rows}
            pending = pending_migrations(discover_migrations(dir_path), applied)

            applied_now: list[str] = []
            for version, path in pending:
                sql = Path(path).read_text(encoding="utf-8")
                for statement in split_statements(sql):
                    await database.execute(statement)
                await database.execute(
                    "INSERT INTO schema_migrations (version) VALUES (:version)",
                    {"version": version},
                )
                applied_now.append(version)
                structured_log("info", "migration_applied", version=version)
        if _production_mode():
            await verify_production_schema()
        return applied_now
    except Exception as exc:
        structured_log("error", "migration_run_failed", mode=settings.deployment_mode, exception=exc)
        _handle_migration_error(exc)
        raise


async def verify_production_schema() -> None:
    """Fail production startup if the DB is missing schema covered by migrations."""
    missing: list[str] = []
    table_rows = await database.fetch_all(
        """SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE()"""
    )
    existing_tables = {row["TABLE_NAME"] for row in table_rows}
    for table in sorted(PRODUCTION_REQUIRED_TABLES - existing_tables):
        missing.append(f"table:{table}")

    for table, columns in PRODUCTION_REQUIRED_COLUMNS.items():
        if table not in existing_tables:
            continue
        column_rows = await database.fetch_all(
            """SELECT COLUMN_NAME FROM information_schema.COLUMNS
               WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :table""",
            {"table": table},
        )
        existing_columns = {row["COLUMN_NAME"] for row in column_rows}
        for column in sorted(columns - existing_columns):
            missing.append(f"column:{table}.{column}")

    trigger_rows = await database.fetch_all(
        """SELECT TRIGGER_NAME FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA = DATABASE()"""
    )
    existing_triggers = {row["TRIGGER_NAME"] for row in trigger_rows}
    for trigger in sorted(PRODUCTION_REQUIRED_TRIGGERS - existing_triggers):
        missing.append(f"trigger:{trigger}")

    if missing:
        raise RuntimeError("Production schema drift detected: " + ", ".join(missing))
    structured_log("info", "production_schema_verified", tables=len(PRODUCTION_REQUIRED_TABLES))
