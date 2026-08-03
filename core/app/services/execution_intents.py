"""Durable, hash-bound execution intents for safe missing-action repair.

The lottery row is mutable review state.  It must never be the sole source of
truth for deciding which external actions a repair task may execute.  A first
real-run therefore freezes the complete reviewed business intent, and every
task receives a second binding for its exact requested action subset and
account/evidence/lease generation.

The Worker independently reconstructs and validates the persisted/message
contract. Core additionally fences rolling deployments on the live Worker
capability heartbeat before dispatching a repair.
"""

from __future__ import annotations

import copy
import json
import re
import uuid
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from app.action_plan import (
    ActionPlanV2Error,
    canonical_json_bytes,
    compute_action_plan_hash,
    compute_target_hash,
    sha256_hex,
    validate_action_plan_v2,
)
from app.platform_modules import (
    PlatformModuleUnavailableError,
    get_platform_module,
)
from app.platform_modules.base import parse_stored_json
from shared.execution_contracts import (
    FULL_EXECUTION_INTENT_KIND,
    REPAIR_EXECUTION_INTENT_KIND,
)


EXECUTION_INTENT_CONTRACT_VERSION = 1
EXECUTION_INTENT_KINDS = frozenset(
    {
        FULL_EXECUTION_INTENT_KIND,
        REPAIR_EXECUTION_INTENT_KIND,
    }
)
EXECUTION_EVIDENCE_KINDS = frozenset(
    {
        "exact_execution_evidence",
        "oauth_account_calibration",
    }
)
HASH_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
MAX_TARGET_URL_LENGTH = 512


class ExecutionIntentError(ValueError):
    """A frozen intent or task binding cannot safely authorize execution."""

    def __init__(self, code: str) -> None:
        self.code = str(code or "execution_intent_invalid")
        super().__init__(self.code)


@dataclass(frozen=True)
class FrozenLotteryExecutionIntent:
    contract_version: int
    intent_id: str
    intent_hash: str
    lottery_id: int
    source_task_id: str
    source_account_id: int
    platform: str
    raw_url: str
    canonical_url: str
    full_action_plan: dict[str, Any]
    full_action_plan_hash: str
    full_required_actions: tuple[str, ...]
    full_required_actions_hash: str
    rule_snapshot_id: int
    rule_hash: str
    execution_path_id: str
    target_hash: str

    def public_metadata(self) -> dict[str, Any]:
        return {
            "execution_intent_contract_version": self.contract_version,
            "execution_intent_id": self.intent_id,
            "execution_intent_hash": self.intent_hash,
            "execution_intent_source_task_id": self.source_task_id,
            "full_action_plan_hash": self.full_action_plan_hash,
            "full_required_actions_hash": self.full_required_actions_hash,
        }


@dataclass(frozen=True)
class ExecutionIntentLoadFailure:
    """A batch-local corrupt root that must not poison unrelated lotteries."""

    lottery_id: int
    code: str


@dataclass(frozen=True)
class RequestedExecutionSubset:
    intent_id: str
    intent_hash: str
    requested_actions: tuple[str, ...]
    requested_actions_hash: str
    action_plan: dict[str, Any]
    action_plan_hash: str


@dataclass(frozen=True)
class TaskExecutionIntentBinding:
    contract_version: int
    task_id: str
    intent_id: str
    intent_hash: str
    lottery_id: int
    account_id: int
    binding_kind: str
    requested_actions: tuple[str, ...]
    requested_actions_hash: str
    bound_action_plan: dict[str, Any]
    bound_action_plan_hash: str
    evidence_action_plan_hash: str
    rule_snapshot_id: int
    rule_hash: str
    execution_evidence_id: str
    execution_evidence_kind: str
    exact_execution_evidence_id: str | None
    oauth_calibration_id: str | None
    execution_path_id: str
    target_hash: str
    config_hash: str
    execution_revision: int
    account_lease_id: str
    account_lease_generation: int
    binding_hash: str

    def message_fields(self) -> dict[str, Any]:
        return {
            "execution_intent_id": self.intent_id,
            "execution_intent_hash": self.intent_hash,
            "execution_intent_kind": self.binding_kind,
            "execution_intent_binding_hash": self.binding_hash,
            "requested_actions": list(self.requested_actions),
            "requested_actions_hash": self.requested_actions_hash,
            "requested_action_plan_hash": self.bound_action_plan_hash,
            "execution_evidence_kind": self.execution_evidence_kind,
            "exact_execution_evidence_id": (
                self.exact_execution_evidence_id or ""
            ),
            "oauth_calibration_id": self.oauth_calibration_id or "",
        }


def _require_positive_int(value: Any, code: str) -> int:
    if type(value) is not int or value <= 0:
        raise ExecutionIntentError(code)
    return value


def _require_string(
    value: Any,
    code: str,
    *,
    max_length: int | None = None,
) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or (max_length is not None and len(value) > max_length)
    ):
        raise ExecutionIntentError(code)
    return value


def _require_hash(value: Any, code: str) -> str:
    result = _require_string(value, code)
    if not HASH_PATTERN.fullmatch(result):
        raise ExecutionIntentError(code)
    return result


def _require_uuid(value: Any, code: str) -> str:
    result = _require_string(value, code)
    try:
        parsed = uuid.UUID(result)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ExecutionIntentError(code) from exc
    if str(parsed) != result:
        raise ExecutionIntentError(code)
    return result


def _required_actions_hash(actions: Sequence[str]) -> str:
    return sha256_hex(canonical_json_bytes(list(actions)))


def _intent_hash_payload(
    *,
    intent_id: str,
    lottery_id: int,
    source_task_id: str,
    source_account_id: int,
    platform: str,
    raw_url: str,
    canonical_url: str,
    full_action_plan: Mapping[str, Any],
    full_action_plan_hash: str,
    full_required_actions: Sequence[str],
    full_required_actions_hash: str,
    rule_snapshot_id: int,
    rule_hash: str,
    execution_path_id: str,
    target_hash: str,
) -> dict[str, Any]:
    return {
        "contract_version": EXECUTION_INTENT_CONTRACT_VERSION,
        "intent_id": intent_id,
        "lottery_id": lottery_id,
        "source_task_id": source_task_id,
        "source_account_id": source_account_id,
        "platform": platform,
        "raw_url": raw_url,
        "canonical_url": canonical_url,
        "full_action_plan": dict(full_action_plan),
        "full_action_plan_hash": full_action_plan_hash,
        "full_required_actions": list(full_required_actions),
        "full_required_actions_hash": full_required_actions_hash,
        "rule_snapshot_id": rule_snapshot_id,
        "rule_hash": rule_hash,
        "execution_path_id": execution_path_id,
        "target_hash": target_hash,
    }


def _compute_intent_hash(**values: Any) -> str:
    return sha256_hex(canonical_json_bytes(_intent_hash_payload(**values)))


def _validated_full_intent_inputs(
    lottery: Mapping[str, Any],
    plan_binding: Mapping[str, Any],
) -> dict[str, Any]:
    lottery_data = dict(lottery)
    lottery_id = _require_positive_int(
        lottery_data.get("id"),
        "execution_intent_lottery_id_invalid",
    )
    platform = _require_string(
        lottery_data.get("platform"),
        "execution_intent_platform_invalid",
    ).casefold()
    if get_platform_module(platform) is None:
        raise ExecutionIntentError("execution_intent_platform_invalid")
    raw_url = _require_string(
        lottery_data.get("raw_url"),
        "execution_intent_raw_url_invalid",
        max_length=MAX_TARGET_URL_LENGTH,
    )
    canonical_url = _require_string(
        lottery_data.get("canonical_url"),
        "execution_intent_canonical_url_invalid",
        max_length=MAX_TARGET_URL_LENGTH,
    )
    action_plan_value = plan_binding.get("action_plan")
    try:
        validated = validate_action_plan_v2(
            action_plan_value,
            require_executable=True,
            reject_media=True,
        )
    except ActionPlanV2Error as exc:
        raise ExecutionIntentError(
            f"execution_intent_{exc.code}"
        ) from exc
    if validated.plan.get("platform") != platform:
        raise ExecutionIntentError("execution_intent_platform_binding_mismatch")

    rule_snapshot_id = _require_positive_int(
        plan_binding.get("rule_snapshot_id"),
        "execution_intent_rule_snapshot_id_invalid",
    )
    rule_hash = _require_hash(
        plan_binding.get("rule_hash"),
        "execution_intent_rule_hash_invalid",
    )
    full_action_plan_hash = _require_hash(
        plan_binding.get("action_plan_hash"),
        "execution_intent_action_plan_hash_invalid",
    )
    execution_path_id = _require_string(
        plan_binding.get("execution_path_id"),
        "execution_intent_execution_path_invalid",
        max_length=128,
    )
    target_hash = _require_hash(
        plan_binding.get("target_hash"),
        "execution_intent_target_hash_invalid",
    )
    required_actions = tuple(plan_binding.get("required_actions") or ())
    if (
        rule_snapshot_id != validated.rule_snapshot_id
        or rule_hash != validated.rule_hash
        or full_action_plan_hash != validated.plan_hash
        or execution_path_id != validated.execution_path_id
        or required_actions != validated.required_actions
        or target_hash != compute_target_hash(canonical_url)
        or lottery_data.get("authoritative_rule_snapshot_id") != rule_snapshot_id
        or lottery_data.get("rule_hash") != rule_hash
        or lottery_data.get("action_plan_hash") != full_action_plan_hash
    ):
        raise ExecutionIntentError("execution_intent_full_binding_mismatch")

    stored_plan = parse_stored_json(lottery_data.get("action_plan"))
    if not isinstance(stored_plan, Mapping):
        raise ExecutionIntentError("execution_intent_lottery_action_plan_invalid")
    try:
        if canonical_json_bytes(stored_plan) != canonical_json_bytes(validated.plan):
            raise ExecutionIntentError(
                "execution_intent_lottery_action_plan_mismatch"
            )
    except ActionPlanV2Error as exc:
        raise ExecutionIntentError(
            "execution_intent_lottery_action_plan_invalid"
        ) from exc

    return {
        "lottery_id": lottery_id,
        "platform": platform,
        "raw_url": raw_url,
        "canonical_url": canonical_url,
        "full_action_plan": copy.deepcopy(validated.plan),
        "full_action_plan_hash": validated.plan_hash,
        "full_required_actions": validated.required_actions,
        "full_required_actions_hash": _required_actions_hash(
            validated.required_actions
        ),
        "rule_snapshot_id": validated.rule_snapshot_id,
        "rule_hash": validated.rule_hash,
        "execution_path_id": validated.execution_path_id,
        "target_hash": target_hash,
    }


def build_frozen_execution_intent(
    lottery: Mapping[str, Any],
    *,
    source_task_id: str,
    source_account_id: int,
    plan_binding: Mapping[str, Any],
    intent_id: str | None = None,
) -> FrozenLotteryExecutionIntent:
    """Build and validate the immutable full intent stored for a lottery."""

    values = _validated_full_intent_inputs(lottery, plan_binding)
    resolved_intent_id = _require_uuid(
        intent_id or str(uuid.uuid4()),
        "execution_intent_id_invalid",
    )
    resolved_source_task_id = _require_uuid(
        source_task_id,
        "execution_intent_source_task_id_invalid",
    )
    resolved_source_account_id = _require_positive_int(
        source_account_id,
        "execution_intent_source_account_id_invalid",
    )
    hash_values = {
        "intent_id": resolved_intent_id,
        "source_task_id": resolved_source_task_id,
        "source_account_id": resolved_source_account_id,
        **values,
    }
    intent_hash = _compute_intent_hash(**hash_values)
    return FrozenLotteryExecutionIntent(
        contract_version=EXECUTION_INTENT_CONTRACT_VERSION,
        intent_hash=intent_hash,
        **hash_values,
    )


def coerce_frozen_execution_intent(
    value: FrozenLotteryExecutionIntent | Mapping[str, Any],
) -> FrozenLotteryExecutionIntent:
    """Parse a database row and revalidate every persisted hash binding."""

    if isinstance(value, FrozenLotteryExecutionIntent):
        row = {
            **value.__dict__,
            "full_required_actions": list(value.full_required_actions),
        }
    elif isinstance(value, Mapping):
        row = dict(value)
    else:
        raise ExecutionIntentError("execution_intent_invalid")
    if row.get("contract_version") != EXECUTION_INTENT_CONTRACT_VERSION:
        raise ExecutionIntentError("execution_intent_contract_version_invalid")

    intent_id = _require_uuid(
        row.get("intent_id"),
        "execution_intent_id_invalid",
    )
    source_task_id = _require_uuid(
        row.get("source_task_id"),
        "execution_intent_source_task_id_invalid",
    )
    lottery_id = _require_positive_int(
        row.get("lottery_id"),
        "execution_intent_lottery_id_invalid",
    )
    source_account_id = _require_positive_int(
        row.get("source_account_id"),
        "execution_intent_source_account_id_invalid",
    )
    platform = _require_string(
        row.get("platform"),
        "execution_intent_platform_invalid",
    ).casefold()
    if get_platform_module(platform) is None:
        raise ExecutionIntentError("execution_intent_platform_invalid")
    raw_url = _require_string(
        row.get("raw_url"),
        "execution_intent_raw_url_invalid",
        max_length=MAX_TARGET_URL_LENGTH,
    )
    canonical_url = _require_string(
        row.get("canonical_url"),
        "execution_intent_canonical_url_invalid",
        max_length=MAX_TARGET_URL_LENGTH,
    )
    full_action_plan = parse_stored_json(row.get("full_action_plan"))
    full_required_actions = parse_stored_json(row.get("full_required_actions"))
    if not isinstance(full_action_plan, Mapping) or not isinstance(
        full_required_actions,
        list,
    ):
        raise ExecutionIntentError("execution_intent_payload_invalid")
    try:
        validated = validate_action_plan_v2(
            full_action_plan,
            require_executable=True,
            reject_media=True,
        )
    except ActionPlanV2Error as exc:
        raise ExecutionIntentError(
            f"execution_intent_{exc.code}"
        ) from exc
    required_actions = tuple(full_required_actions)
    full_action_plan_hash = _require_hash(
        row.get("full_action_plan_hash"),
        "execution_intent_action_plan_hash_invalid",
    )
    full_required_actions_hash = _require_hash(
        row.get("full_required_actions_hash"),
        "execution_intent_required_actions_hash_invalid",
    )
    rule_snapshot_id = _require_positive_int(
        row.get("rule_snapshot_id"),
        "execution_intent_rule_snapshot_id_invalid",
    )
    rule_hash = _require_hash(
        row.get("rule_hash"),
        "execution_intent_rule_hash_invalid",
    )
    execution_path_id = _require_string(
        row.get("execution_path_id"),
        "execution_intent_execution_path_invalid",
        max_length=128,
    )
    target_hash = _require_hash(
        row.get("target_hash"),
        "execution_intent_target_hash_invalid",
    )
    if (
        validated.plan.get("platform") != platform
        or validated.required_actions != required_actions
        or validated.plan_hash != full_action_plan_hash
        or _required_actions_hash(required_actions)
        != full_required_actions_hash
        or validated.rule_snapshot_id != rule_snapshot_id
        or validated.rule_hash != rule_hash
        or validated.execution_path_id != execution_path_id
        or compute_target_hash(canonical_url) != target_hash
    ):
        raise ExecutionIntentError("execution_intent_payload_binding_mismatch")
    intent_hash = _require_hash(
        row.get("intent_hash"),
        "execution_intent_hash_invalid",
    )
    hash_values = {
        "intent_id": intent_id,
        "lottery_id": lottery_id,
        "source_task_id": source_task_id,
        "source_account_id": source_account_id,
        "platform": platform,
        "raw_url": raw_url,
        "canonical_url": canonical_url,
        "full_action_plan": dict(full_action_plan),
        "full_action_plan_hash": full_action_plan_hash,
        "full_required_actions": required_actions,
        "full_required_actions_hash": full_required_actions_hash,
        "rule_snapshot_id": rule_snapshot_id,
        "rule_hash": rule_hash,
        "execution_path_id": execution_path_id,
        "target_hash": target_hash,
    }
    if _compute_intent_hash(**hash_values) != intent_hash:
        raise ExecutionIntentError("execution_intent_hash_mismatch")
    return FrozenLotteryExecutionIntent(
        contract_version=EXECUTION_INTENT_CONTRACT_VERSION,
        intent_hash=intent_hash,
        **hash_values,
    )


def validate_lottery_execution_intent_binding(
    intent: FrozenLotteryExecutionIntent | Mapping[str, Any],
    lottery: Mapping[str, Any],
) -> FrozenLotteryExecutionIntent:
    """Ensure mutable review state still equals the frozen authority."""

    frozen = coerce_frozen_execution_intent(intent)
    lottery_data = dict(lottery)
    try:
        current_plan = validate_action_plan_v2(
            parse_stored_json(lottery_data.get("action_plan")),
            require_executable=True,
            reject_media=True,
        )
        unchanged = (
            lottery_data.get("id") == frozen.lottery_id
            and str(lottery_data.get("platform") or "").casefold()
            == frozen.platform
            and lottery_data.get("raw_url") == frozen.raw_url
            and lottery_data.get("canonical_url") == frozen.canonical_url
            and lottery_data.get("authoritative_rule_snapshot_id")
            == frozen.rule_snapshot_id
            and lottery_data.get("rule_hash") == frozen.rule_hash
            and lottery_data.get("action_plan_hash")
            == frozen.full_action_plan_hash
            and current_plan.plan_hash == frozen.full_action_plan_hash
            and canonical_json_bytes(current_plan.plan)
            == canonical_json_bytes(frozen.full_action_plan)
            and compute_target_hash(str(lottery_data.get("canonical_url") or ""))
            == frozen.target_hash
        )
    except (ActionPlanV2Error, TypeError, ValueError):
        unchanged = False
    if not unchanged:
        raise ExecutionIntentError(
            "execution_intent_lottery_binding_changed"
        )
    return frozen


def compute_requested_actions_hash(
    intent: FrozenLotteryExecutionIntent | Mapping[str, Any],
    requested_actions: Sequence[str],
) -> str:
    frozen = coerce_frozen_execution_intent(intent)
    return sha256_hex(
        canonical_json_bytes(
            {
                "contract_version": EXECUTION_INTENT_CONTRACT_VERSION,
                "execution_intent_id": frozen.intent_id,
                "execution_intent_hash": frozen.intent_hash,
                "requested_actions": list(requested_actions),
            }
        )
    )


def _normalize_requested_actions(
    frozen: FrozenLotteryExecutionIntent,
    requested_actions: Sequence[str],
    *,
    strict_subset: bool,
) -> tuple[str, ...]:
    if not isinstance(requested_actions, (list, tuple)) or not requested_actions:
        raise ExecutionIntentError("execution_intent_requested_actions_invalid")
    requested = tuple(requested_actions)
    if any(not isinstance(action, str) for action in requested):
        raise ExecutionIntentError("execution_intent_requested_actions_invalid")
    if len(set(requested)) != len(requested):
        raise ExecutionIntentError("execution_intent_requested_actions_invalid")
    selected = set(requested)
    normalized = tuple(
        action for action in frozen.full_required_actions if action in selected
    )
    if requested != normalized or not selected.issubset(
        set(frozen.full_required_actions)
    ):
        raise ExecutionIntentError("execution_intent_requested_actions_invalid")
    if strict_subset and len(requested) >= len(frozen.full_required_actions):
        raise ExecutionIntentError(
            "execution_intent_repair_actions_not_strict_subset"
        )
    return requested


def _subset_content_requirements(
    requirements: Mapping[str, Any],
    requested: set[str],
) -> dict[str, Any]:
    empty = {"topic_tags": [], "mentions": []}
    return {
        "follow_targets": (
            copy.deepcopy(requirements["follow_targets"])
            if "followed" in requested
            else []
        ),
        "commented": (
            copy.deepcopy(requirements["commented"])
            if "commented" in requested
            else copy.deepcopy(empty)
        ),
        "reposted": (
            copy.deepcopy(requirements["reposted"])
            if "reposted" in requested
            else copy.deepcopy(empty)
        ),
    }


def build_repair_execution_subset(
    intent: FrozenLotteryExecutionIntent | Mapping[str, Any],
    requested_actions: Sequence[str],
) -> RequestedExecutionSubset:
    """Derive one deterministic Action Plan v2 strict subset from frozen state."""

    frozen = coerce_frozen_execution_intent(intent)
    requested = _normalize_requested_actions(
        frozen,
        requested_actions,
        strict_subset=True,
    )
    try:
        validated_full = validate_action_plan_v2(
            frozen.full_action_plan,
            require_executable=True,
            reject_media=True,
        )
        plan = copy.deepcopy(validated_full.plan)
        selected = set(requested)
        plan["required_actions"] = list(requested)
        plan["action_payloads"] = {
            action: copy.deepcopy(validated_full.action_payloads[action])
            for action in requested
        }
        plan["content_requirements"] = _subset_content_requirements(
            validated_full.content_requirements,
            selected,
        )
        plan["source_content_requirements"] = _subset_content_requirements(
            validated_full.source_content_requirements,
            selected,
        )
        plan["friend_mention_requirements"] = {
            action: copy.deepcopy(requirement)
            for action, requirement in (
                validated_full.friend_mention_requirements.items()
            )
            if action in selected
        }
        platform_module = get_platform_module(frozen.platform)
        if platform_module is None:
            raise ExecutionIntentError("execution_intent_platform_invalid")
        expected_capabilities = (
            platform_module.build_runtime_capability_requirements(
                requested,
                frozen.execution_path_id,
            )
        )
        plan["runtime_capability_requirements"] = (
            copy.deepcopy(expected_capabilities)
            if expected_capabilities is not None
            else {}
        )
        plan["plan_hash"] = compute_action_plan_hash(plan)
        validated_subset = validate_action_plan_v2(
            plan,
            require_executable=True,
            reject_media=True,
        )
    except ExecutionIntentError:
        raise
    except ActionPlanV2Error as exc:
        raise ExecutionIntentError(
            f"execution_intent_repair_{exc.code}"
        ) from exc
    if validated_subset.required_actions != requested:
        raise ExecutionIntentError(
            "execution_intent_repair_action_plan_mismatch"
        )
    requested_hash = compute_requested_actions_hash(frozen, requested)
    return RequestedExecutionSubset(
        intent_id=frozen.intent_id,
        intent_hash=frozen.intent_hash,
        requested_actions=requested,
        requested_actions_hash=requested_hash,
        action_plan=copy.deepcopy(validated_subset.plan),
        action_plan_hash=validated_subset.plan_hash,
    )


def _binding_hash_payload(
    *,
    task_id: str,
    intent_id: str,
    intent_hash: str,
    lottery_id: int,
    account_id: int,
    binding_kind: str,
    requested_actions: Sequence[str],
    requested_actions_hash: str,
    bound_action_plan: Mapping[str, Any],
    bound_action_plan_hash: str,
    evidence_action_plan_hash: str,
    rule_snapshot_id: int,
    rule_hash: str,
    execution_evidence_id: str,
    execution_evidence_kind: str,
    exact_execution_evidence_id: str | None,
    oauth_calibration_id: str | None,
    execution_path_id: str,
    target_hash: str,
    config_hash: str,
    execution_revision: int,
    account_lease_id: str,
    account_lease_generation: int,
) -> dict[str, Any]:
    return {
        "contract_version": EXECUTION_INTENT_CONTRACT_VERSION,
        "task_id": task_id,
        "execution_intent_id": intent_id,
        "execution_intent_hash": intent_hash,
        "lottery_id": lottery_id,
        "account_id": account_id,
        "binding_kind": binding_kind,
        "requested_actions": list(requested_actions),
        "requested_actions_hash": requested_actions_hash,
        "bound_action_plan": dict(bound_action_plan),
        "bound_action_plan_hash": bound_action_plan_hash,
        "evidence_action_plan_hash": evidence_action_plan_hash,
        "rule_snapshot_id": rule_snapshot_id,
        "rule_hash": rule_hash,
        "execution_evidence_id": execution_evidence_id,
        "execution_evidence_kind": execution_evidence_kind,
        "exact_execution_evidence_id": exact_execution_evidence_id,
        "oauth_calibration_id": oauth_calibration_id,
        "execution_path_id": execution_path_id,
        "target_hash": target_hash,
        "config_hash": config_hash,
        "execution_revision": execution_revision,
        "account_lease_id": account_lease_id,
        "account_lease_generation": account_lease_generation,
    }


def build_task_execution_intent_binding(
    intent: FrozenLotteryExecutionIntent | Mapping[str, Any],
    *,
    task_id: str,
    account_id: int,
    binding_kind: str,
    requested_actions: Sequence[str],
    bound_action_plan: Mapping[str, Any],
    execution_evidence_id: str,
    execution_path_id: str,
    target_hash: str,
    config_hash: str,
    execution_revision: int,
    account_lease_id: str,
    account_lease_generation: int,
) -> TaskExecutionIntentBinding:
    """Bind a task's exact plan and execution context to one frozen intent."""

    frozen = coerce_frozen_execution_intent(intent)
    kind = _require_string(
        binding_kind,
        "execution_intent_binding_kind_invalid",
        max_length=16,
    )
    if kind not in EXECUTION_INTENT_KINDS:
        raise ExecutionIntentError("execution_intent_binding_kind_invalid")
    requested = _normalize_requested_actions(
        frozen,
        requested_actions,
        strict_subset=(kind == "repair"),
    )
    try:
        validated_plan = validate_action_plan_v2(
            bound_action_plan,
            require_executable=True,
            reject_media=True,
        )
    except ActionPlanV2Error as exc:
        raise ExecutionIntentError(
            f"execution_intent_binding_{exc.code}"
        ) from exc
    if (
        validated_plan.required_actions != requested
        or validated_plan.rule_snapshot_id != frozen.rule_snapshot_id
        or validated_plan.rule_hash != frozen.rule_hash
        or validated_plan.execution_path_id != frozen.execution_path_id
    ):
        raise ExecutionIntentError(
            "execution_intent_binding_action_plan_mismatch"
        )
    if kind == "full":
        if (
            requested != frozen.full_required_actions
            or canonical_json_bytes(validated_plan.plan)
            != canonical_json_bytes(frozen.full_action_plan)
        ):
            raise ExecutionIntentError(
                "execution_intent_full_task_binding_mismatch"
            )
    else:
        expected_subset = build_repair_execution_subset(frozen, requested)
        if canonical_json_bytes(validated_plan.plan) != canonical_json_bytes(
            expected_subset.action_plan
        ):
            raise ExecutionIntentError(
                "execution_intent_repair_task_binding_mismatch"
            )

    resolved_task_id = _require_uuid(
        task_id,
        "execution_intent_binding_task_id_invalid",
    )
    resolved_account_id = _require_positive_int(
        account_id,
        "execution_intent_binding_account_id_invalid",
    )
    if resolved_account_id != frozen.source_account_id:
        raise ExecutionIntentError(
            "execution_intent_repair_account_mismatch"
            if kind == "repair"
            else "execution_intent_full_account_mismatch"
        )
    resolved_evidence_id = _require_uuid(
        execution_evidence_id,
        "execution_intent_binding_evidence_id_invalid",
    )
    resolved_execution_path = _require_string(
        execution_path_id,
        "execution_intent_binding_execution_path_invalid",
        max_length=128,
    )
    resolved_target_hash = _require_hash(
        target_hash,
        "execution_intent_binding_target_hash_invalid",
    )
    resolved_config_hash = _require_hash(
        config_hash,
        "execution_intent_binding_config_hash_invalid",
    )
    resolved_revision = _require_positive_int(
        execution_revision,
        "execution_intent_binding_execution_revision_invalid",
    )
    resolved_lease_id = _require_uuid(
        account_lease_id,
        "execution_intent_binding_lease_id_invalid",
    )
    resolved_lease_generation = _require_positive_int(
        account_lease_generation,
        "execution_intent_binding_lease_generation_invalid",
    )
    if (
        resolved_execution_path != frozen.execution_path_id
        or resolved_target_hash != frozen.target_hash
    ):
        raise ExecutionIntentError(
            "execution_intent_binding_target_path_mismatch"
        )
    platform_module = get_platform_module(frozen.platform)
    evidence_kind = (
        platform_module.execution_evidence_kind_for(
            frozen.execution_path_id
        )
        if platform_module is not None
        else None
    )
    if evidence_kind not in EXECUTION_EVIDENCE_KINDS:
        raise ExecutionIntentError(
            "execution_intent_binding_evidence_kind_invalid"
        )
    exact_execution_evidence_id = (
        resolved_evidence_id
        if evidence_kind == "exact_execution_evidence"
        else None
    )
    oauth_calibration_id = (
        resolved_evidence_id
        if evidence_kind == "oauth_account_calibration"
        else None
    )
    requested_hash = compute_requested_actions_hash(frozen, requested)
    hash_values = {
        "task_id": resolved_task_id,
        "intent_id": frozen.intent_id,
        "intent_hash": frozen.intent_hash,
        "lottery_id": frozen.lottery_id,
        "account_id": resolved_account_id,
        "binding_kind": kind,
        "requested_actions": requested,
        "requested_actions_hash": requested_hash,
        "bound_action_plan": copy.deepcopy(validated_plan.plan),
        "bound_action_plan_hash": validated_plan.plan_hash,
        # Evidence and task_runs remain bound to the immutable full plan.
        # The exact repair subset has its own independent hash above.
        "evidence_action_plan_hash": frozen.full_action_plan_hash,
        "rule_snapshot_id": frozen.rule_snapshot_id,
        "rule_hash": frozen.rule_hash,
        "execution_evidence_id": resolved_evidence_id,
        "execution_evidence_kind": evidence_kind,
        "exact_execution_evidence_id": exact_execution_evidence_id,
        "oauth_calibration_id": oauth_calibration_id,
        "execution_path_id": resolved_execution_path,
        "target_hash": resolved_target_hash,
        "config_hash": resolved_config_hash,
        "execution_revision": resolved_revision,
        "account_lease_id": resolved_lease_id,
        "account_lease_generation": resolved_lease_generation,
    }
    binding_hash = sha256_hex(
        canonical_json_bytes(_binding_hash_payload(**hash_values))
    )
    return TaskExecutionIntentBinding(
        contract_version=EXECUTION_INTENT_CONTRACT_VERSION,
        binding_hash=binding_hash,
        **hash_values,
    )


def _same_frozen_business_intent(
    current: FrozenLotteryExecutionIntent,
    candidate: FrozenLotteryExecutionIntent,
) -> bool:
    fields = (
        "lottery_id",
        "source_account_id",
        "platform",
        "raw_url",
        "canonical_url",
        "full_action_plan",
        "full_action_plan_hash",
        "full_required_actions",
        "full_required_actions_hash",
        "rule_snapshot_id",
        "rule_hash",
        "execution_path_id",
        "target_hash",
    )
    return all(getattr(current, field) == getattr(candidate, field) for field in fields)


async def _load_lottery_execution_intent_head(
    db,
    lottery_id: int,
    *,
    for_update: bool = False,
) -> tuple[FrozenLotteryExecutionIntent, int] | None:
    suffix = " FOR UPDATE" if for_update else ""
    row = await db.fetch_one(
        """SELECT root.contract_version, root.intent_id, root.intent_hash,
                  root.lottery_id, root.source_task_id,
                  root.source_account_id, root.platform, root.raw_url,
                  root.canonical_url, root.full_action_plan,
                  root.full_action_plan_hash, root.full_required_actions,
                  root.full_required_actions_hash, root.rule_snapshot_id,
                  root.rule_hash, root.execution_path_id, root.target_hash,
                  head.generation AS current_generation
           FROM lottery_execution_intent_heads AS head
           JOIN lottery_execution_intents AS root
             ON root.lottery_id = head.lottery_id
            AND root.intent_id = head.current_intent_id
           WHERE head.lottery_id = :lottery_id"""
        + suffix,
        {"lottery_id": lottery_id},
    )
    if not row:
        return None
    row_data = dict(row)
    generation = row_data.pop("current_generation", None)
    if type(generation) is not int or generation <= 0:
        raise ExecutionIntentError(
            "execution_intent_head_generation_invalid"
        )
    return coerce_frozen_execution_intent(row_data), generation


async def load_lottery_execution_intent(
    db,
    lottery_id: int,
    *,
    for_update: bool = False,
) -> FrozenLotteryExecutionIntent | None:
    head = await _load_lottery_execution_intent_head(
        db,
        lottery_id,
        for_update=for_update,
    )
    return head[0] if head is not None else None


async def load_lottery_execution_intents(
    db,
    lottery_ids: Sequence[int],
) -> dict[
    int,
    FrozenLotteryExecutionIntent | ExecutionIntentLoadFailure,
]:
    """Batch-load roots while containing corrupt rows to their lottery."""

    ids = list(dict.fromkeys(int(lottery_id) for lottery_id in lottery_ids))
    if not ids:
        return {}
    requested_ids = set(ids)
    placeholders = ", ".join(
        f":execution_intent_lottery_{index}" for index in range(len(ids))
    )
    values = {
        f"execution_intent_lottery_{index}": lottery_id
        for index, lottery_id in enumerate(ids)
    }
    rows = await db.fetch_all(
        f"""SELECT root.contract_version, root.intent_id, root.intent_hash,
                   root.lottery_id, root.source_task_id,
                   root.source_account_id, root.platform, root.raw_url,
                   root.canonical_url, root.full_action_plan,
                   root.full_action_plan_hash, root.full_required_actions,
                   root.full_required_actions_hash, root.rule_snapshot_id,
                   root.rule_hash, root.execution_path_id, root.target_hash
            FROM lottery_execution_intent_heads AS head
            JOIN lottery_execution_intents AS root
              ON root.lottery_id = head.lottery_id
             AND root.intent_id = head.current_intent_id
            WHERE head.lottery_id IN ({placeholders})""",
        values,
    )
    result: dict[
        int,
        FrozenLotteryExecutionIntent | ExecutionIntentLoadFailure,
    ] = {}
    for row in rows:
        row_data = dict(row)
        try:
            lottery_id = int(row_data.get("lottery_id"))
        except (TypeError, ValueError) as exc:
            # Without a trustworthy scope this cannot be represented as a
            # per-lottery failure.  The BIGINT/FK schema makes this a database
            # contract violation rather than ordinary row corruption.
            raise ExecutionIntentError(
                "execution_intent_lottery_id_invalid"
            ) from exc
        if lottery_id not in requested_ids or lottery_id <= 0:
            raise ExecutionIntentError(
                "execution_intent_lottery_id_invalid"
            )
        if lottery_id in result:
            result[lottery_id] = ExecutionIntentLoadFailure(
                lottery_id=lottery_id,
                code="execution_intent_duplicate_lottery_binding",
            )
            continue
        try:
            result[lottery_id] = coerce_frozen_execution_intent(row_data)
        except ExecutionIntentError as exc:
            result[lottery_id] = ExecutionIntentLoadFailure(
                lottery_id=lottery_id,
                code=exc.code,
            )
        except (ImportError, PlatformModuleUnavailableError):
            result[lottery_id] = ExecutionIntentLoadFailure(
                lottery_id=lottery_id,
                code="platform_module_unavailable",
            )
        except (KeyError, TypeError, ValueError):
            result[lottery_id] = ExecutionIntentLoadFailure(
                lottery_id=lottery_id,
                code="execution_intent_invalid",
            )
    return result


async def _insert_frozen_intent(
    db,
    intent: FrozenLotteryExecutionIntent,
) -> None:
    await db.execute(
        """INSERT INTO lottery_execution_intents
             (contract_version, intent_id, intent_hash, lottery_id,
              source_task_id, source_account_id, platform, raw_url,
              canonical_url, full_action_plan, full_action_plan_hash,
              full_required_actions, full_required_actions_hash,
              rule_snapshot_id, rule_hash, execution_path_id, target_hash)
           VALUES
             (:contract_version, :intent_id, :intent_hash, :lottery_id,
              :source_task_id, :source_account_id, :platform, :raw_url,
              :canonical_url, :full_action_plan, :full_action_plan_hash,
              :full_required_actions, :full_required_actions_hash,
              :rule_snapshot_id, :rule_hash, :execution_path_id, :target_hash)""",
        {
            **intent.__dict__,
            "full_action_plan": json.dumps(
                intent.full_action_plan,
                ensure_ascii=False,
            ),
            "full_required_actions": json.dumps(
                list(intent.full_required_actions),
                ensure_ascii=False,
            ),
        },
    )


async def _insert_execution_intent_head(
    db,
    intent: FrozenLotteryExecutionIntent,
) -> None:
    await db.execute(
        """INSERT INTO lottery_execution_intent_heads
             (lottery_id, current_intent_id, generation)
           VALUES (:lottery_id, :current_intent_id, 1)""",
        {
            "lottery_id": intent.lottery_id,
            "current_intent_id": intent.intent_id,
        },
    )


async def _switch_execution_intent_head(
    db,
    *,
    current: FrozenLotteryExecutionIntent,
    current_generation: int,
    successor: FrozenLotteryExecutionIntent,
) -> None:
    await db.execute(
        """UPDATE lottery_execution_intent_heads
           SET current_intent_id = :successor_intent_id,
               generation = generation + 1,
               updated_at = NOW()
           WHERE lottery_id = :lottery_id
             AND current_intent_id = :current_intent_id
             AND generation = :current_generation""",
        {
            "successor_intent_id": successor.intent_id,
            "lottery_id": current.lottery_id,
            "current_intent_id": current.intent_id,
            "current_generation": current_generation,
        },
    )
    row = await db.fetch_one("SELECT ROW_COUNT() AS affected")
    try:
        affected = int(row["affected"]) if row is not None else -1
    except (KeyError, TypeError, ValueError):
        affected = -1
    if affected != 1:
        raise ExecutionIntentError(
            "execution_intent_head_switch_conflict"
        )


async def _insert_task_binding(
    db,
    binding: TaskExecutionIntentBinding,
) -> None:
    await db.execute(
        """INSERT INTO task_execution_intent_bindings
             (contract_version, task_id, intent_id, lottery_id, account_id,
              binding_kind, requested_actions, requested_actions_hash,
              bound_action_plan, bound_action_plan_hash,
              evidence_action_plan_hash, rule_snapshot_id, rule_hash,
              execution_evidence_id, execution_evidence_kind,
              exact_execution_evidence_id, oauth_calibration_id,
              execution_path_id, target_hash, config_hash,
              execution_revision, account_lease_id,
              account_lease_generation, binding_hash)
           VALUES
             (:contract_version, :task_id, :intent_id, :lottery_id, :account_id,
              :binding_kind, :requested_actions, :requested_actions_hash,
              :bound_action_plan, :bound_action_plan_hash,
              :evidence_action_plan_hash, :rule_snapshot_id, :rule_hash,
              :execution_evidence_id, :execution_evidence_kind,
              :exact_execution_evidence_id, :oauth_calibration_id,
              :execution_path_id, :target_hash,
              :config_hash, :execution_revision,
              :account_lease_id, :account_lease_generation, :binding_hash)""",
        {
            **binding.__dict__,
            "requested_actions": json.dumps(
                list(binding.requested_actions),
                ensure_ascii=False,
            ),
            "bound_action_plan": json.dumps(
                binding.bound_action_plan,
                ensure_ascii=False,
            ),
        },
    )


async def persist_full_execution_intent(
    db,
    *,
    lottery: Mapping[str, Any],
    task_id: str,
    account_id: int,
    plan_binding: Mapping[str, Any],
    execution_evidence_id: str,
    account_lease_id: str,
    account_lease_generation: int,
    allow_current_intent_supersede: bool = False,
) -> TaskExecutionIntentBinding:
    """Freeze/reuse the full intent and bind one real-run task atomically.

    The caller must already hold the lottery row lock and run this inside the
    same transaction as ``task_runs`` and the outbox row.
    """

    candidate = build_frozen_execution_intent(
        lottery,
        source_task_id=task_id,
        source_account_id=account_id,
        plan_binding=plan_binding,
    )
    current_head = await _load_lottery_execution_intent_head(
        db,
        candidate.lottery_id,
        for_update=True,
    )
    if current_head is None:
        frozen = candidate
        await _insert_frozen_intent(db, frozen)
        await _insert_execution_intent_head(db, frozen)
    else:
        current, current_generation = current_head
        frozen = current
        if _same_frozen_business_intent(current, candidate):
            pass
        elif allow_current_intent_supersede is True:
            frozen = candidate
            await _insert_frozen_intent(db, frozen)
            await _switch_execution_intent_head(
                db,
                current=current,
                current_generation=current_generation,
                successor=frozen,
            )
        else:
            raise ExecutionIntentError("execution_intent_conflict")

    binding = build_task_execution_intent_binding(
        frozen,
        task_id=task_id,
        account_id=account_id,
        binding_kind="full",
        requested_actions=frozen.full_required_actions,
        bound_action_plan=frozen.full_action_plan,
        execution_evidence_id=execution_evidence_id,
        execution_path_id=str(plan_binding.get("execution_path_id") or ""),
        target_hash=str(plan_binding.get("target_hash") or ""),
        config_hash=str(plan_binding.get("config_hash") or ""),
        execution_revision=plan_binding.get("execution_revision"),
        account_lease_id=account_lease_id,
        account_lease_generation=account_lease_generation,
    )
    await _insert_task_binding(db, binding)
    return binding


async def persist_repair_execution_binding(
    db,
    *,
    intent: FrozenLotteryExecutionIntent | Mapping[str, Any],
    task_id: str,
    account_id: int,
    requested_actions: Sequence[str],
    execution_evidence_id: str,
    config_hash: str,
    execution_revision: int,
    account_lease_id: str,
    account_lease_generation: int,
) -> TaskExecutionIntentBinding:
    """Persist a repair task's deterministic strict-subset binding."""

    frozen = coerce_frozen_execution_intent(intent)
    if int(account_id) != frozen.source_account_id:
        raise ExecutionIntentError(
            "execution_intent_repair_account_mismatch"
        )
    subset = build_repair_execution_subset(frozen, requested_actions)
    binding = build_task_execution_intent_binding(
        frozen,
        task_id=task_id,
        account_id=account_id,
        binding_kind="repair",
        requested_actions=subset.requested_actions,
        bound_action_plan=subset.action_plan,
        execution_evidence_id=execution_evidence_id,
        execution_path_id=frozen.execution_path_id,
        target_hash=frozen.target_hash,
        config_hash=config_hash,
        execution_revision=execution_revision,
        account_lease_id=account_lease_id,
        account_lease_generation=account_lease_generation,
    )
    await _insert_task_binding(db, binding)
    return binding
