"""Pure evidence analysis for Xiaohongshu lottery candidates.

The module deliberately has no browser, HTTP, database, or platform-action
dependencies.  Callers must collect evidence elsewhere and pass a JSON-like
mapping here.  The returned hashes make each textual observation tamper
evident; they do not turn unverified evidence into trusted evidence.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse, urlunparse

from app.services.lottery_rules import parse_lottery_rule


ANALYSIS_VERSION = 1
MAX_ORIGINAL_TRACE_HOPS = 5
SOURCE_TYPES = frozenset(
    {"keyword", "author_profile", "offline_search_result"}
)
SOURCE_TYPE_ALIASES = {
    "search": "keyword",
    "direct_url": "offline_search_result",
}

_NOTE_ID_RE = re.compile(r"^[0-9a-fA-F]{24}$")
_PROFILE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{5,64}$")
_COLLECTION_PAGE_TYPES = frozenset(
    {"collection", "collection_item", "album", "合集"}
)
_INTERMEDIATE_PAGE_TYPES = _COLLECTION_PAGE_TYPES | frozenset(
    {"repost", "aggregate", "redirect"}
)
_FINAL_PAGE_TYPES = frozenset({"note", "original", "original_note"})
_SHANGHAI_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")

_DATE_TOKEN_RE = re.compile(
    r"(?:(?P<year>20\d{2})\s*(?:年|[./-])\s*)?"
    r"(?P<month>1[0-2]|0?[1-9])\s*(?:月|[./-])\s*"
    r"(?P<day>3[01]|[12]\d|0?[1-9])\s*日?"
)
_ACTIVITY_LABEL_RE = re.compile(
    r"(?P<label>活动时间|参与时间|报名时间|参与期|活动期间|"
    r"截止时间|截止日期|报名截止)"
    r"\s*[：:]\s*(?P<value>[^\r\n。；;]{1,120})",
    re.IGNORECASE,
)
_DRAW_LABEL_RE = re.compile(
    r"(?:开奖时间|公布时间)\s*[：:]\s*(?P<value>[^\r\n。；;]{1,80})",
    re.IGNORECASE,
)
_PRIZE_RE = re.compile(
    r"(?:活动奖品|活动奖励|抽奖奖品|奖品|奖项)"
    r"\s*[：:]\s*(?P<value>[^\r\n]{1,160})",
    re.IGNORECASE,
)
_NEXT_FIELD_RE = re.compile(
    r"\s+(?:中奖人数|获奖人数|活动时间|参与时间|开奖时间|"
    r"参与方式|抽奖平台|温馨提示)\s*[：:]",
    re.IGNORECASE,
)

_COMPLEX_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "media_or_story_submission",
        re.compile(
            r"(?:晒出?|上传|提交|附上|图评|发出?)"
            r"[^。\n]{0,48}(?:照片|图片|截图|视频|图文|故事)",
            re.IGNORECASE,
        ),
    ),
    (
        "separate_post",
        re.compile(
            r"(?:另行|自行|需要|必须|请)[^。\n]{0,20}"
            r"(?:发布|发一篇|发一条|投稿)[^。\n]{0,20}"
            r"(?:笔记|图文|视频|作品)",
            re.IGNORECASE,
        ),
    ),
    (
        "repost_or_share",
        re.compile(
            r"(?:(?:参与方式|需要|必须|请|同时)[^。\n]{0,24}"
            r"(?:转发|分享)|(?:转发|分享)(?:本篇|本条|这篇|该篇|笔记|活动))",
            re.IGNORECASE,
        ),
    ),
    (
        "purchase_or_visit",
        re.compile(
            r"(?:购买|下单|消费|到店|核销|付款|订单截图)",
            re.IGNORECASE,
        ),
    ),
    (
        "external_form",
        re.compile(
            r"(?:填写|提交)[^。\n]{0,20}(?:表单|问卷|链接|收集表)"
            r"|(?:外链|小程序|问卷)",
            re.IGNORECASE,
        ),
    ),
    (
        "private_message",
        re.compile(r"(?:私信|发送私聊|后台留言)", re.IGNORECASE),
    ),
    (
        "join_group",
        re.compile(r"(?:进群|加群|加入群聊)", re.IGNORECASE),
    ),
    (
        "friend_mention",
        re.compile(
            r"(?:@|艾特|提及|邀请)\s*"
            r"(?:\d{1,2}|[一二两三四五六七八九十]{1,3})\s*"
            r"(?:位|个)?\s*(?:好友|朋友)",
            re.IGNORECASE,
        ),
    ),
    (
        "custom_comment_content",
        re.compile(
            r"(?:评论|留言)(?:区)?[^。\n]{0,24}"
            r"(?:指定口令|指定文案|关键词|回答|写下|说出|分享你的|"
            r"告诉我们|聊聊)",
            re.IGNORECASE,
        ),
    ),
)

_UNSUPPORTED_COMPLEX_CODES = {
    "topic_tag": "topic_tag",
    "mention_account": "friend_or_account_mention",
    "media_submission": "media_or_story_submission",
    "translation_required": "translation_required",
    "comment_content": "custom_comment_content",
    "repost_content": "repost_or_share",
    "reposted": "repost_or_share",
    "separate_post": "separate_post",
    "multiple_prize_branches": "multiple_prize_branches",
}


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json_sha256(value: Any) -> str:
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return _sha256_text(serialized)


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _first_mapping(*values: Any) -> dict[str, Any]:
    for value in values:
        if isinstance(value, dict) and value:
            return value
    return {}


def _first_text(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _first_exact_text(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _normalize_observed_at(value: Any) -> tuple[datetime | None, str | None]:
    if value is None or value == "":
        return None, None
    parsed: datetime
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime(value.year, value.month, value.day)
    elif isinstance(value, str):
        token = value.strip()
        if token.endswith("Z"):
            token = f"{token[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(token)
        except ValueError:
            return None, None
    else:
        return None, None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_SHANGHAI_TZ)
    parsed = parsed.astimezone(_SHANGHAI_TZ)
    return parsed, parsed.isoformat()


def _safe_https_parts(raw_url: Any) -> tuple[Any, str] | None:
    target = str(raw_url or "").strip()
    if not target:
        return None
    parsed = urlparse(target)
    if (
        parsed.scheme.lower() != "https"
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    if port not in (None, 443):
        return None
    host = (parsed.hostname or "").rstrip(".").lower()
    return parsed, host


def _parse_note_url(raw_url: Any) -> dict[str, Any] | None:
    safe = _safe_https_parts(raw_url)
    if safe is None:
        return None
    parsed, host = safe
    parts = [part for part in parsed.path.split("/") if part]
    note_id = ""
    if host in {"xiaohongshu.com", "www.xiaohongshu.com"}:
        if (
            len(parts) == 2
            and parts[0] == "explore"
            and _NOTE_ID_RE.fullmatch(parts[1])
        ):
            note_id = parts[1].lower()
        elif (
            len(parts) == 3
            and parts[0] == "discovery"
            and parts[1] == "item"
            and _NOTE_ID_RE.fullmatch(parts[2])
        ):
            note_id = parts[2].lower()
    if not note_id:
        return None
    return {
        "kind": "note",
        "note_id": note_id,
        "url": f"https://www.xiaohongshu.com/explore/{note_id}",
    }


def _parse_short_link(raw_url: Any) -> dict[str, Any] | None:
    safe = _safe_https_parts(raw_url)
    if safe is None:
        return None
    parsed, host = safe
    parts = [part for part in parsed.path.split("/") if part]
    if host != "xhslink.com" or not parts:
        return None
    clean = urlunparse(("https", "xhslink.com", parsed.path, "", "", ""))
    return {"kind": "short_link", "url": clean}


def _parse_profile_url(raw_url: Any) -> dict[str, str] | None:
    safe = _safe_https_parts(raw_url)
    if safe is None:
        return None
    parsed, host = safe
    parts = [part for part in parsed.path.split("/") if part]
    if (
        host not in {"xiaohongshu.com", "www.xiaohongshu.com"}
        or len(parts) != 3
        or parts[0] != "user"
        or parts[1] != "profile"
        or not _PROFILE_ID_RE.fullmatch(parts[2])
    ):
        return None
    stable_id = parts[2]
    return {
        "stable_id": stable_id,
        "url": f"https://www.xiaohongshu.com/user/profile/{stable_id}",
    }


def _page_type(value: Any) -> str:
    return str(value or "note").strip().lower()


def _trace_author(entry: dict[str, Any]) -> dict[str, Any]:
    return _first_mapping(entry.get("author"), entry.get("original_author"))


def _resolve_original_trace(
    candidate_url: str,
    candidate_page_type: str,
    raw_trace: Any,
) -> dict[str, Any]:
    trace = raw_trace if isinstance(raw_trace, list) else []
    result: dict[str, Any] = {
        "entries": [],
        "complete": False,
        "original_note_url": candidate_url,
        "original_note_id": None,
        "original_author": {},
        "is_collection": candidate_page_type in _COLLECTION_PAGE_TYPES,
        "reason_codes": [],
        "review_reason_codes": [],
    }

    def reason(code: str, *, review: bool = True) -> None:
        if code not in result["reason_codes"]:
            result["reason_codes"].append(code)
        if review and code not in result["review_reason_codes"]:
            result["review_reason_codes"].append(code)

    candidate_info = _parse_note_url(candidate_url)
    if candidate_info is None:
        reason("candidate_note_url_invalid")
        return result
    result["original_note_id"] = candidate_info["note_id"]
    current = candidate_info["url"]

    if not trace:
        if result["is_collection"] or candidate_page_type in _INTERMEDIATE_PAGE_TYPES:
            reason("collection_original_trace_missing")
            return result
        result["complete"] = True
        return result

    if len(trace) > MAX_ORIGINAL_TRACE_HOPS:
        reason("original_trace_limit_exceeded")
        return result

    seen: set[str] = set()
    for index, raw_entry in enumerate(trace):
        if not isinstance(raw_entry, dict):
            reason("original_trace_invalid")
            return result
        entry_url = _first_text(
            raw_entry.get("url"),
            raw_entry.get("from_url"),
            current if index == 0 else "",
        )
        entry_info = _parse_note_url(entry_url)
        if entry_info is None:
            if _parse_short_link(entry_url):
                reason("original_trace_short_link_unresolved")
            else:
                reason("original_trace_url_invalid")
            return result
        canonical_entry_url = entry_info["url"]
        if canonical_entry_url != current:
            reason("original_trace_disconnected")
            return result
        if canonical_entry_url in seen:
            reason("original_trace_cycle")
            return result
        seen.add(canonical_entry_url)

        kind = _page_type(
            raw_entry.get("page_type")
            or raw_entry.get("kind")
            or raw_entry.get("type")
        )
        if kind in _COLLECTION_PAGE_TYPES:
            result["is_collection"] = True
        next_url = _first_text(
            raw_entry.get("next_url"),
            raw_entry.get("to_url"),
            raw_entry.get("original_url"),
        )
        normalized_entry = {
            "index": index,
            "url": canonical_entry_url,
            "note_id": entry_info["note_id"],
            "page_type": kind,
            "next_url": None,
        }
        author = _trace_author(raw_entry)
        if author:
            normalized_entry["author"] = dict(author)
        if next_url:
            next_info = _parse_note_url(next_url)
            if next_info is None:
                if _parse_short_link(next_url):
                    reason("original_trace_short_link_unresolved")
                else:
                    reason("original_trace_url_invalid")
                result["entries"].append(normalized_entry)
                return result
            normalized_entry["next_url"] = next_info["url"]
            result["entries"].append(normalized_entry)
            if next_info["url"] in seen:
                reason("original_trace_cycle")
                return result
            current = next_info["url"]
            continue

        result["entries"].append(normalized_entry)
        if kind in _INTERMEDIATE_PAGE_TYPES:
            reason("original_trace_incomplete")
            return result
        if kind not in _FINAL_PAGE_TYPES:
            reason("original_trace_final_type_unverified")
            return result
        result["complete"] = True
        result["original_note_url"] = canonical_entry_url
        result["original_note_id"] = entry_info["note_id"]
        result["original_author"] = dict(author)
        if result["is_collection"]:
            reason("collection_original_trace_resolved", review=False)
        return result

    reason("original_trace_incomplete")
    return result


def _author_identity(
    author_evidence: dict[str, Any],
    *,
    required_profile_url: str = "",
) -> dict[str, Any]:
    stable_id = _first_text(
        author_evidence.get("stable_id"),
        author_evidence.get("user_id"),
        author_evidence.get("id"),
    )
    profile_url = _first_text(
        author_evidence.get("profile_url"),
        author_evidence.get("url"),
    )
    parsed_profile = _parse_profile_url(profile_url)
    reason_codes: list[str] = []
    verified = True
    if not stable_id or not profile_url:
        verified = False
        reason_codes.append("author_identity_missing")
    elif parsed_profile is None:
        verified = False
        reason_codes.append("author_profile_url_invalid")
    elif parsed_profile["stable_id"] != stable_id:
        verified = False
        reason_codes.append("author_stable_id_profile_mismatch")

    required_profile = _parse_profile_url(required_profile_url)
    if required_profile_url:
        if required_profile is None:
            verified = False
            reason_codes.append("author_profile_source_invalid")
        elif (
            required_profile["stable_id"] != stable_id
            or (
                parsed_profile is not None
                and required_profile["url"] != parsed_profile["url"]
            )
        ):
            verified = False
            reason_codes.append("author_profile_source_mismatch")

    return {
        "stable_id": stable_id or None,
        "profile_url": parsed_profile["url"] if parsed_profile else profile_url or None,
        "display_name": _first_text(
            author_evidence.get("display_name"),
            author_evidence.get("name"),
            author_evidence.get("nickname"),
        )
        or None,
        "verified": verified,
        "reason_codes": reason_codes,
    }


def _same_author(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if not left.get("verified") or not right.get("verified"):
        return False
    return bool(
        left.get("stable_id") == right.get("stable_id")
        and left.get("profile_url") == right.get("profile_url")
    )


def _snapshot(
    source: str,
    text: Any,
    observed_at: str | None,
    *,
    trusted: bool,
) -> dict[str, Any]:
    exact_text = str(text or "")
    digest = _sha256_text(exact_text)
    return {
        "source": source,
        "text": exact_text,
        "sha256": digest,
        "snapshot_id": f"sha256:{digest}",
        "hash_algorithm": "sha256",
        "byte_length": len(exact_text.encode("utf-8")),
        "present": bool(exact_text.strip()),
        "trusted": bool(trusted and exact_text.strip()),
        "observed_at": observed_at,
    }


def _pinned_comment_snapshot(
    comment: dict[str, Any],
    author: dict[str, Any],
    observed_at: str | None,
) -> tuple[dict[str, Any], list[str]]:
    pinned = comment.get("pinned") is True or comment.get("is_pinned") is True
    nested_author = _mapping(comment.get("author"))
    comment_author = _author_identity(
        {
            "stable_id": comment.get("author_stable_id")
            or nested_author.get("stable_id")
            or nested_author.get("user_id")
            or nested_author.get("id"),
            "profile_url": comment.get("author_profile_url")
            or nested_author.get("profile_url")
            or nested_author.get("url"),
            "display_name": comment.get("author_name")
            or nested_author.get("display_name")
            or nested_author.get("name")
            or nested_author.get("nickname"),
        }
    )
    author_verified = pinned and _same_author(comment_author, author)
    snapshot = _snapshot(
        "pinned_comment",
        comment.get("text"),
        observed_at,
        trusted=author_verified,
    )
    snapshot.update(
        {
            "pinned": pinned,
            "author_stable_id": comment_author.get("stable_id"),
            "author_profile_url": comment_author.get("profile_url"),
            "author_verified": author_verified,
            "included_in_rule": bool(snapshot["present"] and author_verified),
        }
    )
    reasons: list[str] = []
    if snapshot["present"] and not pinned:
        reasons.append("pinned_comment_not_confirmed_pinned")
    if snapshot["present"] and not author_verified:
        reasons.append("pinned_comment_author_unverified")
    return snapshot, reasons


def _content_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
    return _first_mapping(evidence.get("content"), evidence.get("content_evidence"))


def _flat_pinned_comment(evidence: dict[str, Any]) -> Any:
    value = evidence.get("pinned_comment")
    if not isinstance(value, str):
        return value
    return {
        "text": value,
        "pinned": evidence.get("pinned_comment_is_pinned", True),
        "author_stable_id": evidence.get("pinned_comment_author_stable_id"),
        "author_profile_url": evidence.get("pinned_comment_author_profile_url"),
        "author_name": evidence.get("pinned_comment_author_name"),
    }


def _derived_original_trace(
    evidence: dict[str, Any],
    candidate_url: str,
    candidate_page_type: str,
) -> list[dict[str, Any]]:
    """Translate the Worker's flat original-note evidence into the trace contract."""

    original_note = _mapping(evidence.get("original_note"))
    original_url = _first_text(
        evidence.get("original_note_url"),
        original_note.get("raw_url"),
        original_note.get("url"),
        original_note.get("note_url"),
    )
    if not original_url:
        return []
    return [
        {
            "url": candidate_url,
            "page_type": candidate_page_type,
            "next_url": original_url,
        },
        {
            "url": original_url,
            "page_type": "original_note",
            "author": _first_mapping(
                original_note.get("author"),
                evidence.get("original_author"),
                evidence.get("author"),
            ),
        },
    ]


def _date_tokens(
    text: str,
    *,
    reference_year: int | None,
) -> list[tuple[date, str]]:
    values: list[tuple[date, str]] = []
    for match in _DATE_TOKEN_RE.finditer(text):
        year_text = match.group("year")
        if year_text is None and reference_year is None:
            continue
        year = int(year_text) if year_text else int(reference_year)
        try:
            parsed = date(year, int(match.group("month")), int(match.group("day")))
        except ValueError:
            continue
        values.append((parsed, match.group(0)))
    return values


def _extract_activity_window(
    text: str,
    observed: datetime | None,
) -> dict[str, Any]:
    reference_year = observed.year if observed else None
    starts_at: date | None = None
    ends_at: date | None = None
    draw_at: date | None = None
    evidence_fragments: list[str] = []
    parse_errors: list[str] = []

    for match in _ACTIVITY_LABEL_RE.finditer(text):
        label = match.group("label")
        value = match.group("value")
        tokens = _date_tokens(value, reference_year=reference_year)
        evidence_fragments.append(f"{label}：{value}".strip())
        deadline_label = "截止" in label
        if len(tokens) >= 2 and not deadline_label:
            start_candidate, end_candidate = tokens[0][0], tokens[1][0]
            if end_candidate < start_candidate and tokens[1][1].find("20") < 0:
                try:
                    end_candidate = end_candidate.replace(
                        year=start_candidate.year + 1
                    )
                except ValueError:
                    pass
            starts_at = starts_at or start_candidate
            ends_at = ends_at or end_candidate
        elif tokens:
            candidate = tokens[0][0]
            if deadline_label or "即日起" in value:
                ends_at = ends_at or candidate
                if "即日起" in value and observed:
                    starts_at = starts_at or observed.date()
            elif starts_at is None:
                starts_at = candidate
            elif ends_at is None:
                ends_at = candidate
        elif _DATE_TOKEN_RE.search(value):
            parse_errors.append("activity_date_year_unresolved")

    draw_match = _DRAW_LABEL_RE.search(text)
    if draw_match:
        tokens = _date_tokens(
            draw_match.group("value"),
            reference_year=reference_year,
        )
        if tokens:
            draw_at = tokens[0][0]

    status = "unknown"
    if observed and (starts_at or ends_at):
        today = observed.date()
        if starts_at and today < starts_at:
            status = "not_started"
        elif ends_at and today > ends_at:
            status = "expired"
        else:
            status = "active"

    return {
        "starts_at": starts_at.isoformat() if starts_at else None,
        "ends_at": ends_at.isoformat() if ends_at else None,
        "draw_at": draw_at.isoformat() if draw_at else None,
        "timezone": "Asia/Shanghai",
        "status": status,
        "evidence": evidence_fragments,
        "parse_errors": sorted(set(parse_errors)),
    }


def _extract_prizes(text: str) -> list[dict[str, Any]]:
    prizes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for match in _PRIZE_RE.finditer(text):
        value = match.group("value").strip()
        next_field = _NEXT_FIELD_RE.search(value)
        if next_field:
            value = value[: next_field.start()].strip()
        value = value.strip(" ，,。；;")
        if not value or value in seen:
            continue
        seen.add(value)
        prizes.append(
            {
                "text": value,
                "sha256": _sha256_text(value),
                "source": "trusted_rule_text",
            }
        )
    return prizes


def _source_for_match(
    match_text: str,
    snapshots: dict[str, dict[str, Any]],
) -> list[str]:
    sources: list[str] = []
    for name, snapshot in snapshots.items():
        if snapshot.get("included_in_rule") is False:
            continue
        if snapshot.get("trusted") and match_text in str(snapshot.get("text") or ""):
            sources.append(name)
    return sources


def _complex_conditions(
    text: str,
    rule: dict[str, Any],
    snapshots: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for code, pattern in _COMPLEX_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        excerpt = match.group(0)[:160]
        found[code] = {
            "code": code,
            "evidence": excerpt,
            "sources": _source_for_match(excerpt, snapshots),
            "origin": "text_pattern",
        }
    for unsupported in rule.get("unsupported_actions", []):
        code = _UNSUPPORTED_COMPLEX_CODES.get(str(unsupported), str(unsupported))
        found.setdefault(
            code,
            {
                "code": code,
                "evidence": str(unsupported),
                "sources": [],
                "origin": "parse_lottery_rule",
            },
        )
    return [found[code] for code in sorted(found)]


def _validate_source(
    source_type: str,
    source_value: Any,
    evidence: dict[str, Any],
    candidate_url: str,
    candidate_author: dict[str, Any],
) -> dict[str, Any]:
    input_type = str(source_type or "").strip().lower()
    normalized_type = SOURCE_TYPE_ALIASES.get(input_type, input_type)
    value = str(source_value or "").strip()
    result = {
        "input_type": input_type,
        "type": normalized_type,
        "value": value,
        "valid": True,
        "short_link": False,
        "reason_codes": [],
    }

    def invalid(code: str) -> None:
        result["valid"] = False
        if code not in result["reason_codes"]:
            result["reason_codes"].append(code)

    if normalized_type not in SOURCE_TYPES:
        invalid("source_type_unsupported")
        return result

    if normalized_type == "keyword":
        if not value or len(value) > 64:
            invalid("keyword_source_value_invalid")
            return result
        search_result = _first_mapping(
            evidence.get("search_result"),
            evidence.get("source_observation"),
        )
        observed_query = _first_text(
            search_result.get("query"),
            search_result.get("keyword"),
        )
        observed_url = _first_text(
            search_result.get("note_url"),
            search_result.get("url"),
            search_result.get("candidate_url"),
        )
        observed_note = _parse_note_url(observed_url)
        candidate_note = _parse_note_url(candidate_url)
        if not search_result:
            invalid("keyword_source_evidence_missing")
        elif observed_query != value:
            invalid("keyword_source_query_mismatch")
        elif (
            observed_note is None
            or candidate_note is None
            or observed_note["url"] != candidate_note["url"]
        ):
            invalid("keyword_source_candidate_mismatch")

    elif normalized_type == "author_profile":
        parsed_profile = _parse_profile_url(value)
        if parsed_profile is None:
            invalid("author_profile_source_invalid")
        elif (
            not candidate_author.get("verified")
            or candidate_author.get("stable_id") != parsed_profile["stable_id"]
            or candidate_author.get("profile_url") != parsed_profile["url"]
        ):
            invalid("author_profile_source_mismatch")

    else:
        source_observation = _first_mapping(
            evidence.get("offline_record"),
            evidence.get("source_observation"),
        )
        observed_url = _first_text(
            source_observation.get("raw_url"),
            source_observation.get("note_url"),
            source_observation.get("url"),
            source_observation.get("candidate_url"),
        )
        # A URL-valued source label is retained for the earlier direct-url
        # contract, but normal offline imports use a file name or batch label.
        if not observed_url and (
            _parse_note_url(value) is not None
            or _parse_short_link(value) is not None
        ):
            observed_url = value
        direct = _parse_note_url(observed_url)
        short = _parse_short_link(observed_url)
        candidate_note = _parse_note_url(candidate_url)
        if not value or len(value) > 256:
            invalid("offline_source_value_invalid")
        if not observed_url:
            invalid("offline_source_evidence_missing")
        elif short is not None:
            result["short_link"] = True
            result["reason_codes"].append("short_link_requires_review")
        elif direct is None:
            invalid("offline_source_record_note_url_invalid")
        elif candidate_note is None or direct["url"] != candidate_note["url"]:
            invalid("offline_source_candidate_mismatch")

    return result


def _confidence(
    rule_confidence: Any,
    reason_codes: list[str],
    review_reason_codes: list[str],
) -> float:
    try:
        parsed_confidence = float(rule_confidence)
    except (TypeError, ValueError):
        parsed_confidence = 0.0
    score = min(0.95, 0.45 + (0.55 * max(0.0, min(parsed_confidence, 1.0))))
    severe = {
        "source_type_unsupported",
        "candidate_note_url_invalid",
        "candidate_note_url_missing",
        "original_trace_cycle",
        "original_trace_limit_exceeded",
        "author_identity_missing",
        "author_profile_url_invalid",
        "lottery_rule_not_detected",
    }
    moderate = {
        "source_value_invalid",
        "keyword_source_evidence_missing",
        "keyword_source_query_mismatch",
        "keyword_source_candidate_mismatch",
        "author_profile_source_mismatch",
        "offline_source_candidate_mismatch",
        "pinned_comment_author_unverified",
        "content_evidence_missing",
        "required_actions_missing",
        "complex_conditions_present",
        "activity_expired",
        "activity_not_started",
    }
    for code in set(review_reason_codes):
        if code in severe:
            score -= 0.25
        elif code in moderate:
            score -= 0.15
        else:
            score -= 0.08
    if "collection_original_trace_resolved" in reason_codes:
        score -= 0.02
    return round(max(0.0, min(score, 1.0)), 2)


def analyze_candidate_evidence(
    source_type: str,
    source_value: Any,
    evidence: Any,
    observed_at: Any = None,
) -> dict[str, Any]:
    """Analyze pre-collected Xiaohongshu evidence without side effects.

    Primary source types are ``keyword``, ``author_profile``, and
    ``offline_search_result``.  ``search`` and ``direct_url`` remain narrow
    aliases for callers that adopted an earlier draft of the contract.
    """

    evidence_map = _mapping(evidence)
    observed, observed_iso = _normalize_observed_at(observed_at)
    candidate = _first_mapping(
        evidence_map.get("candidate"),
        evidence_map.get("note"),
    )
    candidate_url = _first_text(
        candidate.get("url"),
        candidate.get("note_url"),
        candidate.get("raw_url"),
        evidence_map.get("candidate_url"),
        evidence_map.get("note_url"),
        evidence_map.get("raw_url"),
        source_value
        if SOURCE_TYPE_ALIASES.get(
            str(source_type or "").strip().lower(),
            str(source_type or "").strip().lower(),
        )
        == "offline_search_result"
        else "",
    )
    candidate_note = _parse_note_url(candidate_url)
    candidate_page_type = _page_type(
        candidate.get("page_type")
        or candidate.get("kind")
        or (
            "collection"
            if candidate.get(
                "is_collection",
                evidence_map.get("is_collection"),
            )
            else "note"
        )
    )
    raw_trace = evidence_map.get("original_trace", evidence_map.get("trace"))
    if not isinstance(raw_trace, list):
        raw_trace = _derived_original_trace(
            evidence_map,
            candidate_url,
            candidate_page_type,
        )
    trace_result = _resolve_original_trace(
        candidate_url,
        candidate_page_type,
        raw_trace,
    )

    original_note = _mapping(evidence_map.get("original_note"))
    final_author_evidence = _first_mapping(
        trace_result.get("original_author"),
        evidence_map.get("original_author"),
        original_note.get("author"),
        candidate.get("original_author"),
        candidate.get("author"),
        evidence_map.get("author"),
    )
    required_profile = (
        str(source_value or "").strip()
        if SOURCE_TYPE_ALIASES.get(
            str(source_type or "").strip().lower(),
            str(source_type or "").strip().lower(),
        )
        == "author_profile"
        else ""
    )
    author = _author_identity(
        final_author_evidence,
        required_profile_url=required_profile,
    )
    source = _validate_source(
        source_type,
        source_value,
        evidence_map,
        candidate_url,
        author,
    )

    content = _content_evidence(evidence_map)
    original_content = _content_evidence(original_note)
    body = _snapshot(
        "body",
        _first_exact_text(
            original_content.get("body"),
            original_note.get("body_text"),
            content.get("body"),
            evidence_map.get("body_text"),
            evidence_map.get("body"),
            evidence_map.get("published_text"),
        ),
        observed_iso,
        trusted=True,
    )
    expanded = _snapshot(
        "expanded_body",
        _first_exact_text(
            original_content.get("expanded_body"),
            original_content.get("expanded_content"),
            original_note.get("expanded_text"),
            content.get("expanded_body"),
            content.get("expanded_content"),
            evidence_map.get("expanded_text"),
            evidence_map.get("expanded_body"),
            evidence_map.get("expanded_content"),
        ),
        observed_iso,
        trusted=True,
    )
    raw_pinned = original_content.get(
        "pinned_comment",
        _flat_pinned_comment(original_note),
    )
    if raw_pinned is None:
        raw_pinned = content.get(
            "pinned_comment",
            _flat_pinned_comment(evidence_map),
        )
    if raw_pinned is None:
        raw_pinned = evidence_map.get(
            "pinned_comment",
            original_note.get("pinned_comment"),
        )
    if raw_pinned is None:
        pinned_list = content.get(
            "pinned_comments",
            evidence_map.get("pinned_comments"),
        )
        if isinstance(pinned_list, list) and pinned_list:
            raw_pinned = pinned_list[0]
    pinned, pinned_reasons = _pinned_comment_snapshot(
        _mapping(raw_pinned),
        author,
        observed_iso,
    )
    snapshots = {
        "body": body,
        "expanded_body": expanded,
        "pinned_comment": pinned,
    }

    trusted_texts: list[str] = []
    for snapshot in snapshots.values():
        if not snapshot.get("trusted"):
            continue
        text = str(snapshot.get("text") or "")
        if text and text not in trusted_texts:
            trusted_texts.append(text)
    combined_text = "\n\n".join(trusted_texts)
    parsed_rule = parse_lottery_rule(combined_text, "xiaohongshu")
    rule = {
        "combined_sha256": _sha256_text(combined_text),
        "is_lottery": bool(parsed_rule.get("is_lottery")),
        "required_actions": list(parsed_rule.get("required_actions") or []),
        "review_required": bool(parsed_rule.get("review_required")),
        "confidence": parsed_rule.get("confidence"),
        "matched_rules": list(parsed_rule.get("matched_rules") or []),
        "ambiguity_patterns": list(
            parsed_rule.get("ambiguity_patterns") or []
        ),
        "unsupported_actions": list(
            parsed_rule.get("unsupported_actions") or []
        ),
        "content_requirements": _mapping(
            parsed_rule.get("content_requirements")
        ),
        "friend_mention_requirements": _mapping(
            parsed_rule.get("friend_mention_requirements")
        ),
    }
    activity_window = _extract_activity_window(combined_text, observed)
    prizes = _extract_prizes(combined_text)
    complex_conditions = _complex_conditions(
        combined_text,
        rule,
        snapshots,
    )

    reason_codes: list[str] = []
    review_reason_codes: list[str] = []

    def add_reason(code: str, *, review: bool = True) -> None:
        if code not in reason_codes:
            reason_codes.append(code)
        if review and code not in review_reason_codes:
            review_reason_codes.append(code)

    for code in source.get("reason_codes", []):
        add_reason(
            code,
            review=code != "collection_original_trace_resolved",
        )
    if candidate_note is None:
        add_reason(
            "candidate_note_url_missing"
            if not candidate_url
            else "candidate_note_url_invalid"
        )
    for code in trace_result.get("reason_codes", []):
        add_reason(
            code,
            review=code in trace_result.get("review_reason_codes", []),
        )
    for code in author.get("reason_codes", []):
        add_reason(code)
    for code in pinned_reasons:
        add_reason(code)
    if not combined_text:
        add_reason("content_evidence_missing")
    if not rule["is_lottery"]:
        add_reason("lottery_rule_not_detected")
    if not rule["required_actions"]:
        add_reason("required_actions_missing")
    if rule["review_required"]:
        add_reason("rule_parser_review_required")
    if complex_conditions:
        add_reason("complex_conditions_present")
    if activity_window["parse_errors"]:
        add_reason("activity_window_unresolved")
    if not activity_window["starts_at"] and not activity_window["ends_at"]:
        add_reason("activity_window_missing")
    elif observed is None:
        add_reason("observed_at_required_for_time_check")
    elif activity_window["status"] == "not_started":
        add_reason("activity_not_started")
    elif activity_window["status"] == "expired":
        add_reason("activity_expired")
    if not prizes:
        add_reason("prize_missing")

    target = {
        "candidate_url": candidate_note["url"] if candidate_note else candidate_url or None,
        "candidate_note_id": (
            candidate_note["note_id"] if candidate_note else None
        ),
        "candidate_page_type": candidate_page_type,
        "is_collection": bool(trace_result["is_collection"]),
        "original_note_url": (
            trace_result["original_note_url"]
            if trace_result["complete"]
            else None
        ),
        "original_note_id": (
            trace_result["original_note_id"]
            if trace_result["complete"]
            else None
        ),
        "original_trace": trace_result["entries"],
        "trace_complete": bool(trace_result["complete"]),
        "max_trace_hops": MAX_ORIGINAL_TRACE_HOPS,
    }
    decision = "needs_review" if review_reason_codes else "pending"
    result = {
        "version": ANALYSIS_VERSION,
        "platform": "xiaohongshu",
        "observed_at": observed_iso,
        "source": source,
        "capture_method": _first_text(evidence_map.get("capture_method")) or None,
        "target": target,
        "author": author,
        "content_snapshots": snapshots,
        "rule": rule,
        "activity_window": activity_window,
        "prizes": prizes,
        "complex_conditions": complex_conditions,
        "initial_decision": decision,
        "confidence": _confidence(
            rule.get("confidence"),
            reason_codes,
            review_reason_codes,
        ),
        "reason_codes": reason_codes,
        "review_reason_codes": review_reason_codes,
    }
    # Exercise the serialization contract in-process without returning a second
    # mutable representation.
    json.dumps(result, ensure_ascii=False, sort_keys=True)
    return result
