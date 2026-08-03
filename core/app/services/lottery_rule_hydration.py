"""Read-only source hydration for lottery rule authoring.

Hydration is deliberately separate from Action Plan attestation: provider and
candidate evidence can pre-fill the editor, but only the existing operator
save flow can mark a rule snapshot complete.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Mapping

from app.db import database
from app.services.bilibili_preflight_evidence import (
    BilibiliPreflightEvidenceError,
    extract_bilibili_dynamic_id,
)
from app.utils.lottery_targets import validate_lottery_identity


BILIBILI_OPUS_DETAIL_URL = (
    "https://api.bilibili.com/x/polymer/web-dynamic/v1/opus/detail"
)
BILIBILI_RULE_HYDRATION_TIMEOUT_SECONDS = 10.0


class LotteryRuleHydrationError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool = False) -> None:
        self.code = str(code)
        self.retryable = bool(retryable)
        super().__init__(self.code)


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        value = bytes(value).decode("utf-8", errors="strict")
    if isinstance(value, str) and value.strip():
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        if isinstance(decoded, Mapping):
            return dict(decoded)
    return {}


def empty_target_identity() -> dict[str, Any]:
    return {
        "uid": None,
        "display_name": None,
        "profile_url": None,
        "verified": False,
        "source": None,
    }


def _target_identity(
    *,
    uid: Any,
    display_name: Any,
    profile_url: Any,
    verified: bool,
    source: str,
) -> dict[str, Any]:
    normalized_uid = str(uid or "").strip() or None
    normalized_name = str(display_name or "").strip() or None
    normalized_profile = str(profile_url or "").strip() or None
    return {
        "uid": normalized_uid,
        "display_name": normalized_name,
        "profile_url": normalized_profile,
        "verified": bool(
            verified
            and normalized_uid
            and normalized_name
            and normalized_profile
        ),
        "source": str(source or "").strip() or None,
    }


def target_identity_from_lottery(lottery: Mapping[str, Any]) -> dict[str, Any]:
    """Project already-persisted candidate identity without remote I/O."""

    plan = _json_object(lottery.get("action_plan"))
    pursuit = _json_object(plan.get("target_pursuit_review_snapshot"))
    author = _json_object(pursuit.get("author"))
    if author:
        return _target_identity(
            uid=author.get("stable_id") or author.get("uid"),
            display_name=author.get("display_name"),
            profile_url=author.get("profile_url"),
            verified=author.get("verified") is True,
            source="xiaohongshu_target_candidate",
        )
    return empty_target_identity()


def _snapshot(
    name: str,
    text: Any,
    *,
    trusted: bool,
    observed_at: Any,
    source: str,
) -> dict[str, Any]:
    exact_text = str(text or "")
    return {
        "text": exact_text,
        "present": bool(exact_text.strip()),
        "trusted": bool(trusted and exact_text.strip()),
        "observed_at": str(observed_at or "").strip() or None,
        "source": str(source or name),
        "sha256": hashlib.sha256(exact_text.encode("utf-8")).hexdigest(),
    }


def _normalized_snapshot(name: str, value: Any) -> dict[str, Any]:
    snapshot = _json_object(value)
    return _snapshot(
        name,
        snapshot.get("text"),
        trusted=snapshot.get("trusted") is True,
        observed_at=snapshot.get("observed_at"),
        source=str(snapshot.get("source") or name),
    )


def combine_trusted_rule_text(rule_snapshot: Mapping[str, Any]) -> str:
    """Combine exact trusted bytes in source order, de-duplicating repeats."""

    chunks: list[str] = []
    for name in ("body", "expanded_body", "pinned_comment"):
        snapshot = _json_object(rule_snapshot.get(name))
        if snapshot.get("trusted") is not True:
            continue
        text = str(snapshot.get("text") or "").strip()
        if text and text not in chunks:
            chunks.append(text)
    return "\n\n".join(chunks)


def _stored_rule_hydration(lottery: Mapping[str, Any]) -> dict[str, Any]:
    observed_at = lottery.get("extracted_at")
    body = _snapshot(
        "body",
        lottery.get("rule_text"),
        trusted=False,
        observed_at=observed_at,
        source="stored_lottery_rule",
    )
    rule_snapshot = {
        "body": body,
        "expanded_body": _snapshot(
            "expanded_body",
            "",
            trusted=False,
            observed_at=observed_at,
            source="not_observed",
        ),
        "pinned_comment": _snapshot(
            "pinned_comment",
            "",
            trusted=False,
            observed_at=observed_at,
            source="not_observed",
        ),
    }
    return {
        "rule_text": str(lottery.get("rule_text") or ""),
        "rule_snapshot": rule_snapshot,
        "target_identity": target_identity_from_lottery(lottery),
        "source": "stored_lottery_rule",
        "fetched_at": None,
        "warnings": ["lottery_rule_hydration_source_evidence_unavailable"],
    }


async def _hydrate_bilibili(lottery: Mapping[str, Any]) -> dict[str, Any]:
    import httpx

    from app.services.bilibili_discovery import parse_bilibili_opus_detail
    from app.services.discovery import load_bilibili_discovery_cookie_header

    target = validate_lottery_identity(
        str(lottery.get("platform") or ""),
        str(lottery.get("raw_url") or ""),
        str(lottery.get("canonical_url") or ""),
    )
    if not target.valid or target.kind != "dynamic":
        raise LotteryRuleHydrationError(
            "bilibili_rule_hydration_dynamic_target_required"
        )
    try:
        dynamic_id = extract_bilibili_dynamic_id(
            str(lottery.get("canonical_url") or ""),
            str(lottery.get("raw_url") or ""),
        )
    except BilibiliPreflightEvidenceError as exc:
        raise LotteryRuleHydrationError(exc.code) from exc
    try:
        cookie_header = await load_bilibili_discovery_cookie_header()
    except Exception as exc:
        raise LotteryRuleHydrationError(
            "bilibili_rule_hydration_ready_account_required"
        ) from exc

    headers = {
        "Accept": "application/json, text/plain, */*",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/137.0.0.0 Safari/537.36"
        ),
        "Referer": f"https://t.bilibili.com/{dynamic_id}",
        "Origin": "https://www.bilibili.com",
        "Cookie": cookie_header,
    }
    timeout = httpx.Timeout(
        BILIBILI_RULE_HYDRATION_TIMEOUT_SECONDS,
        connect=5.0,
    )
    try:
        async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
            response = await client.get(
                BILIBILI_OPUS_DETAIL_URL,
                params={"id": dynamic_id},
            )
            response.raise_for_status()
            payload = response.json()
    except httpx.TimeoutException as exc:
        raise LotteryRuleHydrationError(
            "bilibili_rule_hydration_timeout",
            retryable=True,
        ) from exc
    except (httpx.RequestError, httpx.HTTPStatusError, ValueError) as exc:
        raise LotteryRuleHydrationError(
            "bilibili_rule_hydration_unavailable",
            retryable=True,
        ) from exc
    if payload.get("code") != 0:
        raise LotteryRuleHydrationError(
            "bilibili_rule_hydration_api_rejected",
            retryable=True,
        )
    item = _json_object(_json_object(payload.get("data")).get("item"))
    observed_dynamic_id = str(item.get("id_str") or "").strip()
    if observed_dynamic_id != dynamic_id:
        raise LotteryRuleHydrationError(
            "bilibili_rule_hydration_target_mismatch"
        )
    # Opus detail's `modules` is an ordered list rather than the feed API's
    # mapping, so parse the original item with the exact node extractor.
    opus = parse_bilibili_opus_detail(item)
    author = _json_object(opus.get("author"))
    expanded_text = str(opus.get("full_text") or "").strip()
    if not expanded_text:
        raise LotteryRuleHydrationError(
            "bilibili_rule_hydration_content_missing"
        )
    try:
        uid = int(author.get("mid"))
    except (TypeError, ValueError):
        uid = 0
    display_name = str(author.get("name") or "").strip()
    identity_verified = bool(uid > 0 and display_name)
    observed_at = datetime.now(timezone.utc).isoformat()
    rule_snapshot = {
        "body": _snapshot(
            "body",
            expanded_text,
            trusted=True,
            observed_at=observed_at,
            source="bilibili_opus_detail",
        ),
        "expanded_body": _snapshot(
            "expanded_body",
            "",
            trusted=False,
            observed_at=observed_at,
            source="not_separate_from_opus_body",
        ),
        "pinned_comment": _snapshot(
            "pinned_comment",
            "",
            trusted=False,
            observed_at=observed_at,
            source="not_observed",
        ),
    }
    return {
        "rule_text": combine_trusted_rule_text(rule_snapshot),
        "rule_snapshot": rule_snapshot,
        "target_identity": _target_identity(
            uid=uid if uid > 0 else None,
            display_name=display_name,
            profile_url=(f"https://space.bilibili.com/{uid}" if uid > 0 else None),
            verified=identity_verified,
            source="bilibili_opus_detail",
        ),
        "source": "bilibili_opus_detail_api",
        "fetched_at": observed_at,
        "warnings": ["bilibili_pinned_comment_not_observed"],
    }


async def _hydrate_xiaohongshu(lottery: Mapping[str, Any]) -> dict[str, Any]:
    row = await database.fetch_one(
        """SELECT classification
           FROM xiaohongshu_target_candidates
           WHERE accepted_lottery_id = :lottery_id
              OR BINARY canonical_url = BINARY :canonical_url
           ORDER BY (accepted_lottery_id = :lottery_id) DESC,
                    last_seen_at DESC,
                    id DESC
           LIMIT 1""",
        {
            "lottery_id": int(lottery.get("id") or 0),
            "canonical_url": str(lottery.get("canonical_url") or ""),
        },
    )
    if not row:
        return _stored_rule_hydration(lottery)
    classification = _json_object(row["classification"])
    snapshots = _json_object(classification.get("content_snapshots"))
    rule_snapshot = {
        name: _normalized_snapshot(name, snapshots.get(name))
        for name in ("body", "expanded_body", "pinned_comment")
    }
    author = _json_object(classification.get("author"))
    rule_text = combine_trusted_rule_text(rule_snapshot)
    return {
        "rule_text": rule_text or str(lottery.get("rule_text") or ""),
        "rule_snapshot": rule_snapshot,
        "target_identity": _target_identity(
            uid=author.get("stable_id"),
            display_name=author.get("display_name"),
            profile_url=author.get("profile_url"),
            verified=author.get("verified") is True,
            source="xiaohongshu_target_candidate",
        ),
        "source": "xiaohongshu_target_candidate",
        "fetched_at": str(classification.get("observed_at") or "").strip() or None,
        "warnings": list(classification.get("review_reason_codes") or []),
    }


async def hydrate_lottery_rule(lottery: Mapping[str, Any]) -> dict[str, Any]:
    platform = str(lottery.get("platform") or "").strip().casefold()
    if platform == "bilibili":
        hydration = await _hydrate_bilibili(lottery)
    elif platform == "xiaohongshu":
        hydration = await _hydrate_xiaohongshu(lottery)
    else:
        hydration = _stored_rule_hydration(lottery)
    target = validate_lottery_identity(
        platform,
        str(lottery.get("raw_url") or ""),
        str(lottery.get("canonical_url") or ""),
    )
    return {
        "lottery_id": int(lottery.get("id") or 0),
        "platform": platform,
        "canonical_url": str(lottery.get("canonical_url") or ""),
        "target_kind": target.kind,
        **hydration,
    }


__all__ = (
    "LotteryRuleHydrationError",
    "combine_trusted_rule_text",
    "empty_target_identity",
    "hydrate_lottery_rule",
    "target_identity_from_lottery",
)
