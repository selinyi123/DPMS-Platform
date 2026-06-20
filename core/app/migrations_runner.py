"""Versioned SQL migration runner (Phase 4 / ops baseline).

Replaces ad-hoc, untracked schema evolution with an ordered, recorded sequence.
Migration files live in ``core/migrations/NNNN_name.sql`` and are applied once
each, in version order, with every applied version recorded in
``schema_migrations``. The existing idempotent ``ensure_*`` startup hooks remain
as a safety net; new schema changes should be added as migration files going
forward.

The ordering / pending / statement-splitting logic is pure so it can be
unit-tested without a database; ``run_migrations`` is the only DB-touching part.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.config import settings
from app.db import database
from app.utils.log import structured_log

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
VERSION_RE = re.compile(r"^(\d{4})_.+\.sql$")


class FatalMigrationError(BaseException):
    """Bypass broad ``except Exception`` startup handlers in production.

    ``core.app.main`` intentionally logs migration failures in development, but
    production must not continue with a half-applied schema. This BaseException
    subclass is not caught by the existing non-fatal startup handler.
    """


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
    for chunk in sql.split(";"):
        lines = [line for line in chunk.splitlines() if not line.strip().startswith("--")]
        cleaned = "\n".join(lines).strip()
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
        return applied_now
    except Exception as exc:
        structured_log("error", "migration_run_failed", mode=settings.deployment_mode, exception=exc)
        _handle_migration_error(exc)
        raise
