import json
import hashlib
import re
import uuid
from pathlib import Path
from typing import Any

from app.db import database
from app.utils.log import structured_log


TERMINAL_OUTBOX_EVENT_TYPES = {"TaskFinished", "TaskFailed", "AccountExecutionFinished"}
MIGRATION_FILENAME_RE = re.compile(r"^(\d{4})_.+\.sql$")
MIGRATION_DIR_CANDIDATES = (
    # Runtime image: worker/Dockerfile copies the release manifest here.
    Path(__file__).resolve().parents[2] / "migrations",
    # Source checkout: keep unit tests and local execution bound to Core's manifest.
    Path(__file__).resolve().parents[3] / "core" / "migrations",
)


def expected_migration_checksums(
    migrations_dir: Path | None = None,
) -> dict[str, str]:
    """Return the exact migration ledger shipped with this Worker release."""

    if migrations_dir is None:
        migrations_dir = next(
            (candidate for candidate in MIGRATION_DIR_CANDIDATES if candidate.is_dir()),
            None,
        )
    if migrations_dir is None or not migrations_dir.is_dir():
        raise RuntimeError("worker_schema_migration_manifest_missing")

    expected: dict[str, str] = {}
    for path in sorted(migrations_dir.iterdir()):
        if not path.is_file():
            continue
        match = MIGRATION_FILENAME_RE.fullmatch(path.name)
        if match is None:
            continue
        version = match.group(1)
        if version in expected:
            raise RuntimeError("worker_schema_migration_manifest_duplicate_version")
        expected[version] = hashlib.sha256(path.read_bytes()).hexdigest()
    if not expected:
        raise RuntimeError("worker_schema_migration_manifest_empty")
    return expected


async def verify_event_schema() -> None:
    """Read-only Worker startup gate for the event-store projection."""

    row = await database.fetch_one(
        """SELECT COUNT(DISTINCT COLUMN_NAME) AS required_columns
           FROM information_schema.COLUMNS
           WHERE TABLE_SCHEMA = DATABASE()
             AND TABLE_NAME = 'events'
             AND COLUMN_NAME IN (
               'id', 'aggregate', 'aggregate_id', 'event_type', 'payload',
               'correlation_id', 'causation_id', 'actor_type', 'actor_id',
               'source_service', 'occurred_at'
             )"""
    )
    if row is None or int(row["required_columns"] or 0) != 11:
        raise RuntimeError("worker_event_schema_not_current")

    expected = expected_migration_checksums()
    migration_rows = await database.fetch_all(
        """SELECT version, checksum
           FROM schema_migrations
           ORDER BY version"""
    )
    applied = {
        str(row["version"]): str(row["checksum"] or "").lower()
        for row in migration_rows
    }
    if applied != expected:
        missing = sorted(expected.keys() - applied.keys())
        unexpected = sorted(applied.keys() - expected.keys())
        mismatched = sorted(
            version
            for version in expected.keys() & applied.keys()
            if expected[version] != applied[version]
        )
        structured_log(
            "error",
            "worker_schema_migration_ledger_mismatch",
            missing_versions=missing,
            unexpected_versions=unexpected,
            checksum_mismatch_versions=mismatched,
        )
        raise RuntimeError("worker_schema_migration_ledger_not_current")


async def record_event(
    *,
    aggregate: str,
    aggregate_id: Any,
    event_type: str,
    payload: dict[str, Any] | None = None,
    correlation_id: Any | None = None,
    causation_id: Any | None = None,
    actor_type: str = "system",
    actor_id: Any | None = None,
    source_service: str = "worker",
) -> str | None:
    if source_service == "worker" and event_type in TERMINAL_OUTBOX_EVENT_TYPES:
        structured_log(
            "info",
            "terminal_event_direct_write_skipped",
            aggregate=aggregate,
            aggregate_id=aggregate_id,
            event_type=event_type,
        )
        return None

    event_id = str(uuid.uuid4())
    try:
        await database.execute(
            """INSERT INTO events
               (id, aggregate, aggregate_id, event_type, payload, correlation_id, causation_id, actor_type, actor_id, source_service)
               VALUES (:id, :aggregate, :aggregate_id, :event_type, :payload, :correlation_id, :causation_id, :actor_type, :actor_id, :source_service)""",
            {
                "id": event_id,
                "aggregate": aggregate,
                "aggregate_id": str(aggregate_id),
                "event_type": event_type,
                "payload": json.dumps(payload or {}, ensure_ascii=False, default=str),
                "correlation_id": str(correlation_id) if correlation_id is not None else None,
                "causation_id": str(causation_id) if causation_id is not None else None,
                "actor_type": actor_type,
                "actor_id": str(actor_id) if actor_id is not None else None,
                "source_service": source_service,
            },
        )
        return event_id
    except Exception as exc:
        structured_log(
            "warning",
            "event_store_write_failed",
            aggregate=aggregate,
            aggregate_id=aggregate_id,
            event_type=event_type,
            exception=exc,
        )
        return None
