"""Validation and canonical hashing for immutable real-run Action Plan v2.

The queue message is untrusted.  A worker may execute a real mutation only
after the exact reviewed plan has been bound to the task, lottery, rule
snapshot, evidence and account lease by :mod:`app.real_run_gate`.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping


ACTION_ORDER = ("followed", "liked", "commented", "reposted")
ACTION_SET = frozenset(ACTION_ORDER)
TEXT_ACTIONS = frozenset({"commented", "reposted"})
OPTIONAL_TEXT_METADATA = frozenset(
    {"topic_tags", "mentions", "media_refs", "translation"}
)
FOLLOW_PAYLOAD_FIELDS = frozenset({"target_handle"})
CONTENT_REQUIREMENT_ACTIONS = ("commented", "reposted")
CONTENT_REQUIREMENT_FIELDS = ("topic_tags", "mentions")
HANDLE_PATTERN = re.compile(r"@[\w\u4e00-\u9fff-]{1,64}\Z")
BILIBILI_API_EXECUTION_PATH = "bilibili_api_v2"
BILIBILI_PREFLIGHT_CONTRACT_VERSION = 1


class ActionPlanV2Error(ValueError):
    """The plan is not an executable, immutable Action Plan v2."""

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


def _json_object(value: Any, *, code: str) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ActionPlanV2Error(code) from exc
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ActionPlanV2Error(code) from exc
        if isinstance(parsed, dict):
            return parsed
    raise ActionPlanV2Error(code)


def canonical_json_bytes(value: Any) -> bytes:
    """Return the single canonical JSON representation used by Core/Worker.

    ``allow_nan=False`` is important: accepting a non-standard NaN token would
    make hashes dependent on the JSON implementation used by another service.
    """

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
    """Hash the exact stored UTF-8 rule text without normalization."""

    if not isinstance(rule_text, str) or not rule_text:
        raise ActionPlanV2Error("rule_text_required")
    return sha256_hex(rule_text)


def compute_target_hash(canonical_url: str) -> str:
    value = str(canonical_url or "").strip()
    if not value:
        raise ActionPlanV2Error("canonical_target_required")
    return sha256_hex(value)


def compute_config_hash(config: Mapping[str, Any] | None) -> str:
    return sha256_hex(canonical_json_bytes(dict(config or {})))


def compute_bilibili_api_config_hash(execution_revision: int) -> str:
    """Bind evidence to both the API contract and credential/account revision."""

    if type(execution_revision) is not int or execution_revision <= 0:
        raise ActionPlanV2Error("execution_revision_invalid")
    return compute_config_hash(
        {
            "execution_revision": execution_revision,
            "execution_path_id": BILIBILI_API_EXECUTION_PATH,
            "preflight_contract_version": BILIBILI_PREFLIGHT_CONTRACT_VERSION,
        }
    )


def compute_action_plan_hash(plan: Mapping[str, Any]) -> str:
    """Hash the whole plan except its self-referential ``plan_hash`` field."""

    canonical_plan = dict(plan)
    canonical_plan.pop("plan_hash", None)
    return hashlib.sha256(canonical_json_bytes(canonical_plan)).hexdigest()


def _required_string(plan: Mapping[str, Any], field: str) -> str:
    value = plan.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ActionPlanV2Error(f"action_plan_{field}_invalid")
    # Bind exactly what was reviewed. Leading/trailing whitespace in identity
    # fields creates visually identical but byte-distinct contracts.
    if value != value.strip():
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


def _validate_action_payload(action: str, value: Any) -> dict[str, Any]:
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

    unknown = set(payload) - ({"text"} | OPTIONAL_TEXT_METADATA)
    if unknown:
        raise ActionPlanV2Error("action_payload_unknown_field")
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ActionPlanV2Error(f"action_payload_{action}_text_required")
    # Whitespace is meaningful in an operator-reviewed exact payload.  Do not
    # trim, synthesize, translate or otherwise rewrite it at execution time.
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
        # Translation is executable only as part of the exact reviewed text.
        # A descriptive bool/object would be silently ignored by the adapter
        # and therefore cannot satisfy an eligibility requirement.
        if not isinstance(translation, str) or not translation.strip():
            raise ActionPlanV2Error("action_payload_translation_invalid")
        if translation not in text:
            raise ActionPlanV2Error("action_payload_translation_missing")
    return payload


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
    if any(not HANDLE_PATTERN.fullmatch(target) for target in requirements["follow_targets"]):
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
    reject_media: bool = False,
) -> ValidatedActionPlanV2:
    plan = _json_object(value, code="action_plan_v2_invalid")
    # bool is a subclass of int, so compare both type and value.
    if type(plan.get("version")) is not int or plan.get("version") != 2:
        raise ActionPlanV2Error("action_plan_version_unsupported")
    if plan.get("executable") is not True:
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

    rule_snapshot_id = plan.get("rule_snapshot_id")
    if type(rule_snapshot_id) is not int or rule_snapshot_id <= 0:
        raise ActionPlanV2Error("action_plan_rule_snapshot_id_invalid")
    rule_hash = _required_string(plan, "rule_hash")
    execution_path_id = _required_string(plan, "execution_path_id")
    plan_hash = _required_string(plan, "plan_hash")
    if len(rule_hash) != 64 or any(ch not in "0123456789abcdef" for ch in rule_hash):
        raise ActionPlanV2Error("action_plan_rule_hash_invalid")
    if len(plan_hash) != 64 or any(ch not in "0123456789abcdef" for ch in plan_hash):
        raise ActionPlanV2Error("action_plan_hash_invalid")

    raw_actions = plan.get("required_actions")
    if not isinstance(raw_actions, list) or not raw_actions:
        raise ActionPlanV2Error("action_plan_required_actions_invalid")
    actions: list[str] = []
    for raw_action in raw_actions:
        if not isinstance(raw_action, str):
            raise ActionPlanV2Error("action_plan_required_actions_invalid")
        action = raw_action.strip().lower()
        if action != raw_action or action not in ACTION_SET or action in actions:
            raise ActionPlanV2Error("action_plan_required_actions_invalid")
        actions.append(action)
    normalized_actions = tuple(action for action in ACTION_ORDER if action in set(actions))
    if tuple(actions) != normalized_actions:
        raise ActionPlanV2Error("action_plan_action_order_invalid")

    # Verify the immutable envelope before interpreting nested semantics.  A
    # tampered queue message must consistently be classified as a hash failure,
    # even if the changed text also violates a topic/mention constraint.
    computed_hash = compute_action_plan_hash(plan)
    if computed_hash != plan_hash:
        raise ActionPlanV2Error("action_plan_hash_mismatch")

    raw_payloads = plan.get("action_payloads")
    if not isinstance(raw_payloads, Mapping):
        raise ActionPlanV2Error("action_plan_payloads_invalid")
    if set(raw_payloads) != set(actions):
        raise ActionPlanV2Error("action_plan_payload_binding_mismatch")
    payloads = {
        action: _validate_action_payload(action, raw_payloads[action]) for action in actions
    }
    content_requirements = _validated_content_requirements(
        plan.get("content_requirements")
    )
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
    if reject_media:
        for payload in payloads.values():
            if payload.get("media_refs"):
                raise ActionPlanV2Error("action_payload_media_unsupported")

    return ValidatedActionPlanV2(
        plan=plan,
        plan_hash=plan_hash,
        rule_snapshot_id=rule_snapshot_id,
        rule_hash=rule_hash,
        execution_path_id=execution_path_id,
        required_actions=normalized_actions,
        action_payloads=payloads,
        content_requirements=content_requirements,
    )
