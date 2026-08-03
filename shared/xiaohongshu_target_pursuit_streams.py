"""Redis contract for read-only Xiaohongshu target pursuit.

The request stream carries only bounded source locators.  Browser credentials
never enter Redis.  Results are short-lived evidence envelopes consumed by the
Core API and then persisted in the candidate review projection.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import uuid


XIAOHONGSHU_TARGET_PURSUIT_PROTOCOL_VERSION = 1
XIAOHONGSHU_TARGET_PURSUIT_STREAM_KEY = (
    "xiaohongshu_target_pursuit_requests:v1"
)
XIAOHONGSHU_TARGET_PURSUIT_GROUP_NAME = (
    "xiaohongshu-target-pursuers:v1"
)
XIAOHONGSHU_TARGET_PURSUIT_RESULT_PREFIX = (
    "xiaohongshu_target_pursuit_result:v1"
)
XIAOHONGSHU_TARGET_PURSUIT_KEYWORD_MAX_LENGTH = 64
XIAOHONGSHU_TARGET_PURSUIT_SOURCE_TYPES = frozenset(
    {"keyword", "author_profile", "offline_search_result"}
)
XIAOHONGSHU_TARGET_PURSUIT_BROWSER_SOURCE_TYPES = frozenset(
    {"keyword", "author_profile"}
)
XIAOHONGSHU_TARGET_PURSUIT_REQUEST_FIELDS = frozenset(
    {
        "protocol_version",
        "request_id",
        "source_type",
        "source_value",
        "requested_at_ms",
        "max_candidates",
    }
)
_REQUEST_ID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}$"
)


@dataclass(frozen=True)
class XiaohongshuTargetPursuitStreamBinding:
    stream_key: str = XIAOHONGSHU_TARGET_PURSUIT_STREAM_KEY
    group_name: str = XIAOHONGSHU_TARGET_PURSUIT_GROUP_NAME
    platform: str = "xiaohongshu"


XIAOHONGSHU_TARGET_PURSUIT_STREAM_BINDING = (
    XiaohongshuTargetPursuitStreamBinding()
)


def canonical_target_pursuit_request_id(value) -> str:
    normalized = str(value or "").strip().casefold()
    if not _REQUEST_ID_PATTERN.fullmatch(normalized):
        raise ValueError("xiaohongshu_target_pursuit_request_id_invalid")
    try:
        parsed = uuid.UUID(normalized)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(
            "xiaohongshu_target_pursuit_request_id_invalid"
        ) from exc
    if str(parsed) != normalized:
        raise ValueError(
            "xiaohongshu_target_pursuit_request_id_invalid"
        )
    return normalized


def xiaohongshu_target_pursuit_result_key(request_id: str) -> str:
    return (
        f"{XIAOHONGSHU_TARGET_PURSUIT_RESULT_PREFIX}:"
        f"{canonical_target_pursuit_request_id(request_id)}"
    )


def validate_target_pursuit_stream_fields(fields: dict) -> dict[str, str]:
    if not isinstance(fields, dict) or set(fields) != (
        XIAOHONGSHU_TARGET_PURSUIT_REQUEST_FIELDS
    ):
        raise ValueError(
            "xiaohongshu_target_pursuit_request_fields_invalid"
        )
    normalized = {}
    for raw_key, raw_value in fields.items():
        if isinstance(raw_key, bytes):
            raw_key = raw_key.decode("utf-8", errors="strict")
        if isinstance(raw_value, bytes):
            raw_value = raw_value.decode("utf-8", errors="strict")
        if not isinstance(raw_key, str) or not isinstance(
            raw_value, (str, int)
        ) or isinstance(raw_value, bool):
            raise ValueError(
                "xiaohongshu_target_pursuit_request_fields_invalid"
            )
        normalized[raw_key] = str(raw_value)

    if normalized["protocol_version"] != str(
        XIAOHONGSHU_TARGET_PURSUIT_PROTOCOL_VERSION
    ):
        raise ValueError(
            "xiaohongshu_target_pursuit_protocol_version_invalid"
        )
    normalized["request_id"] = canonical_target_pursuit_request_id(
        normalized["request_id"]
    )
    source_type = normalized["source_type"].strip().casefold()
    if source_type not in XIAOHONGSHU_TARGET_PURSUIT_BROWSER_SOURCE_TYPES:
        raise ValueError(
            "xiaohongshu_target_pursuit_browser_source_unsupported"
        )
    source_value = normalized["source_value"].strip()
    if not source_value or len(source_value) > 256:
        raise ValueError(
            "xiaohongshu_target_pursuit_source_value_invalid"
        )
    if (
        source_type == "keyword"
        and len(source_value) > XIAOHONGSHU_TARGET_PURSUIT_KEYWORD_MAX_LENGTH
    ):
        raise ValueError(
            "xiaohongshu_target_pursuit_source_value_invalid"
        )
    try:
        requested_at_ms = int(normalized["requested_at_ms"])
        max_candidates = int(normalized["max_candidates"])
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "xiaohongshu_target_pursuit_request_number_invalid"
        ) from exc
    if requested_at_ms <= 0 or not 1 <= max_candidates <= 50:
        raise ValueError(
            "xiaohongshu_target_pursuit_request_number_invalid"
        )
    normalized.update(
        source_type=source_type,
        source_value=source_value,
        requested_at_ms=str(requested_at_ms),
        max_candidates=str(max_candidates),
    )
    return normalized


__all__ = (
    "XIAOHONGSHU_TARGET_PURSUIT_BROWSER_SOURCE_TYPES",
    "XIAOHONGSHU_TARGET_PURSUIT_GROUP_NAME",
    "XIAOHONGSHU_TARGET_PURSUIT_KEYWORD_MAX_LENGTH",
    "XIAOHONGSHU_TARGET_PURSUIT_PROTOCOL_VERSION",
    "XIAOHONGSHU_TARGET_PURSUIT_REQUEST_FIELDS",
    "XIAOHONGSHU_TARGET_PURSUIT_RESULT_PREFIX",
    "XIAOHONGSHU_TARGET_PURSUIT_SOURCE_TYPES",
    "XIAOHONGSHU_TARGET_PURSUIT_STREAM_BINDING",
    "XIAOHONGSHU_TARGET_PURSUIT_STREAM_KEY",
    "canonical_target_pursuit_request_id",
    "validate_target_pursuit_stream_fields",
    "xiaohongshu_target_pursuit_result_key",
)
