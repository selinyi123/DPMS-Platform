"""Read-only, hash-addressed preflight for the ``bilibili_api_v2`` path.

The preflight deliberately uses only GET-based client methods.  It proves that
the authenticated account can read the exact reviewed dynamic and that the API
card contains every identifier needed by the reviewed actions.  It never calls
the mutation methods used by :class:`BilibiliApiExecutor`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from app.action_plan import (
    ACTION_ORDER,
    BILIBILI_API_EXECUTION_PATH,
    BILIBILI_PREFLIGHT_CONTRACT_VERSION,
    HANDLE_PATTERN,
    canonical_json_bytes,
    compute_bilibili_api_config_hash,
    sha256_hex,
)
from app.bilibili.client import BilibiliApiClient
from app.bilibili.config import BiliEngineConfig
from app.bilibili.runtime import (
    DPMS_TO_API_ACTION,
    dpms_phases_to_api_actions,
    parse_detail_card,
    validate_card_for_actions,
)


API_PREFLIGHT_KIND = "bilibili_api_readonly_v2"
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


@dataclass(frozen=True)
class ApiPreflightEvidence:
    observation: dict[str, Any]
    observation_hash: str


def hash_preflight_observation(observation: dict[str, Any]) -> str:
    """Hash the complete canonical observation; no mutable hash field exists."""

    return sha256_hex(canonical_json_bytes(observation))


def bilibili_author_handle(uname: Any) -> str:
    """Return the exact Action Plan handle for one API author name."""

    value = str(uname or "").strip()
    if not value or value.startswith("@"):
        raise ValueError("bilibili_api_preflight_author_handle_invalid")
    handle = f"@{value}"
    if not HANDLE_PATTERN.fullmatch(handle):
        raise ValueError("bilibili_api_preflight_author_handle_invalid")
    return handle


def validate_preflight_observation(
    value: Any,
    *,
    expected_dynamic_id: str,
    expected_actions: tuple[str, ...] | list[str],
    expected_execution_revision: int,
    expected_config_hash: str,
    expected_follow_handle: str = "",
) -> ApiPreflightEvidence:
    if not isinstance(value, Mapping):
        raise ValueError("bilibili_api_preflight_observation_invalid")
    observation = dict(value)
    if set(observation) != OBSERVATION_FIELDS:
        raise ValueError("bilibili_api_preflight_observation_schema_invalid")

    expected_action_list = list(expected_actions)
    if (
        not expected_action_list
        or expected_action_list
        != [action for action in ACTION_ORDER if action in set(expected_action_list)]
        or any(action not in DPMS_TO_API_ACTION for action in expected_action_list)
    ):
        raise ValueError("bilibili_api_preflight_expected_actions_invalid")
    expected_api_actions = [
        DPMS_TO_API_ACTION[action] for action in expected_action_list
    ]
    capability_checks = observation.get("capability_checks")
    if (
        type(observation.get("version")) is not int
        or observation.get("version") != 1
        or observation.get("probe_kind") != API_PREFLIGHT_KIND
        or observation.get("execution_path_id") != BILIBILI_API_EXECUTION_PATH
        or type(observation.get("preflight_contract_version")) is not int
        or observation.get("preflight_contract_version")
        != BILIBILI_PREFLIGHT_CONTRACT_VERSION
        or observation.get("side_effects") is not False
        or observation.get("account_authenticated") is not True
        or observation.get("api_preflight_complete") is not True
        or observation.get("required_actions") != expected_action_list
        or observation.get("api_actions") != expected_api_actions
        or not isinstance(capability_checks, Mapping)
        or set(capability_checks) != set(expected_action_list)
        or any(
            capability_checks.get(action) is not True
            for action in expected_action_list
        )
    ):
        raise ValueError("bilibili_api_preflight_observation_invalid")

    dynamic_id = str(expected_dynamic_id or "").strip()
    if (
        not dynamic_id
        or observation.get("requested_dynamic_id") != dynamic_id
        or observation.get("observed_dynamic_id") != dynamic_id
    ):
        raise ValueError("bilibili_api_preflight_target_mismatch")

    execution_revision = observation.get("execution_revision")
    if type(execution_revision) is not int or execution_revision <= 0:
        raise ValueError("bilibili_api_preflight_execution_revision_invalid")
    if execution_revision != expected_execution_revision:
        raise ValueError("bilibili_api_preflight_execution_revision_mismatch")
    config_hash = compute_bilibili_api_config_hash(execution_revision)
    if observation.get("config_hash") != config_hash or config_hash != expected_config_hash:
        raise ValueError("bilibili_api_preflight_config_hash_mismatch")

    target_type = observation.get("target_type")
    if type(target_type) is not int or target_type <= 0:
        raise ValueError("bilibili_api_preflight_target_invalid")
    if target_type == 1:
        raise ValueError("bilibili_forwarded_origin_requires_review")
    target_uid = observation.get("target_uid")
    if type(target_uid) is not int or target_uid <= 0:
        raise ValueError("bilibili_api_preflight_target_invalid")
    target_identity = observation.get("target_identity")
    if (
        not isinstance(target_identity, Mapping)
        or set(target_identity) != TARGET_IDENTITY_FIELDS
    ):
        raise ValueError("bilibili_api_preflight_target_identity_invalid")
    author_handle = target_identity.get("author_handle")
    if (
        target_identity.get("verified") is not True
        or target_identity.get("dynamic_id") != dynamic_id
        or type(target_identity.get("author_uid")) is not int
        or target_identity.get("author_uid") <= 0
        or target_identity.get("author_uid") != target_uid
        or not isinstance(author_handle, str)
        or not HANDLE_PATTERN.fullmatch(author_handle)
        or observation.get("author_handle") != author_handle
    ):
        raise ValueError("bilibili_api_preflight_target_identity_invalid")

    if "followed" in expected_action_list:
        if (
            not expected_follow_handle
            or author_handle != expected_follow_handle
            or observation.get("follow_target_handle") != expected_follow_handle
        ):
            raise ValueError("bilibili_api_preflight_follow_target_mismatch")
    elif observation.get("follow_target_handle") not in {"", None}:
        raise ValueError("bilibili_api_preflight_unexpected_follow_target")

    if "commented" in expected_action_list:
        if (
            not str(observation.get("comment_rid_str") or "").strip()
            or type(observation.get("comment_type")) is not int
            or observation.get("comment_type") <= 0
        ):
            raise ValueError("bilibili_api_preflight_comment_target_invalid")
    return ApiPreflightEvidence(
        observation=observation,
        observation_hash=hash_preflight_observation(observation),
    )


async def run_readonly_api_preflight(
    *,
    cookie_header: str,
    dynamic_id: str,
    required_actions: tuple[str, ...] | list[str],
    execution_revision: int,
    config_hash: str,
    expected_follow_handle: str | None = None,
    client_factory: Callable[..., Any] = BilibiliApiClient,
) -> ApiPreflightEvidence:
    """Run login/detail GETs only and return immutable, non-secret evidence."""

    phases = list(required_actions)
    if compute_bilibili_api_config_hash(execution_revision) != config_hash:
        raise ValueError("bilibili_api_preflight_config_hash_mismatch")
    api_actions = dpms_phases_to_api_actions(phases)
    async with client_factory(cookie_header, config=BiliEngineConfig()) as client:
        if not await client.check_login():
            raise RuntimeError("bilibili_api_preflight_login_required")
        detail = await client.get_dynamic_detail(dynamic_id)
        if not isinstance(detail, dict) or int(detail.get("code", -1)) != 0:
            raise RuntimeError("bilibili_api_preflight_detail_failed")
        card = parse_detail_card(detail, dynamic_id)
        # The real path rejects forwarded wrappers because the reviewed target
        # does not bind the origin identifiers.  Preflight must prove the same
        # path, not a more permissive substitute.
        if card.type == 1:
            raise RuntimeError("bilibili_forwarded_origin_requires_review")
        validate_card_for_actions(card, api_actions)

    author_handle = bilibili_author_handle(card.uname)
    if "followed" in phases:
        if not expected_follow_handle or author_handle != expected_follow_handle:
            raise RuntimeError("bilibili_api_preflight_follow_target_mismatch")
    elif expected_follow_handle:
        raise RuntimeError("bilibili_api_preflight_unexpected_follow_target")

    checks = {
        phase: (
            (phase != "followed" or bool(card.uid))
            and (phase not in {"liked", "reposted"} or bool(card.dynamic_id))
            and (
                phase != "commented"
                or bool(card.rid_str and card.chat_type)
            )
        )
        for phase in phases
    }
    observation = {
        "version": 1,
        "probe_kind": API_PREFLIGHT_KIND,
        "execution_path_id": BILIBILI_API_EXECUTION_PATH,
        "preflight_contract_version": BILIBILI_PREFLIGHT_CONTRACT_VERSION,
        "execution_revision": execution_revision,
        "config_hash": config_hash,
        "side_effects": False,
        "account_authenticated": True,
        "api_preflight_complete": all(checks.values()),
        "requested_dynamic_id": str(dynamic_id),
        "observed_dynamic_id": str(card.dynamic_id),
        "target_type": int(card.type),
        "target_uid": int(card.uid or 0),
        "author_handle": author_handle,
        "follow_target_handle": expected_follow_handle or "",
        "target_identity": {
            "verified": True,
            "dynamic_id": str(card.dynamic_id),
            "author_uid": int(card.uid or 0),
            "author_handle": author_handle,
        },
        "comment_rid_str": str(card.rid_str or ""),
        "comment_type": int(card.chat_type or 0),
        "required_actions": phases,
        "api_actions": api_actions,
        "capability_checks": checks,
    }
    return validate_preflight_observation(
        observation,
        expected_dynamic_id=dynamic_id,
        expected_actions=phases,
        expected_execution_revision=execution_revision,
        expected_config_hash=config_hash,
        expected_follow_handle=expected_follow_handle,
    )
