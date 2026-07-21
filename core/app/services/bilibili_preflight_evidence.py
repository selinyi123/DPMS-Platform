"""Pure validation for immutable Bilibili API preflight observations.

Core deliberately validates Worker observations independently.  A row being
present (or even satisfying a foreign key) is not proof that the JSON describes
the exact reviewed target, account credential generation, and action set.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlparse

from app.action_plan import (
    ACTION_ORDER,
    BILIBILI_API_EXECUTION_PATH,
    BILIBILI_API_PREFLIGHT_CONTRACT_VERSION,
    HANDLE_PATTERN,
    canonical_json_bytes,
    compute_bilibili_api_config_hash,
    sha256_hex,
)


API_PREFLIGHT_KIND = "bilibili_api_readonly_v2"
DPMS_TO_API_ACTION = {
    "followed": "follow",
    "liked": "like",
    "commented": "comment",
    "reposted": "repost",
}
OBSERVATION_FIELDS = frozenset(
    {
        "version",
        "probe_kind",
        "execution_path_id",
        "preflight_contract_version",
        "execution_revision",
        "config_hash",
        "side_effects",
        "account_authenticated",
        "api_preflight_complete",
        "requested_dynamic_id",
        "observed_dynamic_id",
        "target_type",
        "target_uid",
        "author_handle",
        "follow_target_handle",
        "target_identity",
        "comment_rid_str",
        "comment_type",
        "required_actions",
        "api_actions",
        "capability_checks",
    }
)
TARGET_IDENTITY_FIELDS = frozenset(
    {"verified", "dynamic_id", "author_uid", "author_handle"}
)


class BilibiliPreflightEvidenceError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = str(code or "bilibili_api_preflight_observation_invalid")
        super().__init__(self.code)


@dataclass(frozen=True)
class ValidatedBilibiliPreflightEvidence:
    observation: dict[str, Any]
    observation_hash: str


def parse_observation_json(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="strict")
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise BilibiliPreflightEvidenceError(
                "bilibili_api_preflight_observation_invalid"
            ) from exc
        if isinstance(parsed, Mapping):
            return dict(parsed)
    raise BilibiliPreflightEvidenceError(
        "bilibili_api_preflight_observation_invalid"
    )


def hash_preflight_observation(observation: Mapping[str, Any]) -> str:
    return sha256_hex(canonical_json_bytes(dict(observation)))


def extract_bilibili_dynamic_id(*values: str | None) -> str:
    for value in values:
        raw = str(value or "").strip()
        if re.fullmatch(r"\d{10,}", raw):
            return raw
        parsed = urlparse(raw)
        host = (parsed.hostname or "").rstrip(".").lower()
        parts = [part for part in parsed.path.split("/") if part]
        if host == "t.bilibili.com":
            if len(parts) == 1 and parts[0].isdigit():
                return parts[0]
            if len(parts) == 2 and parts[0] == "opus" and parts[1].isdigit():
                return parts[1]
        if host in {"bilibili.com", "www.bilibili.com"}:
            if len(parts) == 2 and parts[0] == "opus" and parts[1].isdigit():
                return parts[1]
    raise BilibiliPreflightEvidenceError("bilibili_dynamic_target_required")


def _positive_int(value: Any, code: str) -> int:
    if type(value) is not int or value <= 0:
        raise BilibiliPreflightEvidenceError(code)
    return value


def validate_preflight_observation(
    value: Any,
    *,
    expected_dynamic_id: str,
    expected_actions: tuple[str, ...] | list[str],
    expected_execution_revision: int,
    expected_config_hash: str,
    expected_follow_handle: str = "",
) -> ValidatedBilibiliPreflightEvidence:
    observation = parse_observation_json(value)
    if set(observation) != OBSERVATION_FIELDS:
        raise BilibiliPreflightEvidenceError(
            "bilibili_api_preflight_observation_schema_invalid"
        )

    expected_action_list = list(expected_actions)
    if (
        not expected_action_list
        or expected_action_list
        != [action for action in ACTION_ORDER if action in set(expected_action_list)]
        or any(action not in DPMS_TO_API_ACTION for action in expected_action_list)
    ):
        raise BilibiliPreflightEvidenceError(
            "bilibili_api_preflight_expected_actions_invalid"
        )
    expected_api_actions = [DPMS_TO_API_ACTION[action] for action in expected_action_list]
    capability_checks = observation.get("capability_checks")
    if (
        type(observation.get("version")) is not int
        or observation.get("version") != 1
        or observation.get("probe_kind") != API_PREFLIGHT_KIND
        or observation.get("execution_path_id") != BILIBILI_API_EXECUTION_PATH
        or type(observation.get("preflight_contract_version")) is not int
        or observation.get("preflight_contract_version")
        != BILIBILI_API_PREFLIGHT_CONTRACT_VERSION
        or observation.get("side_effects") is not False
        or observation.get("account_authenticated") is not True
        or observation.get("api_preflight_complete") is not True
        or observation.get("required_actions") != expected_action_list
        or observation.get("api_actions") != expected_api_actions
        or not isinstance(capability_checks, Mapping)
        or set(capability_checks) != set(expected_action_list)
        or any(capability_checks.get(action) is not True for action in expected_action_list)
    ):
        raise BilibiliPreflightEvidenceError(
            "bilibili_api_preflight_observation_invalid"
        )

    dynamic_id = str(expected_dynamic_id or "").strip()
    if (
        not dynamic_id
        or observation.get("requested_dynamic_id") != dynamic_id
        or observation.get("observed_dynamic_id") != dynamic_id
    ):
        raise BilibiliPreflightEvidenceError("bilibili_api_preflight_target_mismatch")

    execution_revision = _positive_int(
        observation.get("execution_revision"),
        "bilibili_api_preflight_execution_revision_invalid",
    )
    if execution_revision != expected_execution_revision:
        raise BilibiliPreflightEvidenceError(
            "bilibili_api_preflight_execution_revision_mismatch"
        )
    calculated_config_hash = compute_bilibili_api_config_hash(execution_revision)
    if (
        observation.get("config_hash") != calculated_config_hash
        or calculated_config_hash != expected_config_hash
    ):
        raise BilibiliPreflightEvidenceError(
            "bilibili_api_preflight_config_hash_mismatch"
        )

    target_type = _positive_int(
        observation.get("target_type"), "bilibili_api_preflight_target_invalid"
    )
    if target_type == 1:
        raise BilibiliPreflightEvidenceError(
            "bilibili_forwarded_origin_requires_review"
        )
    target_uid = _positive_int(
        observation.get("target_uid"), "bilibili_api_preflight_target_invalid"
    )
    target_identity = observation.get("target_identity")
    if not isinstance(target_identity, Mapping) or set(target_identity) != TARGET_IDENTITY_FIELDS:
        raise BilibiliPreflightEvidenceError(
            "bilibili_api_preflight_target_identity_invalid"
        )
    author_handle = target_identity.get("author_handle")
    if (
        target_identity.get("verified") is not True
        or target_identity.get("dynamic_id") != dynamic_id
        or _positive_int(
            target_identity.get("author_uid"),
            "bilibili_api_preflight_target_identity_invalid",
        )
        != target_uid
        or not isinstance(author_handle, str)
        or not HANDLE_PATTERN.fullmatch(author_handle)
        or observation.get("author_handle") != author_handle
    ):
        raise BilibiliPreflightEvidenceError(
            "bilibili_api_preflight_target_identity_invalid"
        )
    if "followed" in expected_action_list:
        if (
            not expected_follow_handle
            or author_handle != expected_follow_handle
            or observation.get("follow_target_handle") != expected_follow_handle
        ):
            raise BilibiliPreflightEvidenceError(
                "bilibili_api_preflight_follow_target_mismatch"
            )
    elif observation.get("follow_target_handle") not in {"", None}:
        raise BilibiliPreflightEvidenceError(
            "bilibili_api_preflight_unexpected_follow_target"
        )

    if "commented" in expected_action_list:
        if (
            not str(observation.get("comment_rid_str") or "").strip()
            or type(observation.get("comment_type")) is not int
            or observation.get("comment_type") <= 0
        ):
            raise BilibiliPreflightEvidenceError(
                "bilibili_api_preflight_comment_target_invalid"
            )

    return ValidatedBilibiliPreflightEvidence(
        observation=observation,
        observation_hash=hash_preflight_observation(observation),
    )


def validate_preflight_observation_binding(
    value: Any,
    *,
    source_observation_kind: str,
    source_observation_hash: str,
    evidence_observation_kind: str,
    evidence_observation_hash: str,
    expected_dynamic_id: str,
    expected_actions: tuple[str, ...] | list[str],
    expected_execution_revision: int,
    expected_config_hash: str,
    expected_follow_handle: str = "",
) -> ValidatedBilibiliPreflightEvidence:
    """Verify both the source row and evidence row name the canonical bytes."""

    if (
        str(source_observation_kind or "") != API_PREFLIGHT_KIND
        or str(evidence_observation_kind or "") != API_PREFLIGHT_KIND
    ):
        raise BilibiliPreflightEvidenceError(
            "bilibili_api_preflight_observation_kind_mismatch"
        )
    validated = validate_preflight_observation(
        value,
        expected_dynamic_id=expected_dynamic_id,
        expected_actions=expected_actions,
        expected_execution_revision=expected_execution_revision,
        expected_config_hash=expected_config_hash,
        expected_follow_handle=expected_follow_handle,
    )
    if (
        validated.observation_hash != str(source_observation_hash or "")
        or validated.observation_hash != str(evidence_observation_hash or "")
    ):
        raise BilibiliPreflightEvidenceError(
            "bilibili_api_preflight_observation_hash_mismatch"
        )
    return validated
