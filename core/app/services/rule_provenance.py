"""Immutable rule-source snapshots and operator completeness attestations."""

from __future__ import annotations

from typing import Any, Mapping

from app.action_plan import compute_rule_hash
from app.db import database


def row_value(row, key: str, default=None):
    if row is None:
        return default
    if hasattr(row, "get"):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, TypeError):
        return default


def snapshot_source(lottery: Mapping[str, Any], *, complete: bool) -> tuple[str, str, str]:
    source_kind = str(row_value(lottery, "source_type") or "manual").strip() or "manual"
    locator = str(
        row_value(lottery, "source_id")
        or row_value(lottery, "canonical_url")
        or row_value(lottery, "raw_url")
        or f"lottery:{row_value(lottery, 'id')}"
    ).strip()
    fetch_method = "operator_complete_attestation" if complete else "operator_draft_capture"
    return source_kind[:32], locator[:1024], fetch_method


async def ensure_rule_snapshot(
    lottery: Mapping[str, Any],
    rule_text: str,
    *,
    complete: bool,
    actor_id: str | None = None,
    source_kind: str | None = None,
    source_locator: str | None = None,
    fetch_method: str | None = None,
    allow_existing_complete: bool = True,
    db=database,
) -> dict[str, Any]:
    """Return an exact immutable snapshot, inserting one when necessary.

    A complete snapshot is never created by upgrading an incomplete discovery
    row.  The attestation is a distinct provenance fact, even when the source
    bytes (and therefore rule hash) are identical.
    """

    lottery_id = int(row_value(lottery, "id"))
    platform = str(row_value(lottery, "platform") or "").strip().lower()
    if lottery_id <= 0 or not platform:
        raise ValueError("invalid_lottery_snapshot_binding")
    rule_hash = compute_rule_hash(rule_text)

    # An existing complete attestation remains authoritative even when a
    # legacy client does not repeat the confirmation flag on every edit.
    if allow_existing_complete:
        complete_row = await db.fetch_one(
            """SELECT id, rule_hash, is_complete, attested_by, attested_at
               FROM lottery_rule_snapshots
               WHERE lottery_id = :lottery_id
                 AND rule_hash = :rule_hash
                 AND BINARY rule_text = BINARY :rule_text
                 AND is_complete = 1
                 AND attested_by IS NOT NULL
                 AND attested_at IS NOT NULL
               ORDER BY id DESC
               LIMIT 1""",
            {"lottery_id": lottery_id, "rule_hash": rule_hash, "rule_text": rule_text},
        )
        if complete_row:
            return {
                "id": int(row_value(complete_row, "id")),
                "rule_hash": rule_hash,
                "is_complete": True,
            }

    desired_complete = bool(complete)
    if not desired_complete:
        draft_row = await db.fetch_one(
            """SELECT id, rule_hash, is_complete
               FROM lottery_rule_snapshots
               WHERE lottery_id = :lottery_id
                 AND rule_hash = :rule_hash
                 AND BINARY rule_text = BINARY :rule_text
                 AND is_complete = 0
               ORDER BY id DESC
               LIMIT 1""",
            {"lottery_id": lottery_id, "rule_hash": rule_hash, "rule_text": rule_text},
        )
        if draft_row:
            return {
                "id": int(row_value(draft_row, "id")),
                "rule_hash": rule_hash,
                "is_complete": False,
            }

    default_kind, default_locator, default_method = snapshot_source(
        lottery, complete=desired_complete
    )
    values = {
        "lottery_id": lottery_id,
        "platform": platform,
        "source_kind": str(source_kind or default_kind).strip()[:32],
        "source_locator": str(source_locator or default_locator).strip()[:1024],
        "fetch_method": str(fetch_method or default_method).strip()[:64],
        "rule_text": rule_text,
        "rule_hash": rule_hash,
        "is_complete": int(desired_complete),
        "attested_by": str(actor_id or "").strip()[:128] or None,
    }
    if desired_complete and not values["attested_by"]:
        raise ValueError("complete_rule_snapshot_requires_actor")
    snapshot_id = await db.execute(
        """INSERT INTO lottery_rule_snapshots
              (lottery_id, platform, source_kind, source_locator, fetch_method,
               rule_text, rule_hash, is_complete, attested_by, attested_at)
           VALUES
              (:lottery_id, :platform, :source_kind, :source_locator, :fetch_method,
               :rule_text, :rule_hash, :is_complete, :attested_by,
               IF(:is_complete = 1, NOW(), NULL))""",
        values,
    )
    try:
        normalized_id = int(snapshot_id)
    except (TypeError, ValueError):
        inserted = await db.fetch_one(
            """SELECT id FROM lottery_rule_snapshots
               WHERE lottery_id = :lottery_id
                 AND rule_hash = :rule_hash
                 AND BINARY rule_text = BINARY :rule_text
                 AND is_complete = :is_complete
               ORDER BY id DESC LIMIT 1""",
            {
                "lottery_id": lottery_id,
                "rule_hash": rule_hash,
                "rule_text": rule_text,
                "is_complete": int(desired_complete),
            },
        )
        normalized_id = int(row_value(inserted, "id", 0) or 0)
    if normalized_id <= 0:
        raise RuntimeError("rule_snapshot_insert_returned_no_id")
    return {
        "id": normalized_id,
        "rule_hash": rule_hash,
        "is_complete": desired_complete,
    }
