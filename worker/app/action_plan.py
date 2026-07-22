"""Canonical Action Plan v2 contract shared conceptually with Worker.

The queue message is untrusted. Worker keeps an independent validator so a
message cannot weaken Core's immutable, hash-addressed contract; cross-service
test vectors pin the canonical representation.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Mapping


LEGACY_ACTION_ORDER = ("followed", "liked", "commented", "reposted")
# Preserve the relative order of every existing action while adding collection
# before repost.  The global order is used for deterministic presentation and
# hashes; platform validators still enforce their own supported subset.
ACTION_ORDER = ("followed", "liked", "commented", "favorited", "reposted")
ACTION_SET = frozenset(ACTION_ORDER)
BILIBILI_ACTION_ORDER = LEGACY_ACTION_ORDER
WEIBO_ACTION_ORDER = ACTION_ORDER
WEIBO_MAX_UNIQUE_HANDLES = 32
XIAOHONGSHU_ACTION_ORDER = ("followed", "liked", "commented", "favorited")
DOUYIN_ACTION_ORDER = ACTION_ORDER
# Compatibility alias for worker modules/tests created before the shared name.
XIAOHONGSHU_REQUIRED_ACTIONS = XIAOHONGSHU_ACTION_ORDER
PLATFORM_ACTION_ORDERS = {
    "bilibili": BILIBILI_ACTION_ORDER,
    "weibo": WEIBO_ACTION_ORDER,
    "xiaohongshu": XIAOHONGSHU_ACTION_ORDER,
    "douyin": DOUYIN_ACTION_ORDER,
}
TEXT_ACTIONS = frozenset({"commented", "reposted"})
OPTIONAL_TEXT_METADATA = frozenset(
    {"topic_tags", "mentions", "media_refs", "translation"}
)
FOLLOW_PAYLOAD_FIELDS = frozenset({"target_handle"})
CONTENT_REQUIREMENT_ACTIONS = ("commented", "reposted")
CONTENT_REQUIREMENT_FIELDS = ("topic_tags", "mentions")
FRIEND_MENTION_MODES = frozenset({"minimum", "exact"})
HANDLE_PATTERN = re.compile(r"@[\w\u4e00-\u9fff-]{1,64}\Z")
# Extract the complete handle token rather than accepting substring prefixes
# (for example, reviewed ``@alice`` must not be satisfied by ``@alice2``).
MENTION_IN_TEXT_PATTERN = re.compile(
    r"@[\w\u4e00-\u9fff-]{1,64}(?![\w\u4e00-\u9fff-])"
)
BILIBILI_API_EXECUTION_PATH = "bilibili_api_v2"
BILIBILI_API_PREFLIGHT_CONTRACT_VERSION = 1
BILIBILI_PREFLIGHT_CONTRACT_VERSION = BILIBILI_API_PREFLIGHT_CONTRACT_VERSION
WEIBO_OAUTH_EXECUTION_PATH = "weibo_oauth_v1"
WEIBO_MANUAL_EXECUTION_PATH = "weibo_manual_v1"
WEIBO_MANUAL_EXECUTION_BLOCKER = "weibo_manual_execution_selected"
WEIBO_OAUTH_CAPABILITY_CONTRACT_VERSION = 1
WEIBO_ACTION_CAPABILITY_REQUIREMENTS = {
    "followed": {
        "endpoint": "friendships/create",
        "permission": "advanced",
        "client_type": "weibo",
    },
    "liked": {"endpoint": "attitudes/create", "permission": "advanced"},
    "commented": {"endpoint": "comments/create", "permission": "standard"},
    "favorited": {"endpoint": "favorites/create", "permission": "standard"},
    "reposted": {"endpoint": "statuses/repost", "permission": "standard"},
}
WEIBO_RIP_ACTIONS = frozenset({"followed", "commented", "reposted"})
XIAOHONGSHU_MANUAL_EXECUTION_PATH = "xiaohongshu_manual_v1"
XIAOHONGSHU_NO_OFFICIAL_API_BLOCKER = (
    "xiaohongshu_no_official_interaction_api"
)
DOUYIN_MANUAL_EXECUTION_PATH = "douyin_manual_v1"
DOUYIN_NO_OFFICIAL_API_BLOCKER = "douyin_no_official_interaction_api"


class ActionPlanV2Error(ValueError):
    """The plan cannot safely authorize an external mutation."""

    def __init__(self, code: str) -> None:
        self.code = str(code or "action_plan_v2_invalid")
        super().__init__(self.code)


def _json_object(value: Any, *, code: str) -> dict[str, Any]:
    """Decode JSON database/stream values without weakening object checks."""

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
    source_content_requirements: dict[str, Any]
    friend_mention_requirements: dict[str, dict[str, Any]]
    runtime_capability_requirements: dict[str, Any]

    def payload_for(self, action: str) -> dict[str, Any]:
        return dict(self.action_payloads.get(action, {}))

    @property
    def follow_target_handle(self) -> str:
        return str(self.action_payloads.get("followed", {}).get("target_handle") or "")


def action_order_for_platform(platform: str) -> tuple[str, ...]:
    """Return the canonical action subset for a platform.

    Weibo and Douyin support collection as a distinct requirement; it is never
    interchangeable with repost/share. Unknown platforms retain the legacy
    four-action subset for backward compatibility.
    """

    key = str(platform or "").strip().casefold()
    return PLATFORM_ACTION_ORDERS.get(key, LEGACY_ACTION_ORDER)


def default_execution_path_for_platform(platform: str) -> str:
    key = str(platform or "").strip().casefold()
    if key == "weibo":
        return WEIBO_OAUTH_EXECUTION_PATH
    if key == "xiaohongshu":
        return XIAOHONGSHU_MANUAL_EXECUTION_PATH
    if key == "douyin":
        return DOUYIN_MANUAL_EXECUTION_PATH
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
        return encoded.encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ActionPlanV2Error("action_plan_not_canonicalizable") from exc


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


def weibo_runtime_capability_requirements(
    required_actions: list[str] | tuple[str, ...],
) -> dict[str, Any]:
    """Return the versioned, non-secret OAuth capability contract for a plan."""

    selected = set(required_actions)
    return {
        "contract_version": WEIBO_OAUTH_CAPABILITY_CONTRACT_VERSION,
        "actions": {
            action: dict(WEIBO_ACTION_CAPABILITY_REQUIREMENTS[action])
            for action in WEIBO_ACTION_ORDER
            if action in selected
        },
    }


def _required_string(plan: Mapping[str, Any], field: str) -> str:
    value = plan.get(field)
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ActionPlanV2Error(f"action_plan_{field}_invalid")
    return value


def _utf8_length(value: str, *, error_code: str) -> int:
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ActionPlanV2Error(error_code) from exc


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
        ):
            raise ActionPlanV2Error(f"action_payload_{field}_invalid")
        if _utf8_length(
            item,
            error_code=f"action_payload_{field}_invalid",
        ) > 512:
            raise ActionPlanV2Error(f"action_payload_{field}_invalid")
        if item in result or (
            field == "mentions" and not HANDLE_PATTERN.fullmatch(item)
        ):
            raise ActionPlanV2Error(f"action_payload_{field}_invalid")
        result.append(item)
    return result


def validate_action_payload(
    action: str,
    value: Any,
    *,
    allow_empty_repost_text: bool = False,
    platform: str | None = None,
) -> dict[str, Any]:
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
        ):
            raise ActionPlanV2Error("action_payload_followed_target_invalid")
        if _utf8_length(
            target_handle,
            error_code="action_payload_followed_target_invalid",
        ) > 512:
            raise ActionPlanV2Error("action_payload_followed_target_invalid")
        return payload
    if action not in TEXT_ACTIONS:
        if payload:
            raise ActionPlanV2Error("non_text_action_payload_not_empty")
        return payload

    if set(payload) - ({"text"} | OPTIONAL_TEXT_METADATA):
        raise ActionPlanV2Error("action_payload_unknown_field")
    if action == "reposted" and allow_empty_repost_text and not payload:
        # Douyin and Weibo activities may require a plain repost/share without
        # accompanying text. Preserve the exact source requirement instead of
        # forcing the operator to invent a stricter payload.
        return {}
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ActionPlanV2Error(f"action_payload_{action}_text_required")
    if _utf8_length(
        text,
        error_code=f"action_payload_{action}_text_invalid",
    ) > 4096:
        raise ActionPlanV2Error(f"action_payload_{action}_text_too_large")
    if str(platform or "").strip().casefold() == "weibo":
        # Count UTF-16 code units conservatively: a non-BMP emoji consumes two.
        # Invalid surrogate text is rejected rather than leaking an encoder
        # failure across the queue boundary or normalizing reviewed content.
        try:
            utf16_units = len(text.encode("utf-16-le")) // 2
        except UnicodeEncodeError as exc:
            raise ActionPlanV2Error(
                f"action_payload_{action}_text_invalid"
            ) from exc
        if utf16_units > 140:
            raise ActionPlanV2Error(f"weibo_{action}_text_too_long")

    topic_tags = _validated_metadata_list(payload, "topic_tags")
    mentions = _validated_metadata_list(payload, "mentions")
    _validated_metadata_list(payload, "media_refs")
    for required_token in topic_tags:
        if required_token not in text:
            raise ActionPlanV2Error("action_payload_required_token_missing")
    observed_mentions = set(MENTION_IN_TEXT_PATTERN.findall(text))
    for required_mention in mentions:
        if required_mention not in observed_mentions:
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
    allow_empty_repost_text: bool = False,
    platform: str | None = None,
) -> dict[str, dict[str, Any]]:
    raw = dict(value or {})
    if set(raw) - allowed_actions:
        raise ActionPlanV2Error("action_plan_payload_unknown_action")
    if set(raw) != set(required_actions):
        raise ActionPlanV2Error("action_plan_payload_binding_mismatch")
    result: dict[str, dict[str, Any]] = {}
    for action in required_actions:
        result[action] = validate_action_payload(
            action,
            raw.get(action, {}),
            allow_empty_repost_text=allow_empty_repost_text,
            platform=platform,
        )
    return result


def semantic_requirement_status(
    unsupported_actions: list[str],
    action_payloads: Mapping[str, Mapping[str, Any]],
    content_requirements: Mapping[str, Any] | None = None,
    *,
    friend_mention_requirements: Mapping[str, Any] | None = None,
    source_content_requirements: Mapping[str, Any] | None = None,
    media_capability_blocker: str = "bilibili_media_submission_unsupported",
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
        elif requirement == "mention_friends":
            resolved = friend_mention_requirements_satisfied(
                action_payloads,
                exact_requirements,
                friend_mention_requirements,
                source_content_requirements=source_content_requirements,
            )
        elif requirement == "media_submission":
            resolved = any(payload.get("media_refs") for payload in payload_values)
            if resolved:
                capability_blockers.append(media_capability_blocker)
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
        capability_blockers.append(media_capability_blocker)
    return represented, unresolved, list(dict.fromkeys(capability_blockers))


def bind_manual_follow_target(
    required_actions: list[str] | tuple[str, ...],
    action_payloads: Mapping[str, Mapping[str, Any]],
    content_requirements: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind an implicit author-follow rule to the reviewed exact handle.

    Manual-only rules commonly say ``关注博主`` without repeating the author's
    handle. The contract may use the operator-reviewed ``target_handle`` in
    that narrow case. Explicit source handles are never replaced, so
    ambiguous/multiple targets still fail closed in the semantic validator.
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


def validate_friend_mention_requirements(value: Any) -> dict[str, dict[str, Any]]:
    """Validate optional action-scoped friend counts stored in a v2 plan."""

    if value is None:
        return {}
    if not isinstance(value, Mapping) or set(value) - set(CONTENT_REQUIREMENT_ACTIONS):
        raise ActionPlanV2Error("action_plan_friend_mention_requirements_invalid")
    result: dict[str, dict[str, Any]] = {}
    for action in CONTENT_REQUIREMENT_ACTIONS:
        if action not in value:
            continue
        constraint = value[action]
        if not isinstance(constraint, Mapping) or set(constraint) != {"mode", "count"}:
            raise ActionPlanV2Error("action_plan_friend_mention_requirements_invalid")
        mode = constraint.get("mode")
        count = constraint.get("count")
        if mode not in FRIEND_MENTION_MODES or type(count) is not int or not 1 <= count <= 32:
            raise ActionPlanV2Error("action_plan_friend_mention_requirements_invalid")
        result[action] = {"mode": mode, "count": count}
    return result


def _friend_count_satisfied(constraint: Mapping[str, Any], actual: int) -> bool:
    count = int(constraint["count"])
    return actual == count if constraint["mode"] == "exact" else actual >= count


def _mention_identity_key(value: str) -> str:
    """Canonical comparison key for a reviewed ``@handle`` identity."""

    return unicodedata.normalize("NFKC", value).casefold()


def _mention_identity_keys(values: list[str]) -> list[str]:
    return [_mention_identity_key(value) for value in values]


def _has_duplicate_mention_identities(values: list[str]) -> bool:
    keys = _mention_identity_keys(values)
    return len(keys) != len(set(keys))


def bind_manual_friend_mentions(
    action_payloads: Mapping[str, Mapping[str, Any]],
    content_requirements: Mapping[str, Any],
    friend_mention_requirements: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Bind reviewed concrete friend handles to their source-scoped action.

    Generic source text such as ``评论@3位好友`` names a count, not identities.
    Concrete handles therefore come only from the reviewed payload. They are
    copied into the corresponding content-requirement bucket only when the
    source's exact/minimum count is met; otherwise the requirement remains
    unresolved and the resulting plan cannot become shadow-ready.
    """

    requirements = {
        "follow_targets": list(content_requirements.get("follow_targets") or []),
        "commented": dict(content_requirements.get("commented") or {}),
        "reposted": dict(content_requirements.get("reposted") or {}),
    }
    constraints = validate_friend_mention_requirements(friend_mention_requirements)
    follow_target_keys = set(_mention_identity_keys(requirements["follow_targets"]))
    for action, constraint in constraints.items():
        if action not in action_payloads:
            continue
        source_mentions = list(requirements[action].get("mentions") or [])
        payload_mentions = action_payloads.get(action, {}).get("mentions", [])
        if not isinstance(payload_mentions, list):
            continue
        payload_keys = _mention_identity_keys(payload_mentions)
        source_keys = set(_mention_identity_keys(source_mentions))
        if (
            len(payload_keys) != len(set(payload_keys))
            or not source_keys.issubset(payload_keys)
        ):
            continue
        friend_handles = [
            mention
            for mention, identity_key in zip(payload_mentions, payload_keys)
            if identity_key not in source_keys
            and identity_key not in follow_target_keys
        ]
        if _friend_count_satisfied(constraint, len(friend_handles)):
            requirements[action]["mentions"] = list(payload_mentions)
    return requirements


def friend_mention_requirements_satisfied(
    action_payloads: Mapping[str, Mapping[str, Any]],
    bound_content_requirements: Mapping[str, Any],
    friend_mention_requirements: Mapping[str, Any] | None,
    *,
    source_content_requirements: Mapping[str, Any] | None = None,
) -> bool:
    """Check action, count and exact handle binding for friend mentions."""

    try:
        constraints = validate_friend_mention_requirements(
            friend_mention_requirements
        )
    except ActionPlanV2Error:
        return False
    if not constraints:
        return False
    source = dict(source_content_requirements or {})
    follow_target_keys = set(
        _mention_identity_keys(list(source.get("follow_targets") or []))
    ) | set(
        _mention_identity_keys(
            list(bound_content_requirements.get("follow_targets") or [])
        )
    )
    for action, constraint in constraints.items():
        payload = action_payloads.get(action)
        if not isinstance(payload, Mapping):
            return False
        payload_mentions = payload.get("mentions", [])
        bound_action = bound_content_requirements.get(action, {})
        bound_mentions = (
            bound_action.get("mentions", [])
            if isinstance(bound_action, Mapping)
            else []
        )
        source_action = source.get(action, {})
        source_mentions = (
            source_action.get("mentions", [])
            if isinstance(source_action, Mapping)
            else []
        )
        if not isinstance(payload_mentions, list):
            return False
        payload_keys = _mention_identity_keys(payload_mentions)
        source_keys = set(_mention_identity_keys(list(source_mentions)))
        if (
            bound_mentions != payload_mentions
            or len(payload_keys) != len(set(payload_keys))
            or not source_keys.issubset(payload_keys)
        ):
            return False
        friend_identity_keys = [
            identity_key
            for identity_key in payload_keys
            if identity_key not in source_keys
            and identity_key not in follow_target_keys
        ]
        if not _friend_count_satisfied(constraint, len(friend_identity_keys)):
            return False
    return True


def bind_xiaohongshu_manual_follow_target(
    required_actions: list[str] | tuple[str, ...],
    action_payloads: Mapping[str, Mapping[str, Any]],
    content_requirements: Mapping[str, Any],
) -> dict[str, Any]:
    """Backward-compatible Xiaohongshu wrapper for the shared manual binder."""

    return bind_manual_follow_target(
        required_actions,
        action_payloads,
        content_requirements,
    )


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
    plan = _json_object(value, code="action_plan_v2_invalid")
    if type(plan.get("version")) is not int or plan.get("version") != 2:
        raise ActionPlanV2Error("action_plan_version_unsupported")
    raw_platform = plan.get("platform")
    platform_key = (
        raw_platform.strip().casefold()
        if isinstance(raw_platform, str)
        else ""
    )
    is_weibo = platform_key == "weibo"
    is_xiaohongshu = platform_key == "xiaohongshu"
    is_douyin = platform_key == "douyin"
    manual_execution_paths = {
        "xiaohongshu": XIAOHONGSHU_MANUAL_EXECUTION_PATH,
        "douyin": DOUYIN_MANUAL_EXECUTION_PATH,
    }
    # Manual-only platforms have no proven official participant-interaction
    # write API. Even a non-executable validator must reject a forged
    # executable claim before generic validation can treat it as authoritative.
    if platform_key in manual_execution_paths and plan.get("executable") is not False:
        raise ActionPlanV2Error(
            f"{platform_key}_manual_plan_must_be_non_executable"
        )
    if (
        is_weibo
        and plan.get("execution_path_id") == WEIBO_MANUAL_EXECUTION_PATH
        and plan.get("executable") is not False
    ):
        raise ActionPlanV2Error("weibo_manual_plan_must_be_non_executable")
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
    expected_manual_path = manual_execution_paths.get(platform_key)
    if expected_manual_path and path_id != expected_manual_path:
        raise ActionPlanV2Error(
            "xiaohongshu_execution_path_not_supported"
            if is_xiaohongshu
            else f"{platform_key}_execution_path_invalid"
        )
    if is_weibo and path_id not in {
        WEIBO_OAUTH_EXECUTION_PATH,
        WEIBO_MANUAL_EXECUTION_PATH,
    }:
        raise ActionPlanV2Error("weibo_execution_path_invalid")
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
    deferred_canonicalization_error: ActionPlanV2Error | None = None
    try:
        computed_plan_hash = compute_action_plan_hash(plan)
    except ActionPlanV2Error as exc:
        if exc.code != "action_plan_not_canonicalizable":
            raise
        # Defer only encoding failures so payload validators can return the
        # precise action/metadata/follow field code for lone surrogates.
        deferred_canonicalization_error = exc
    else:
        if computed_plan_hash != plan_hash:
            raise ActionPlanV2Error("action_plan_hash_mismatch")
    payloads = normalize_action_payloads(
        actions,
        plan.get("action_payloads"),
        allowed_actions=platform_action_set,
        allow_empty_repost_text=is_weibo or is_douyin,
        platform=platform_key,
    )
    content_requirements = _validated_content_requirements(
        plan.get("content_requirements")
    )
    friend_mention_requirements = validate_friend_mention_requirements(
        plan.get("friend_mention_requirements", {})
    )
    if set(friend_mention_requirements) - set(actions):
        raise ActionPlanV2Error("action_plan_friend_mention_action_missing")
    raw_source_content_requirements = plan.get("source_content_requirements")
    if raw_source_content_requirements is None:
        # Legacy plans remain compatible only when no operator-supplied friend
        # handles need to be distinguished from tokens present in source text.
        if friend_mention_requirements:
            raise ActionPlanV2Error(
                "action_plan_friend_mention_requirement_binding_mismatch"
            )
        source_content_requirements = content_requirements
    else:
        source_content_requirements = _validated_content_requirements(
            raw_source_content_requirements
        )
    if is_weibo:
        weibo_handles = [
            *content_requirements["follow_targets"],
            *source_content_requirements["follow_targets"],
        ]
        followed_payload = payloads.get("followed", {})
        if isinstance(followed_payload.get("target_handle"), str):
            weibo_handles.append(followed_payload["target_handle"])
        for action in CONTENT_REQUIREMENT_ACTIONS:
            weibo_handles.extend(content_requirements[action]["mentions"])
            weibo_handles.extend(source_content_requirements[action]["mentions"])
            weibo_handles.extend(payloads.get(action, {}).get("mentions") or [])
        if len(set(_mention_identity_keys(weibo_handles))) > WEIBO_MAX_UNIQUE_HANDLES:
            raise ActionPlanV2Error(
                "weibo_preflight_unique_handle_limit_exceeded"
            )
    source_follow_target_keys = set(
        _mention_identity_keys(source_content_requirements["follow_targets"])
    )
    bound_follow_target_keys = set(
        _mention_identity_keys(content_requirements["follow_targets"])
    )
    if not source_follow_target_keys.issubset(bound_follow_target_keys):
        raise ActionPlanV2Error(
            "action_plan_friend_mention_requirement_binding_mismatch"
        )
    for action in CONTENT_REQUIREMENT_ACTIONS:
        source_action = source_content_requirements[action]
        bound_action = content_requirements[action]
        if source_action["topic_tags"] != bound_action["topic_tags"]:
            raise ActionPlanV2Error(
                "action_plan_friend_mention_requirement_binding_mismatch"
            )
        if action in friend_mention_requirements:
            source_mention_keys = set(
                _mention_identity_keys(source_action["mentions"])
            )
            bound_mention_keys = _mention_identity_keys(bound_action["mentions"])
            if (
                len(bound_mention_keys) != len(set(bound_mention_keys))
                or not source_mention_keys.issubset(bound_mention_keys)
            ):
                raise ActionPlanV2Error(
                    "action_plan_friend_mention_requirement_binding_mismatch"
                )
        elif source_action["mentions"] != bound_action["mentions"]:
            raise ActionPlanV2Error(
                "action_plan_friend_mention_requirement_binding_mismatch"
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
    if friend_mention_requirements and not friend_mention_requirements_satisfied(
        payloads,
        content_requirements,
        friend_mention_requirements,
        source_content_requirements=source_content_requirements,
    ):
        raise ActionPlanV2Error("action_plan_friend_mention_count_mismatch")
    if reject_media and any(payload.get("media_refs") for payload in payloads.values()):
        raise ActionPlanV2Error("action_payload_media_unsupported")
    if deferred_canonicalization_error is not None:
        raise deferred_canonicalization_error
    runtime_capability_requirements = plan.get(
        "runtime_capability_requirements", {}
    )
    if runtime_capability_requirements is None:
        runtime_capability_requirements = {}
    if not isinstance(runtime_capability_requirements, Mapping):
        raise ActionPlanV2Error("weibo_oauth_capability_contract_mismatch")
    runtime_capability_requirements = dict(runtime_capability_requirements)
    if is_weibo:
        expected_capabilities = (
            weibo_runtime_capability_requirements(actions)
            if path_id == WEIBO_OAUTH_EXECUTION_PATH
            else {}
        )
        if runtime_capability_requirements != expected_capabilities:
            raise ActionPlanV2Error("weibo_oauth_capability_contract_mismatch")
    return ValidatedActionPlanV2(
        plan=plan,
        plan_hash=plan_hash,
        rule_snapshot_id=snapshot_id,
        rule_hash=rule_hash,
        execution_path_id=path_id,
        required_actions=normalized,
        action_payloads=payloads,
        content_requirements=content_requirements,
        source_content_requirements=source_content_requirements,
        friend_mention_requirements=friend_mention_requirements,
        runtime_capability_requirements=runtime_capability_requirements,
    )
