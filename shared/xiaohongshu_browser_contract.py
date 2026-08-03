"""Versioned, hash-addressed Xiaohongshu browser execution evidence.

This module is deliberately pure so Core and Worker validate the same bytes at
the external-mutation boundary.  An observation proves only read-only browser
preflight capability; real actions still require a separately verified Probe
+ Shadow pair bound to the exact immutable execution contract.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping


XIAOHONGSHU_BROWSER_CONTRACT_VERSION = 1
XIAOHONGSHU_BROWSER_EXECUTION_PATH = "xiaohongshu_browser_v1"
XIAOHONGSHU_BROWSER_PROBE_OBSERVATION_KIND = (
    "xiaohongshu_browser_probe_v1"
)
XIAOHONGSHU_BROWSER_SHADOW_OBSERVATION_KIND = (
    "xiaohongshu_browser_shadow_v1"
)
XIAOHONGSHU_BROWSER_OBSERVATION_KINDS = frozenset(
    {
        XIAOHONGSHU_BROWSER_PROBE_OBSERVATION_KIND,
        XIAOHONGSHU_BROWSER_SHADOW_OBSERVATION_KIND,
    }
)
XIAOHONGSHU_BROWSER_ACTION_ORDER = (
    "followed",
    "liked",
    "commented",
    "favorited",
)
XIAOHONGSHU_BROWSER_OBSERVATION_FIELDS = frozenset(
    {
        "contract_version",
        "platform",
        "execution_path_id",
        "lottery_id",
        "account_id",
        "execution_revision",
        "target_hash",
        "observed_target_hash",
        "rule_snapshot_id",
        "rule_hash",
        "action_plan_hash",
        "config_hash",
        "required_actions",
        "follow_target_handle",
        "comment_text_hash",
        "observation_kind",
        "observed_at",
        "evidence_id",
        "side_effects",
        "account_authenticated",
        "target_identity_verified",
        "selector_observation_complete",
        "capability_checks",
    }
)

_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}", re.ASCII)
_UTC_TIMESTAMP_PATTERN = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z",
    re.ASCII,
)


class XiaohongshuBrowserContractError(ValueError):
    def __init__(self, code: str):
        self.code = str(code)
        super().__init__(self.code)


@dataclass(frozen=True)
class ValidatedXiaohongshuBrowserObservation:
    observation: dict[str, Any]
    observation_hash: str
    observed_at: datetime


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return encoded.encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise XiaohongshuBrowserContractError(
            "xiaohongshu_browser_contract_not_canonicalizable"
        ) from exc


def _sha256_hex(value: bytes | str) -> str:
    raw = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(raw).hexdigest()


def compute_xiaohongshu_browser_config_hash(
    execution_revision: int,
    selector_config: Mapping[str, Any],
) -> str:
    """Bind selector bytes to one browser-session credential generation."""

    if type(execution_revision) is not int or execution_revision <= 0:
        raise XiaohongshuBrowserContractError("execution_revision_invalid")
    if not isinstance(selector_config, Mapping) or not selector_config:
        raise XiaohongshuBrowserContractError(
            "xiaohongshu_selector_config_invalid"
        )
    return _sha256_hex(
        _canonical_json_bytes(
            {
                "contract_version": XIAOHONGSHU_BROWSER_CONTRACT_VERSION,
                "execution_path_id": XIAOHONGSHU_BROWSER_EXECUTION_PATH,
                "execution_revision": execution_revision,
                "selector_config": dict(selector_config),
            }
        )
    )


def compute_xiaohongshu_comment_text_hash(comment_text: str) -> str:
    """Hash the exact reviewed comment, including whitespace and Unicode."""

    if not isinstance(comment_text, str):
        raise XiaohongshuBrowserContractError(
            "xiaohongshu_comment_text_invalid"
        )
    try:
        return _sha256_hex(comment_text.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise XiaohongshuBrowserContractError(
            "xiaohongshu_comment_text_invalid"
        ) from exc


def hash_xiaohongshu_browser_observation(
    observation: Mapping[str, Any],
) -> str:
    return _sha256_hex(_canonical_json_bytes(dict(observation)))


def format_xiaohongshu_observed_at(value: datetime) -> str:
    """Return the canonical UTC timestamp emitted by Worker observations."""

    if not isinstance(value, datetime) or value.tzinfo is None:
        raise XiaohongshuBrowserContractError(
            "xiaohongshu_browser_observed_at_invalid"
        )
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _parse_observation(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise XiaohongshuBrowserContractError(
                "xiaohongshu_browser_observation_invalid"
            ) from exc
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise XiaohongshuBrowserContractError(
                "xiaohongshu_browser_observation_invalid"
            ) from exc
        if isinstance(parsed, Mapping):
            return dict(parsed)
    raise XiaohongshuBrowserContractError(
        "xiaohongshu_browser_observation_invalid"
    )


def _positive_int(value: Any) -> bool:
    return type(value) is int and value > 0


def _exact_token(value: Any, *, max_length: int = 128) -> bool:
    return bool(
        isinstance(value, str)
        and value
        and value == value.strip()
        and len(value) <= max_length
    )


def _parse_observed_at(value: Any) -> datetime:
    if not isinstance(value, str) or not _UTC_TIMESTAMP_PATTERN.fullmatch(value):
        raise XiaohongshuBrowserContractError(
            "xiaohongshu_browser_observed_at_invalid"
        )
    try:
        parsed = datetime.fromisoformat(f"{value[:-1]}+00:00")
    except ValueError as exc:
        raise XiaohongshuBrowserContractError(
            "xiaohongshu_browser_observed_at_invalid"
        ) from exc
    return parsed.astimezone(timezone.utc)


def validate_xiaohongshu_browser_observation(
    value: Any,
    *,
    expected_observation_kind: str,
    expected_evidence_id: str,
    expected_lottery_id: int,
    expected_account_id: int,
    expected_execution_revision: int,
    expected_target_hash: str,
    expected_rule_snapshot_id: int,
    expected_rule_hash: str,
    expected_action_plan_hash: str,
    expected_config_hash: str,
    expected_actions: tuple[str, ...] | list[str],
    expected_follow_target_handle: str = "",
    expected_comment_text_hash: str,
) -> ValidatedXiaohongshuBrowserObservation:
    """Validate the exact read-only proof and all immutable bindings."""

    observation = _parse_observation(value)
    if set(observation) != XIAOHONGSHU_BROWSER_OBSERVATION_FIELDS:
        raise XiaohongshuBrowserContractError(
            "xiaohongshu_browser_observation_schema_invalid"
        )
    actions = list(expected_actions)
    if (
        not actions
        or actions
        != [
            action
            for action in XIAOHONGSHU_BROWSER_ACTION_ORDER
            if action in set(actions)
        ]
        or len(actions) != len(set(actions))
    ):
        raise XiaohongshuBrowserContractError(
            "xiaohongshu_browser_expected_actions_invalid"
        )
    capability_checks = observation.get("capability_checks")
    expected_hashes = (
        expected_target_hash,
        expected_rule_hash,
        expected_action_plan_hash,
        expected_config_hash,
        expected_comment_text_hash,
    )
    if (
        expected_observation_kind
        not in XIAOHONGSHU_BROWSER_OBSERVATION_KINDS
        or not _exact_token(expected_evidence_id)
        or not _positive_int(expected_lottery_id)
        or not _positive_int(expected_account_id)
        or not _positive_int(expected_execution_revision)
        or not _positive_int(expected_rule_snapshot_id)
        or any(not _SHA256_PATTERN.fullmatch(str(value or "")) for value in expected_hashes)
        or not isinstance(expected_follow_target_handle, str)
        or observation.get("contract_version")
        != XIAOHONGSHU_BROWSER_CONTRACT_VERSION
        or observation.get("platform") != "xiaohongshu"
        or observation.get("execution_path_id")
        != XIAOHONGSHU_BROWSER_EXECUTION_PATH
        or observation.get("lottery_id") != expected_lottery_id
        or observation.get("account_id") != expected_account_id
        or observation.get("execution_revision")
        != expected_execution_revision
        or observation.get("target_hash") != expected_target_hash
        or observation.get("observed_target_hash") != expected_target_hash
        or observation.get("rule_snapshot_id")
        != expected_rule_snapshot_id
        or observation.get("rule_hash") != expected_rule_hash
        or observation.get("action_plan_hash")
        != expected_action_plan_hash
        or observation.get("config_hash") != expected_config_hash
        or observation.get("required_actions") != actions
        or observation.get("follow_target_handle")
        != expected_follow_target_handle
        or observation.get("comment_text_hash")
        != expected_comment_text_hash
        or observation.get("observation_kind")
        != expected_observation_kind
        or observation.get("evidence_id") != expected_evidence_id
        or observation.get("side_effects") is not False
        or observation.get("account_authenticated") is not True
        or observation.get("target_identity_verified") is not True
        or observation.get("selector_observation_complete") is not True
        or not isinstance(capability_checks, Mapping)
        or set(capability_checks) != set(actions)
        or any(capability_checks.get(action) is not True for action in actions)
    ):
        raise XiaohongshuBrowserContractError(
            "xiaohongshu_browser_observation_binding_mismatch"
        )
    if "followed" in actions:
        if not expected_follow_target_handle:
            raise XiaohongshuBrowserContractError(
                "xiaohongshu_browser_follow_target_mismatch"
            )
    elif expected_follow_target_handle or observation["follow_target_handle"]:
        raise XiaohongshuBrowserContractError(
            "xiaohongshu_browser_follow_target_mismatch"
        )
    observed_at = _parse_observed_at(observation.get("observed_at"))
    return ValidatedXiaohongshuBrowserObservation(
        observation=observation,
        observation_hash=hash_xiaohongshu_browser_observation(observation),
        observed_at=observed_at,
    )


def validate_xiaohongshu_browser_observation_binding(
    value: Any,
    *,
    source_observation_kind: str,
    source_observation_hash: str,
    evidence_observation_kind: str,
    evidence_observation_hash: str,
    **expected: Any,
) -> ValidatedXiaohongshuBrowserObservation:
    """Verify source and materialized evidence rows name identical bytes."""

    expected_kind = str(expected.get("expected_observation_kind") or "")
    if (
        source_observation_kind != expected_kind
        or evidence_observation_kind != expected_kind
    ):
        raise XiaohongshuBrowserContractError(
            "xiaohongshu_browser_observation_kind_mismatch"
        )
    validated = validate_xiaohongshu_browser_observation(value, **expected)
    if (
        validated.observation_hash != source_observation_hash
        or validated.observation_hash != evidence_observation_hash
    ):
        raise XiaohongshuBrowserContractError(
            "xiaohongshu_browser_observation_hash_mismatch"
        )
    return validated


__all__ = (
    "XIAOHONGSHU_BROWSER_ACTION_ORDER",
    "XIAOHONGSHU_BROWSER_CONTRACT_VERSION",
    "XIAOHONGSHU_BROWSER_EXECUTION_PATH",
    "XIAOHONGSHU_BROWSER_OBSERVATION_FIELDS",
    "XIAOHONGSHU_BROWSER_OBSERVATION_KINDS",
    "XIAOHONGSHU_BROWSER_PROBE_OBSERVATION_KIND",
    "XIAOHONGSHU_BROWSER_SHADOW_OBSERVATION_KIND",
    "ValidatedXiaohongshuBrowserObservation",
    "XiaohongshuBrowserContractError",
    "compute_xiaohongshu_browser_config_hash",
    "compute_xiaohongshu_comment_text_hash",
    "format_xiaohongshu_observed_at",
    "hash_xiaohongshu_browser_observation",
    "validate_xiaohongshu_browser_observation",
    "validate_xiaohongshu_browser_observation_binding",
)
