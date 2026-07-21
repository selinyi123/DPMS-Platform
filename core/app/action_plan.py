"""Canonical Action Plan v2 contract shared conceptually with Worker.

Core creates and verifies immutable, hash-addressed plans.  Worker keeps an
independent copy of the validator so a queue message cannot weaken this
contract; cross-service test vectors pin the canonical representation.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping


LEGACY_ACTION_ORDER = ("followed", "liked", "commented", "reposted")
# Preserve the relative order of every existing action while adding collection
# before repost.  The global order is used for deterministic presentation and
# hashes; platform validators still enforce their own supported subset.
ACTION_ORDER = ("followed", "liked", "commented", "favorited", "reposted")
ACTION_SET = frozenset(ACTION_ORDER)
BILIBILI_ACTION_ORDER = LEGACY_ACTION_ORDER
XIAOHONGSHU_ACTION_ORDER = ("followed", "liked", "commented", "favorited")
PLATFORM_ACTION_ORDERS = {
    "bilibili": BILIBILI_ACTION_ORDER,
    "xiaohongshu": XIAOHONGSHU_ACTION_ORDER,
}
TEXT_ACTIONS = frozenset({"commented", "reposted"})
OPTIONAL_TEXT_METADATA = frozenset(
    {"topic_tags", "mentions", "media_refs", "translation"}
)
FOLLOW_PAYLOAD_FIELDS = frozenset({"target_handle"})
CONTENT_REQUIREMENT_ACTIONS = ("commented", "reposted")
CONTENT_REQUIREMENT_FIELDS = ("topic_tags", "mentions")
HANDLE_PATTERN = re.compile(r"@[\w\u4e00-\u9fff-]{1,64}\Z")
BILIBILI_API_EXECUTION_PATH = "bilibili_api_v2"
BILIBILI_API_PREFLIGHT_CONTRACT_VERSION = 1
XIAOHONGSHU_MANUAL_EXECUTION_PATH = "xiaohongshu_manual_v1"
XIAOHONGSHU_NO_OFFICIAL_API_BLOCKER = (
    "xiaohongshu_no_official_interaction_api"
)


class ActionPlanV2Error(ValueError):
    """The plan cannot safely authorize an external mutation."""

    def __init__(self, code: str) -> None:
        self.code = str(code or "action_plan_v2_invalid")
        super().__init__(self.code)


@dataclass(frozen=True)
class ValidatedActionPlanV2:
    plan: dict[str, Any]
    plan_hash: str
    rule_snapshot_id: int
    rule_hash: str
    execution_path_id: str
    required_actions: tuple[str, ...]
    action_payloads: dict[str, dict[str, Any]]
    content_requirements: dict[str, Any]

    def payload_for(self, action: str) -> dict[str, Any]:
        return dict(self.action_payloads.get(action, {}))

    @property
    def follow_target_handle(self) -> str:
        return str(self.action_payloads.get("followed", {}).get("target_handle") or "")


def action_order_for_platform(platform: str) -> tuple[str, ...]:
    """Return the canonical action subset for a platform.

    Unknown and existing non-Xiaohongshu platforms retain the legacy four
    actions.  This prevents the new ``favorited`` action from silently becoming
    valid for Bilibili, Weibo or Douyin plans.
    """

    key = str(platform or "").strip().casefold()
    return PLATFORM_ACTION_ORDERS.get(key, LEGACY_ACTION_ORDER)


def default_execution_path_for_platform(platform: str) -> str:
    if str(platform or "").strip().casefold() == "xiaohongshu":
        return XIAOHONGSHU_MANUAL_EXECUTION_PATH
    # Preserve the previous request-model default for every existing caller.
    return BILIBILI_API_EXECUTION_PATH


def canonical_json_bytes(value: Any) -> bytes:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ActionPlanV2Error("action_plan_not_canonicalizable") from exc
    return encoded.encode("utf-8")


def sha256_hex(value: bytes | str) -> str:
    raw = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(raw).hexdigest()


def compute_rule_hash(rule_text: str) -> str:
    """Hash exact stored UTF-8 source text; whitespace remains authoritative."""

    if not isinstance(rule_text, str) or not rule_text:
        raise ActionPlanV2Error("rule_text_required")
    return sha256_hex(rule_text)


def compute_action_plan_hash(plan: Mapping[str, Any]) -> str:
    canonical_plan = dict(plan)
    canonical_plan.pop("plan_hash", None)
    return sha256_hex(canonical_json_bytes(canonical_plan))


def compute_target_hash(canonical_url: str) -> str:
    value = str(canonical_url or "").strip()
    if not value:
        raise ActionPlanV2Error("canonical_target_required")
    return sha256_hex(value)


def compute_config_hash(config: Mapping[str, Any] | None) -> str:
    return sha256_hex(canonical_json_bytes(dict(config or {})))


def compute_bilibili_api_config_hash(execution_revision: int) -> str:
    """Bind preflight evidence to both contract and credential generation."""

    if type(execution_revision) is not int or execution_revision <= 0:
        raise ActionPlanV2Error("execution_revision_invalid")
    return compute_config_hash(
        {
            "execution_revision": execution_revision,
            "execution_path_id": BILIBILI_API_EXECUTION_PATH,
            "preflight_contract_version": BILIBILI_API_PREFLIGHT_CONTRACT_VERSION,
        }
    )


def _required_string(plan: Mapping[str, Any], field: str) -> str:
    value = plan.get(field)
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ActionPlanV2Error(f"action_plan_{field}_invalid")
    return value


def _validated_metadata_list(payload: Mapping[str, Any], field: str) -> list[str]:
    value = payload.get(field, [])
    if value is None:
        return []
    if not isinstance(value, list):
        raise ActionPlanV2Error(f"action_payload_{field}_invalid")
    if len(value) > 32:
        raise ActionPlanV2Error(f"action_payload_{field}_too_many")
    result: list[str] = []
    for item in value:
        if (
            not isinstance(item, str)
            or not item
            or item != item.strip()
            or len(item.encode("utf-8")) > 512
            or item in result
        ):
            raise ActionPlanV2Error(f"action_payload_{field}_invalid")
        result.append(item)
    return result


def validate_action_payload(action: str, value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ActionPlanV2Error("action_payload_invalid")
    payload = dict(value)
    if action == "followed":
        if set(payload) != FOLLOW_PAYLOAD_FIELDS:
            raise ActionPlanV2Error("action_payload_followed_target_required")
        target_handle = payload.get("target_handle")
        if (
            not isinstance(target_handle, str)
            or not HANDLE_PATTERN.fullmatch(target_handle)
            or len(target_handle.encode("utf-8")) > 512
        ):
            raise ActionPlanV2Error("action_payload_followed_target_invalid")
        return payload
    if action not in TEXT_ACTIONS:
        if payload:
            raise ActionPlanV2Error("non_text_action_payload_not_empty")
        return payload

    if set(payload) - ({"text"} | OPTIONAL_TEXT_METADATA):
        raise ActionPlanV2Error("action_payload_unknown_field")
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ActionPlanV2Error(f"action_payload_{action}_text_required")
    if len(text.encode("utf-8")) > 4096:
        raise ActionPlanV2Error(f"action_payload_{action}_text_too_large")

    topic_tags = _validated_metadata_list(payload, "topic_tags")
    mentions = _validated_metadata_list(payload, "mentions")
    _validated_metadata_list(payload, "media_refs")
    for required_token in (*topic_tags, *mentions):
        if required_token not in text:
            raise ActionPlanV2Error("action_payload_required_token_missing")

    translation = payload.get("translation")
    if translation is not None:
        if not isinstance(translation, str) or not translation.strip():
            raise ActionPlanV2Error("action_payload_translation_invalid")
        if translation not in text:
            raise ActionPlanV2Error("action_payload_translation_missing")
    return payload


def normalize_action_payloads(
    required_actions: list[str],
    value: Mapping[str, Any] | None,
    *,
    allowed_actions: frozenset[str] = ACTION_SET,
) -> dict[str, dict[str, Any]]:
    raw = dict(value or {})
    if set(raw) - allowed_actions:
        raise ActionPlanV2Error("action_plan_payload_unknown_action")
    if set(raw) != set(required_actions):
        raise ActionPlanV2Error("action_plan_payload_binding_mismatch")
    result: dict[str, dict[str, Any]] = {}
    for action in required_actions:
        result[action] = validate_action_payload(action, raw.get(action, {}))
    return result


def semantic_requirement_status(
    unsupported_actions: list[str],
    action_payloads: Mapping[str, Mapping[str, Any]],
    content_requirements: Mapping[str, Any] | None = None,
) -> tuple[list[str], list[str], list[str]]:
    """Split parser requirements into represented, unresolved and unsupported.

    Content requirements become representable only when the exact reviewed
    payload carries their structured metadata.  Media can be represented but
    the current Bilibili API path still cannot execute it, so it is returned as
    a capability blocker rather than silently discarded.
    """

    exact_requirements = dict(content_requirements or {})
    payload_values = list(action_payloads.values())

    def exact_action_tokens(field: str) -> bool:
        any_required = False
        for action in CONTENT_REQUIREMENT_ACTIONS:
            action_requirement = exact_requirements.get(action, {})
            required = (
                action_requirement.get(field, [])
                if isinstance(action_requirement, Mapping)
                else []
            )
            if required:
                any_required = True
            declared = action_payloads.get(action, {}).get(field, [])
            if not isinstance(declared, list) or declared != required:
                return False
        return any_required

    represented: list[str] = []
    unresolved: list[str] = []
    capability_blockers: list[str] = []
    if "followed" in action_payloads:
        follow_targets = exact_requirements.get("follow_targets", [])
        target_handle = action_payloads.get("followed", {}).get("target_handle")
        if (
            not isinstance(follow_targets, list)
            or len(follow_targets) != 1
            or target_handle != follow_targets[0]
        ):
            unresolved.append("follow_target")
    for requirement in dict.fromkeys(str(item) for item in unsupported_actions):
        resolved = False
        if requirement == "topic_tag":
            resolved = exact_action_tokens("topic_tags")
        elif requirement == "mention_account":
            resolved = exact_action_tokens("mentions")
        elif requirement == "media_submission":
            resolved = any(payload.get("media_refs") for payload in payload_values)
            if resolved:
                capability_blockers.append("bilibili_media_submission_unsupported")
        elif requirement == "translation_required":
            resolved = any(payload.get("translation") for payload in payload_values)
        elif requirement == "comment_content":
            resolved = bool(action_payloads.get("commented", {}).get("text"))
        elif requirement == "repost_content":
            resolved = bool(action_payloads.get("reposted", {}).get("text"))
        if resolved:
            represented.append(requirement)
        else:
            unresolved.append(requirement)
    if any(payload.get("media_refs") for payload in payload_values):
        capability_blockers.append("bilibili_media_submission_unsupported")
    return represented, unresolved, list(dict.fromkeys(capability_blockers))


def bind_xiaohongshu_manual_follow_target(
    required_actions: list[str] | tuple[str, ...],
    action_payloads: Mapping[str, Mapping[str, Any]],
    content_requirements: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind an implicit note-author follow rule to the reviewed exact handle.

    Xiaohongshu rules commonly say ``关注博主`` or simply ``四连`` without
    repeating the author's handle. The manual-only contract may use the
    operator-reviewed ``target_handle`` in that narrow case. Explicit source
    handles are never replaced, so ambiguous/multiple targets still fail
    closed in the normal semantic validator.
    """

    requirements = {
        "follow_targets": list(content_requirements.get("follow_targets") or []),
        "commented": dict(content_requirements.get("commented") or {}),
        "reposted": dict(content_requirements.get("reposted") or {}),
    }
    if "followed" not in required_actions or requirements["follow_targets"]:
        return requirements
    target_handle = action_payloads.get("followed", {}).get("target_handle")
    if isinstance(target_handle, str) and HANDLE_PATTERN.fullmatch(target_handle):
        requirements["follow_targets"] = [target_handle]
    return requirements


def _validated_content_requirements(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
        "follow_targets",
        "commented",
        "reposted",
    }:
        raise ActionPlanV2Error("action_plan_content_requirements_invalid")
    requirements: dict[str, Any] = {
        "follow_targets": _validated_metadata_list(value, "follow_targets"),
    }
    if any(
        not HANDLE_PATTERN.fullmatch(target)
        for target in requirements["follow_targets"]
    ):
        raise ActionPlanV2Error("action_plan_follow_target_source_invalid")
    for action in CONTENT_REQUIREMENT_ACTIONS:
        action_value = value.get(action)
        if not isinstance(action_value, Mapping) or set(action_value) != set(
            CONTENT_REQUIREMENT_FIELDS
        ):
            raise ActionPlanV2Error("action_plan_content_requirements_invalid")
        requirements[action] = {
            field: _validated_metadata_list(action_value, field)
            for field in CONTENT_REQUIREMENT_FIELDS
        }
    return requirements


def validate_action_plan_v2(
    value: Any,
    *,
    require_executable: bool = True,
    reject_media: bool = False,
) -> ValidatedActionPlanV2:
    if not isinstance(value, Mapping):
        raise ActionPlanV2Error("action_plan_v2_invalid")
    plan = dict(value)
    if type(plan.get("version")) is not int or plan.get("version") != 2:
        raise ActionPlanV2Error("action_plan_version_unsupported")
    raw_platform = plan.get("platform")
    is_xiaohongshu = bool(
        isinstance(raw_platform, str)
        and raw_platform.strip().casefold() == "xiaohongshu"
    )
    # Xiaohongshu has no official interaction API.  Even callers validating a
    # non-executable/manual plan must never be able to smuggle an executable
    # claim through this shared contract.
    if is_xiaohongshu and plan.get("executable") is not False:
        raise ActionPlanV2Error(
            "xiaohongshu_manual_plan_must_be_non_executable"
        )
    if require_executable and plan.get("executable") is not True:
        raise ActionPlanV2Error("action_plan_not_executable")
    if plan.get("review_required") is not False:
        raise ActionPlanV2Error("action_plan_review_required")
    reviewed_by = plan.get("reviewed_by")
    if (
        not isinstance(reviewed_by, str)
        or not reviewed_by.strip()
        or reviewed_by != reviewed_by.strip()
        or len(reviewed_by.encode("utf-8")) > 128
        or plan.get("rule_complete_confirmed") is not True
    ):
        raise ActionPlanV2Error("action_plan_review_attestation_invalid")

    snapshot_id = plan.get("rule_snapshot_id")
    if type(snapshot_id) is not int or snapshot_id <= 0:
        raise ActionPlanV2Error("action_plan_rule_snapshot_id_invalid")
    rule_hash = _required_string(plan, "rule_hash")
    path_id = _required_string(plan, "execution_path_id")
    if is_xiaohongshu and path_id != XIAOHONGSHU_MANUAL_EXECUTION_PATH:
        raise ActionPlanV2Error("xiaohongshu_execution_path_not_supported")
    plan_hash = _required_string(plan, "plan_hash")
    for field, value_hash in (("rule_hash", rule_hash), ("hash", plan_hash)):
        if len(value_hash) != 64 or any(ch not in "0123456789abcdef" for ch in value_hash):
            raise ActionPlanV2Error(f"action_plan_{field}_invalid")

    platform = _required_string(plan, "platform")
    platform_order = action_order_for_platform(platform)
    platform_action_set = frozenset(platform_order)
    raw_actions = plan.get("required_actions")
    if not isinstance(raw_actions, list) or not raw_actions:
        raise ActionPlanV2Error("action_plan_required_actions_invalid")
    actions: list[str] = []
    for action in raw_actions:
        if (
            not isinstance(action, str)
            or action not in platform_action_set
            or action in actions
        ):
            raise ActionPlanV2Error("action_plan_required_actions_invalid")
        actions.append(action)
    normalized = tuple(action for action in platform_order if action in set(actions))
    if tuple(actions) != normalized:
        raise ActionPlanV2Error("action_plan_action_order_invalid")
    if platform.casefold() == "xiaohongshu" and normalized != XIAOHONGSHU_ACTION_ORDER:
        raise ActionPlanV2Error("xiaohongshu_four_action_plan_required")
    if compute_action_plan_hash(plan) != plan_hash:
        raise ActionPlanV2Error("action_plan_hash_mismatch")
    payloads = normalize_action_payloads(
        actions,
        plan.get("action_payloads"),
        allowed_actions=platform_action_set,
    )
    content_requirements = _validated_content_requirements(
        plan.get("content_requirements")
    )
    if is_xiaohongshu and content_requirements["reposted"] != {
        "topic_tags": [],
        "mentions": [],
    }:
        raise ActionPlanV2Error("xiaohongshu_repost_content_not_supported")
    for action in CONTENT_REQUIREMENT_ACTIONS:
        for field, mismatch_code in (
            ("topic_tags", "action_plan_required_topic_mismatch"),
            ("mentions", "action_plan_required_mention_mismatch"),
        ):
            if payloads.get(action, {}).get(field, []) != content_requirements[action][field]:
                raise ActionPlanV2Error(mismatch_code)

    follow_targets = content_requirements["follow_targets"]
    if "followed" in actions:
        if len(follow_targets) != 1:
            raise ActionPlanV2Error("action_plan_follow_target_source_ambiguous")
        if payloads["followed"].get("target_handle") != follow_targets[0]:
            raise ActionPlanV2Error("action_plan_follow_target_mismatch")
    elif follow_targets:
        raise ActionPlanV2Error("action_plan_follow_target_without_action")
    if reject_media and any(payload.get("media_refs") for payload in payloads.values()):
        raise ActionPlanV2Error("action_payload_media_unsupported")
    return ValidatedActionPlanV2(
        plan=plan,
        plan_hash=plan_hash,
        rule_snapshot_id=snapshot_id,
        rule_hash=rule_hash,
        execution_path_id=path_id,
        required_actions=normalized,
        action_payloads=payloads,
        content_requirements=content_requirements,
    )
