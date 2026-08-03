"""SQL repository for the Xiaohongshu target review projection."""

from __future__ import annotations

import json
from typing import Any, Iterable

from app.db import database


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (dict, list)):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        value = bytes(value).decode("utf-8", errors="strict")
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return value


def _json_text(value: Any) -> str:
    return json.dumps(
        value if isinstance(value, (dict, list)) else {},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    )


def serialize_source(row: Any) -> dict[str, Any]:
    return dict(row)


def serialize_hit(row: Any) -> dict[str, Any]:
    item = dict(row)
    item["evidence"] = _json_value(item.get("evidence"))
    return item


def serialize_candidate(
    row: Any,
    *,
    source_hits: Iterable[Any] = (),
) -> dict[str, Any]:
    item = dict(row)
    for field in ("evidence", "rule", "classification"):
        item[field] = _json_value(item.get(field))
    item["source_hits"] = [serialize_hit(hit) for hit in source_hits]
    return item


async def list_sources(
    *,
    source_type: str | None = None,
    active: bool | None = None,
    limit: int = 100,
    offset: int = 0,
    db=database,
) -> dict[str, Any]:
    filters: list[str] = []
    filter_values: dict[str, Any] = {}
    if source_type is not None:
        filters.append("source_type = :source_type")
        filter_values["source_type"] = source_type
    if active is not None:
        filters.append("active = :active")
        filter_values["active"] = int(bool(active))
    where = f" WHERE {' AND '.join(filters)}" if filters else ""
    count_row = await db.fetch_one(
        f"SELECT COUNT(*) AS total FROM xiaohongshu_target_sources{where}",
        filter_values,
    )
    page_values = {
        **filter_values,
        "limit": int(limit),
        "offset": int(offset),
    }
    rows = await db.fetch_all(
        f"""SELECT *
            FROM xiaohongshu_target_sources
            {where}
            ORDER BY updated_at DESC, id DESC
            LIMIT :limit OFFSET :offset""",
        page_values,
    )
    return {
        "items": [serialize_source(row) for row in rows],
        "total": int(count_row["total"] or 0) if count_row else 0,
        "limit": int(limit),
        "offset": int(offset),
    }


async def get_source(source_id: int, *, for_update: bool = False, db=database):
    suffix = " FOR UPDATE" if for_update else ""
    return await db.fetch_one(
        f"""SELECT *
            FROM xiaohongshu_target_sources
            WHERE id = :source_id{suffix}""",
        {"source_id": int(source_id)},
    )


async def get_source_by_identity(
    source_type: str,
    source_value: str,
    *,
    for_update: bool = False,
    db=database,
):
    suffix = " FOR UPDATE" if for_update else ""
    return await db.fetch_one(
        f"""SELECT *
            FROM xiaohongshu_target_sources
            WHERE source_type = :source_type
              AND source_value = :source_value{suffix}""",
        {
            "source_type": source_type,
            "source_value": source_value,
        },
    )


async def source_scan_is_stale(
    source_id: int,
    *,
    stale_after_seconds: int,
    db=database,
) -> bool:
    row = await db.fetch_one(
        """SELECT (
                    status = 'scanning'
                    AND TIMESTAMPDIFF(SECOND, updated_at, NOW(6))
                        >= :stale_after_seconds
                  ) AS is_stale
           FROM xiaohongshu_target_sources
           WHERE id = :source_id""",
        {
            "source_id": int(source_id),
            "stale_after_seconds": int(stale_after_seconds),
        },
    )
    return bool(row and int(row["is_stale"] or 0) == 1)


async def create_or_get_source(
    source_type: str,
    source_value: str,
    *,
    db=database,
) -> tuple[dict[str, Any], bool]:
    await db.execute(
        """INSERT INTO xiaohongshu_target_sources
              (source_type, source_value)
           VALUES (:source_type, :source_value)
           ON DUPLICATE KEY UPDATE id = LAST_INSERT_ID(id)""",
        {
            "source_type": source_type,
            "source_value": source_value,
        },
    )
    affected = await db.fetch_one("SELECT ROW_COUNT() AS affected")
    row = await get_source_by_identity(
        source_type,
        source_value,
        db=db,
    )
    if not row:
        raise RuntimeError("xiaohongshu_target_source_insert_not_visible")
    return serialize_source(row), bool(
        affected and int(affected["affected"] or 0) == 1
    )


async def update_source_active(
    source_id: int,
    *,
    expected_version: int,
    active: bool,
    db=database,
) -> bool:
    await db.execute(
        """UPDATE xiaohongshu_target_sources
           SET active = :active,
               status = IF(:active = 1, status, 'idle'),
               last_error_code = IF(:active = 1, last_error_code, NULL),
               version = version + 1
           WHERE id = :source_id
             AND version = :expected_version""",
        {
            "source_id": int(source_id),
            "expected_version": int(expected_version),
            "active": int(bool(active)),
        },
    )
    affected = await db.fetch_one("SELECT ROW_COUNT() AS affected")
    return bool(affected and int(affected["affected"] or 0) == 1)


async def update_source_scan_state(
    source_id: int,
    *,
    expected_version: int,
    status: str,
    error_code: str | None,
    completed: bool,
    db=database,
) -> bool:
    await db.execute(
        """UPDATE xiaohongshu_target_sources
           SET status = :status,
               last_error_code = :error_code,
               last_scan_at = IF(:completed = 1, NOW(6), last_scan_at),
               version = version + 1
           WHERE id = :source_id
             AND version = :expected_version""",
        {
            "source_id": int(source_id),
            "expected_version": int(expected_version),
            "status": status,
            "error_code": error_code,
            "completed": int(bool(completed)),
        },
    )
    affected = await db.fetch_one("SELECT ROW_COUNT() AS affected")
    return bool(affected and int(affected["affected"] or 0) == 1)


async def get_tracked_source(tracked_source_id: int, *, db=database):
    return await db.fetch_one(
        """SELECT id, platform, source_type, source_value
           FROM tracked_sources
           WHERE id = :tracked_source_id""",
        {"tracked_source_id": int(tracked_source_id)},
    )


def _candidate_filter(
    *,
    decision_status: str | None,
    source_type: str | None,
) -> tuple[str, dict[str, Any]]:
    filters: list[str] = []
    values: dict[str, Any] = {}
    if decision_status is not None:
        filters.append("candidate.decision_status = :decision_status")
        values["decision_status"] = decision_status
    if source_type is not None:
        filters.append(
            """EXISTS (
                 SELECT 1
                 FROM xiaohongshu_target_candidate_source_hits hit_filter
                 WHERE hit_filter.candidate_id = candidate.id
                   AND hit_filter.source_type = :source_type
               )"""
        )
        values["source_type"] = source_type
    where = f" WHERE {' AND '.join(filters)}" if filters else ""
    return where, values


async def _source_hits_for_candidates(
    candidate_ids: Iterable[int],
    *,
    db=database,
) -> dict[int, list[Any]]:
    normalized = sorted({int(value) for value in candidate_ids if int(value) > 0})
    if not normalized:
        return {}
    placeholders = []
    values: dict[str, Any] = {}
    for index, candidate_id in enumerate(normalized):
        key = f"candidate_id_{index}"
        placeholders.append(f":{key}")
        values[key] = candidate_id
    rows = await db.fetch_all(
        f"""SELECT *
            FROM xiaohongshu_target_candidate_source_hits
            WHERE candidate_id IN ({', '.join(placeholders)})
            ORDER BY last_seen_at DESC, id DESC""",
        values,
    )
    grouped: dict[int, list[Any]] = {candidate_id: [] for candidate_id in normalized}
    for row in rows:
        grouped.setdefault(int(row["candidate_id"]), []).append(row)
    return grouped


async def list_candidates(
    *,
    decision_status: str | None = None,
    source_type: str | None = None,
    limit: int = 50,
    offset: int = 0,
    db=database,
) -> dict[str, Any]:
    where, values = _candidate_filter(
        decision_status=decision_status,
        source_type=source_type,
    )
    count_row = await db.fetch_one(
        f"""SELECT COUNT(*) AS total
            FROM xiaohongshu_target_candidates candidate{where}""",
        values,
    )
    page_values = {
        **values,
        "limit": int(limit),
        "offset": int(offset),
    }
    rows = await db.fetch_all(
        f"""SELECT candidate.*
            FROM xiaohongshu_target_candidates candidate
            {where}
            ORDER BY candidate.last_seen_at DESC, candidate.id DESC
            LIMIT :limit OFFSET :offset""",
        page_values,
    )
    hits = await _source_hits_for_candidates(
        (int(row["id"]) for row in rows),
        db=db,
    )
    return {
        "items": [
            serialize_candidate(
                row,
                source_hits=hits.get(int(row["id"]), ()),
            )
            for row in rows
        ],
        "total": int(count_row["total"] or 0) if count_row else 0,
        "limit": int(limit),
        "offset": int(offset),
    }


async def get_candidate(
    candidate_id: int,
    *,
    for_update: bool = False,
    db=database,
):
    suffix = " FOR UPDATE" if for_update else ""
    return await db.fetch_one(
        f"""SELECT *
            FROM xiaohongshu_target_candidates
            WHERE id = :candidate_id{suffix}""",
        {"candidate_id": int(candidate_id)},
    )


async def get_candidate_snapshot(candidate_id: int, *, db=database):
    row = await get_candidate(candidate_id, db=db)
    if not row:
        return None
    hits = await _source_hits_for_candidates((candidate_id,), db=db)
    return serialize_candidate(
        row,
        source_hits=hits.get(int(candidate_id), ()),
    )


async def upsert_candidate_observation(
    *,
    source: dict[str, Any],
    tracked_source_id: int | None,
    raw_url: str,
    canonical_url: str,
    title: str | None,
    evidence: dict[str, Any],
    rule: dict[str, Any],
    classification: dict[str, Any],
    initial_decision: str,
    published_at: Any,
    value_score: int,
    expires_at: Any,
    db=database,
) -> tuple[dict[str, Any], bool]:
    values = {
        "raw_url": raw_url,
        "canonical_url": canonical_url,
        "title": title,
        "evidence": _json_text(evidence),
        "rule": _json_text(rule),
        "classification": _json_text(classification),
        "initial_decision": initial_decision,
        "published_at": published_at,
        "value_score": int(value_score),
        "expires_at": expires_at,
    }
    await db.execute(
        """INSERT INTO xiaohongshu_target_candidates
              (platform, raw_url, canonical_url, title, evidence, rule,
               classification, published_at, value_score, expires_at,
               decision_status)
           VALUES
              ('xiaohongshu', :raw_url, :canonical_url, :title, :evidence,
               :rule, :classification, :published_at, :value_score,
               :expires_at, :initial_decision)
           ON DUPLICATE KEY UPDATE
              id = LAST_INSERT_ID(id),
              raw_url = :raw_url,
              title = COALESCE(:title, title),
              evidence = :evidence,
              rule = :rule,
              classification = :classification,
              published_at = COALESCE(:published_at, published_at),
              value_score = GREATEST(value_score, :value_score),
              expires_at = COALESCE(:expires_at, expires_at),
              decision_status = IF(
                decision_status = 'pending'
                AND :initial_decision = 'needs_review',
                'needs_review',
                decision_status
              ),
              last_seen_at = NOW(6),
              version = version + 1""",
        values,
    )
    affected = await db.fetch_one("SELECT ROW_COUNT() AS affected")
    row = await db.fetch_one(
        """SELECT *
           FROM xiaohongshu_target_candidates
           WHERE url_hash = SHA2(:canonical_url, 256)
             AND BINARY canonical_url = BINARY :canonical_url
           FOR UPDATE""",
        {"canonical_url": canonical_url},
    )
    if not row:
        raise RuntimeError("xiaohongshu_target_candidate_hash_collision")
    candidate_id = int(row["id"])
    await db.execute(
        """INSERT INTO xiaohongshu_target_candidate_source_hits
              (candidate_id, source_id, tracked_source_id, source_type,
               source_value, evidence)
           VALUES
              (:candidate_id, :source_id, :tracked_source_id, :source_type,
               :source_value, :evidence)
           ON DUPLICATE KEY UPDATE
              tracked_source_id = COALESCE(
                :tracked_source_id,
                tracked_source_id
              ),
              source_type = :source_type,
              source_value = :source_value,
              evidence = :evidence,
              hit_count = hit_count + 1,
              last_seen_at = NOW(6)""",
        {
            "candidate_id": candidate_id,
            "source_id": int(source["id"]),
            "tracked_source_id": tracked_source_id,
            "source_type": source["source_type"],
            "source_value": source["source_value"],
            "evidence": _json_text(evidence),
        },
    )
    snapshot = await get_candidate_snapshot(candidate_id, db=db)
    if snapshot is None:
        raise RuntimeError("xiaohongshu_target_candidate_insert_not_visible")
    created = bool(affected and int(affected["affected"] or 0) == 1)
    return snapshot, created


async def latest_candidate_source_hit(candidate_id: int, *, db=database):
    return await db.fetch_one(
        """SELECT *
           FROM xiaohongshu_target_candidate_source_hits
           WHERE candidate_id = :candidate_id
           ORDER BY last_seen_at DESC, id DESC
           LIMIT 1""",
        {"candidate_id": int(candidate_id)},
    )


async def create_or_get_lottery_for_candidate(
    candidate: Any,
    *,
    source_hit: Any,
    rule_text: str,
    action_plan: dict[str, Any],
    db=database,
) -> dict[str, Any]:
    source_id = int(source_hit["source_id"]) if source_hit else 0
    await db.execute(
        """INSERT INTO lotteries
              (platform, source_type, source_id, raw_url, canonical_url, title,
               rule_text, action_plan, published_at, value_score, expires_at,
               status)
           VALUES
              ('xiaohongshu', :source_type, :source_id, :raw_url,
               :canonical_url, :title, :rule_text, :action_plan,
               :published_at, :value_score, :expires_at, 'pending')
           ON DUPLICATE KEY UPDATE id = LAST_INSERT_ID(id)""",
        {
            "source_type": (
                str(source_hit["source_type"])
                if source_hit
                else "offline_search_result"
            ),
            "source_id": f"xhs-target-source:{source_id}"[:64],
            "raw_url": str(candidate["raw_url"]),
            "canonical_url": str(candidate["canonical_url"]),
            "title": candidate["title"],
            "rule_text": rule_text,
            "action_plan": _json_text(action_plan),
            "published_at": candidate["published_at"],
            "value_score": int(candidate["value_score"] or 0),
            "expires_at": candidate["expires_at"],
        },
    )
    lottery = await db.fetch_one(
        """SELECT *
           FROM lotteries
           WHERE url_hash = SHA2(:canonical_url, 256)
             AND BINARY canonical_url = BINARY :canonical_url
           FOR UPDATE""",
        {"canonical_url": str(candidate["canonical_url"])},
    )
    if not lottery:
        raise RuntimeError("xiaohongshu_target_lottery_hash_collision")
    return dict(lottery)


async def persist_candidate_decision(
    candidate_id: int,
    *,
    expected_version: int,
    decision_status: str,
    decision_reason: str | None,
    accepted_lottery_id: int | None,
    actor_id: str,
    db=database,
) -> bool:
    await db.execute(
        """UPDATE xiaohongshu_target_candidates
           SET decision_status = :decision_status,
               decision_reason = :decision_reason,
               accepted_lottery_id = :accepted_lottery_id,
               decided_at = IF(
                 :decision_status = 'pending',
                 NULL,
                 NOW(6)
               ),
               decision_actor_id = IF(
                 :decision_status = 'pending',
                 NULL,
                 :actor_id
               ),
               version = version + 1
           WHERE id = :candidate_id
             AND version = :expected_version""",
        {
            "candidate_id": int(candidate_id),
            "expected_version": int(expected_version),
            "decision_status": decision_status,
            "decision_reason": decision_reason,
            "accepted_lottery_id": accepted_lottery_id,
            "actor_id": actor_id,
        },
    )
    affected = await db.fetch_one("SELECT ROW_COUNT() AS affected")
    return bool(affected and int(affected["affected"] or 0) == 1)
