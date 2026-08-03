"""Independent Worker validation for durable full and repair intents.

Redis is an untrusted transport.  The Worker therefore reconstructs every
hash in Core's persisted execution-intent contract and derives the exact
repair subset again before exposing any action to a platform executor.
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
    ValidatedActionPlanV2,
    action_plan_contract_for,
    canonical_json_bytes,
    compute_action_plan_hash,
    compute_target_hash,
    sha256_hex,
    validate_action_plan_v2,
)
from app.task_streams import LEGACY_TASK_STREAM_KEY
from shared.execution_contracts import (
    FULL_EXECUTION_INTENT_KIND,
    REPAIR_EXECUTION_INTENT_KIND,
)


EXECUTION_INTENT_CONTRACT_VERSION = 1
_HASH_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_REDIS_STREAM_ID_PATTERN = re.compile(r"(?:0|[1-9][0-9]*)-(?:0|[1-9][0-9]*)\Z")
_KINDS = frozenset(
    {
        FULL_EXECUTION_INTENT_KIND,
        REPAIR_EXECUTION_INTENT_KIND,
    }
)
_EVIDENCE_KINDS = frozenset(
    {"exact_execution_evidence", "oauth_account_calibration"}
)
_MESSAGE_SCALAR_FIELDS = (
    "execution_intent_id",
    "execution_intent_hash",
    "execution_intent_kind",
    "execution_intent_binding_hash",
    "requested_actions_hash",
    "requested_action_plan_hash",
    "execution_evidence_kind",
    "exact_execution_evidence_id",
    "oauth_calibration_id",
)


class ExecutionIntentValidationError(ValueError):
    """The persisted/message contract cannot authorize execution."""

    def __init__(self, code: str) -> None:
        self.code = str(code or "execution_intent_invalid")
        super().__init__(self.code)


@dataclass(frozen=True)
class ValidatedTaskExecutionIntent:
    intent_id: str | None
    intent_hash: str | None
    binding_kind: str
    binding_hash: str | None
    requested_actions: tuple[str, ...]
    action_plan: ValidatedActionPlanV2
    legacy: bool = False


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    if hasattr(row, "get"):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, TypeError):
        return default


def _require_string(
    value: Any,
    code: str,
    *,
    max_length: int | None = None,
) -> str:
    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ExecutionIntentValidationError(code) from exc
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or (max_length is not None and len(value) > max_length)
    ):
        raise ExecutionIntentValidationError(code)
    return value


def _require_hash(value: Any, code: str) -> str:
    result = _require_string(value, code)
    if not _HASH_PATTERN.fullmatch(result):
        raise ExecutionIntentValidationError(code)
    return result


def _require_uuid(value: Any, code: str) -> str:
    result = _require_string(value, code)
    try:
        parsed = uuid.UUID(result)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ExecutionIntentValidationError(code) from exc
    if str(parsed) != result:
        raise ExecutionIntentValidationError(code)
    return result


def _optional_uuid(value: Any, code: str) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    return _require_uuid(value, code)


def _require_positive_int(value: Any, code: str) -> int:
    if type(value) is not int or value <= 0:
        raise ExecutionIntentValidationError(code)
    return value


def _message_positive_int(value: Any, code: str) -> int:
    if isinstance(value, bool):
        raise ExecutionIntentValidationError(code)
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ExecutionIntentValidationError(code) from exc
    if result <= 0 or str(value).strip() != str(result):
        raise ExecutionIntentValidationError(code)
    return result


def _json_value(value: Any, code: str) -> Any:
    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ExecutionIntentValidationError(code) from exc
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ExecutionIntentValidationError(code) from exc
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, (list, tuple)):
        return list(value)
    raise ExecutionIntentValidationError(code)


def _json_object(value: Any, code: str) -> dict[str, Any]:
    result = _json_value(value, code)
    if not isinstance(result, dict):
        raise ExecutionIntentValidationError(code)
    return result


def _json_list(value: Any, code: str) -> list[Any]:
    result = _json_value(value, code)
    if not isinstance(result, list):
        raise ExecutionIntentValidationError(code)
    return result


def _canonical_hash(value: Any) -> str:
    try:
        return sha256_hex(canonical_json_bytes(value))
    except ActionPlanV2Error as exc:
        raise ExecutionIntentValidationError(
            "execution_intent_not_canonicalizable"
        ) from exc


def _db_contract_present(row: Any, prefix: str) -> bool:
    return any(
        _row_get(row, field) is not None
        for field in (
            f"{prefix}_contract_version",
            f"{prefix}_intent_id",
            f"{prefix}_task_id",
        )
    )


def _message_contract_present(task: Mapping[str, Any]) -> bool:
    if any(str(task.get(field) or "").strip() for field in _MESSAGE_SCALAR_FIELDS):
        return True
    if "requested_actions" not in task:
        return False
    try:
        return _json_list(
            task.get("requested_actions"),
            "execution_intent_requested_actions_invalid",
        ) != []
    except ExecutionIntentValidationError:
        return True


def _validate_legacy_full_authority(
    task: Mapping[str, Any],
    row: Any,
    *,
    task_id: str,
    account_id: int,
    lottery_id: int,
    platform: str,
    full_plan: ValidatedActionPlanV2,
) -> None:
    """Accept only a pre-contract task re-emitted by the trusted legacy fanout.

    Absence of the new contract is not itself proof that a task predates the
    migration.  The immutable legacy Outbox row is the business authority and
    the two provenance fields prove that Core, rather than a new producer,
    routed it from the retired shared stream.
    """

    source_stream = str(task.get("legacy_source_stream") or "").strip()
    source_message_id = str(
        task.get("legacy_source_message_id") or ""
    ).strip()
    if (
        source_stream != LEGACY_TASK_STREAM_KEY
        or not _REDIS_STREAM_ID_PATTERN.fullmatch(source_message_id)
    ):
        raise ExecutionIntentValidationError(
            "execution_intent_legacy_authority_missing"
        )

    if (
        str(_row_get(row, "legacy_outbox_stream_key") or "").strip()
        != LEGACY_TASK_STREAM_KEY
        or str(_row_get(row, "legacy_outbox_status") or "").strip().lower()
        != "sent"
        or str(_row_get(row, "legacy_outbox_dedup_key") or "").strip()
        != task_id
    ):
        raise ExecutionIntentValidationError(
            "execution_intent_legacy_authority_missing"
        )
    payload = _json_object(
        _row_get(row, "legacy_outbox_payload"),
        "execution_intent_legacy_outbox_invalid",
    )
    try:
        payload_account_id = int(payload.get("account_id"))
        payload_lottery_id = int(payload.get("lottery_id"))
    except (TypeError, ValueError) as exc:
        raise ExecutionIntentValidationError(
            "execution_intent_legacy_outbox_invalid"
        ) from exc
    payload_platform = str(payload.get("platform") or "").strip().lower()
    # The only platform-less historical envelope was the original Bilibili
    # format.  A missing platform must never become a wildcard for later lanes.
    if not payload_platform:
        payload_platform = "bilibili"
    if (
        str(payload.get("task_id") or "").strip() != task_id
        or payload_account_id != account_id
        or payload_lottery_id != lottery_id
        or payload_platform != platform
        or str(payload.get("mode") or "").strip().lower() != "real_run"
    ):
        raise ExecutionIntentValidationError(
            "execution_intent_legacy_outbox_mismatch"
        )
    payload_plan = _json_object(
        payload.get("action_plan"),
        "execution_intent_legacy_outbox_invalid",
    )
    if payload_plan != full_plan.plan:
        raise ExecutionIntentValidationError(
            "execution_intent_legacy_outbox_mismatch"
        )
    payload_plan_hash = str(payload.get("action_plan_hash") or "").strip()
    if payload_plan_hash and payload_plan_hash != full_plan.plan_hash:
        raise ExecutionIntentValidationError(
            "execution_intent_legacy_outbox_mismatch"
        )


def _normalize_requested_actions(
    full_actions: tuple[str, ...],
    value: Any,
    *,
    strict_subset: bool,
) -> tuple[str, ...]:
    actions = _json_list(value, "execution_intent_requested_actions_invalid")
    if (
        not actions
        or any(not isinstance(action, str) for action in actions)
        or len(set(actions)) != len(actions)
    ):
        raise ExecutionIntentValidationError(
            "execution_intent_requested_actions_invalid"
        )
    requested = tuple(actions)
    selected = set(requested)
    normalized = tuple(action for action in full_actions if action in selected)
    if requested != normalized or not selected.issubset(set(full_actions)):
        raise ExecutionIntentValidationError(
            "execution_intent_requested_actions_invalid"
        )
    if strict_subset and len(requested) >= len(full_actions):
        raise ExecutionIntentValidationError(
            "execution_intent_repair_actions_not_strict_subset"
        )
    return requested


def _subset_requirements(
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


def _derive_subset_plan(
    full_plan: ValidatedActionPlanV2,
    requested_actions: tuple[str, ...],
) -> ValidatedActionPlanV2:
    plan = copy.deepcopy(full_plan.plan)
    selected = set(requested_actions)
    plan["required_actions"] = list(requested_actions)
    plan["action_payloads"] = {
        action: copy.deepcopy(full_plan.action_payloads[action])
        for action in requested_actions
    }
    plan["content_requirements"] = _subset_requirements(
        full_plan.content_requirements,
        selected,
    )
    plan["source_content_requirements"] = _subset_requirements(
        full_plan.source_content_requirements,
        selected,
    )
    plan["friend_mention_requirements"] = {
        action: copy.deepcopy(requirement)
        for action, requirement in full_plan.friend_mention_requirements.items()
        if action in selected
    }
    contract = action_plan_contract_for(
        str(full_plan.plan.get("platform") or "").strip().casefold()
    )
    plan["runtime_capability_requirements"] = contract.runtime_capabilities_for(
        full_plan.execution_path_id,
        requested_actions,
    )
    plan["plan_hash"] = compute_action_plan_hash(plan)
    try:
        result = validate_action_plan_v2(
            plan,
            require_executable=True,
            reject_media=True,
        )
    except ActionPlanV2Error as exc:
        raise ExecutionIntentValidationError(
            f"execution_intent_repair_{exc.code}"
        ) from exc
    if result.required_actions != requested_actions:
        raise ExecutionIntentValidationError(
            "execution_intent_repair_action_plan_mismatch"
        )
    return result


def _validate_root(
    task: Mapping[str, Any],
    row: Any,
    *,
    task_id: str,
    account_id: int,
    lottery_id: int,
    platform: str,
    full_plan: ValidatedActionPlanV2,
) -> dict[str, Any]:
    contract_version = _require_positive_int(
        _row_get(row, "root_contract_version"),
        "execution_intent_contract_version_invalid",
    )
    if contract_version != EXECUTION_INTENT_CONTRACT_VERSION:
        raise ExecutionIntentValidationError(
            "execution_intent_contract_version_invalid"
        )
    intent_id = _require_uuid(
        _row_get(row, "root_intent_id"),
        "execution_intent_id_invalid",
    )
    intent_hash = _require_hash(
        _row_get(row, "root_intent_hash"),
        "execution_intent_hash_invalid",
    )
    root_lottery_id = _require_positive_int(
        _row_get(row, "root_lottery_id"),
        "execution_intent_lottery_id_invalid",
    )
    source_task_id = _require_uuid(
        _row_get(row, "root_source_task_id"),
        "execution_intent_source_task_id_invalid",
    )
    source_account_id = _require_positive_int(
        _row_get(row, "root_source_account_id"),
        "execution_intent_source_account_id_invalid",
    )
    root_platform = _require_string(
        _row_get(row, "root_platform"),
        "execution_intent_platform_invalid",
    ).casefold()
    raw_url = _require_string(
        _row_get(row, "root_raw_url"),
        "execution_intent_raw_url_invalid",
        max_length=512,
    )
    canonical_url = _require_string(
        _row_get(row, "root_canonical_url"),
        "execution_intent_canonical_url_invalid",
        max_length=512,
    )
    root_plan_value = _json_object(
        _row_get(row, "root_full_action_plan"),
        "execution_intent_action_plan_invalid",
    )
    try:
        root_plan = validate_action_plan_v2(
            root_plan_value,
            require_executable=True,
            reject_media=True,
        )
    except ActionPlanV2Error as exc:
        raise ExecutionIntentValidationError(
            f"execution_intent_{exc.code}"
        ) from exc
    full_action_plan_hash = _require_hash(
        _row_get(row, "root_full_action_plan_hash"),
        "execution_intent_action_plan_hash_invalid",
    )
    full_required_actions = _normalize_requested_actions(
        root_plan.required_actions,
        _row_get(row, "root_full_required_actions"),
        strict_subset=False,
    )
    full_required_actions_hash = _require_hash(
        _row_get(row, "root_full_required_actions_hash"),
        "execution_intent_required_actions_hash_invalid",
    )
    rule_snapshot_id = _require_positive_int(
        _row_get(row, "root_rule_snapshot_id"),
        "execution_intent_rule_snapshot_id_invalid",
    )
    rule_hash = _require_hash(
        _row_get(row, "root_rule_hash"),
        "execution_intent_rule_hash_invalid",
    )
    execution_path_id = _require_string(
        _row_get(row, "root_execution_path_id"),
        "execution_intent_execution_path_invalid",
        max_length=128,
    )
    target_hash = _require_hash(
        _row_get(row, "root_target_hash"),
        "execution_intent_target_hash_invalid",
    )
    if (
        root_lottery_id != lottery_id
        or root_platform != platform
        or raw_url != str(task.get("raw_url") or "")
        or canonical_url != str(task.get("canonical_url") or "")
        or root_plan.plan_hash != full_action_plan_hash
        or full_required_actions != root_plan.required_actions
        or _canonical_hash(list(full_required_actions))
        != full_required_actions_hash
        or rule_snapshot_id != full_plan.rule_snapshot_id
        or rule_hash != full_plan.rule_hash
        or execution_path_id != full_plan.execution_path_id
        or target_hash != compute_target_hash(canonical_url)
        or full_action_plan_hash != full_plan.plan_hash
        or canonical_json_bytes(root_plan.plan)
        != canonical_json_bytes(full_plan.plan)
    ):
        raise ExecutionIntentValidationError(
            "execution_intent_payload_binding_mismatch"
        )
    payload = {
        "contract_version": EXECUTION_INTENT_CONTRACT_VERSION,
        "intent_id": intent_id,
        "lottery_id": root_lottery_id,
        "source_task_id": source_task_id,
        "source_account_id": source_account_id,
        "platform": root_platform,
        "raw_url": raw_url,
        "canonical_url": canonical_url,
        "full_action_plan": root_plan.plan,
        "full_action_plan_hash": full_action_plan_hash,
        "full_required_actions": list(full_required_actions),
        "full_required_actions_hash": full_required_actions_hash,
        "rule_snapshot_id": rule_snapshot_id,
        "rule_hash": rule_hash,
        "execution_path_id": execution_path_id,
        "target_hash": target_hash,
    }
    if _canonical_hash(payload) != intent_hash:
        raise ExecutionIntentValidationError("execution_intent_hash_mismatch")
    if (
        str(task.get("execution_intent_id") or "").strip() != intent_id
        or str(task.get("execution_intent_hash") or "").strip() != intent_hash
    ):
        raise ExecutionIntentValidationError(
            "execution_intent_message_binding_mismatch"
        )
    return {
        **payload,
        "intent_hash": intent_hash,
        "full_plan": root_plan,
        "dispatch_task_id": task_id,
        "dispatch_account_id": account_id,
    }


def _validate_binding(
    task: Mapping[str, Any],
    row: Any,
    *,
    root: Mapping[str, Any],
    task_id: str,
    account_id: int,
    lottery_id: int,
    expected_evidence_kind: str,
) -> ValidatedTaskExecutionIntent:
    version = _require_positive_int(
        _row_get(row, "binding_contract_version"),
        "execution_intent_binding_contract_version_invalid",
    )
    if version != EXECUTION_INTENT_CONTRACT_VERSION:
        raise ExecutionIntentValidationError(
            "execution_intent_binding_contract_version_invalid"
        )
    binding_task_id = _require_uuid(
        _row_get(row, "binding_task_id"),
        "execution_intent_binding_task_id_invalid",
    )
    binding_intent_id = _require_uuid(
        _row_get(row, "binding_intent_id"),
        "execution_intent_binding_intent_id_invalid",
    )
    binding_lottery_id = _require_positive_int(
        _row_get(row, "binding_lottery_id"),
        "execution_intent_binding_lottery_id_invalid",
    )
    binding_account_id = _require_positive_int(
        _row_get(row, "binding_account_id"),
        "execution_intent_binding_account_id_invalid",
    )
    kind = _require_string(
        _row_get(row, "binding_kind"),
        "execution_intent_binding_kind_invalid",
        max_length=16,
    )
    if kind not in _KINDS:
        raise ExecutionIntentValidationError(
            "execution_intent_binding_kind_invalid"
        )
    if (
        binding_account_id != root["source_account_id"]
        or account_id != root["source_account_id"]
    ):
        raise ExecutionIntentValidationError(
            "execution_intent_repair_account_mismatch"
            if kind == "repair"
            else "execution_intent_full_account_mismatch"
        )
    full_plan = root["full_plan"]
    requested = _normalize_requested_actions(
        tuple(root["full_required_actions"]),
        _row_get(row, "binding_requested_actions"),
        strict_subset=(kind == "repair"),
    )
    requested_hash = _require_hash(
        _row_get(row, "binding_requested_actions_hash"),
        "execution_intent_requested_actions_hash_invalid",
    )
    expected_requested_hash = _canonical_hash(
        {
            "contract_version": EXECUTION_INTENT_CONTRACT_VERSION,
            "execution_intent_id": root["intent_id"],
            "execution_intent_hash": root["intent_hash"],
            "requested_actions": list(requested),
        }
    )
    bound_plan_value = _json_object(
        _row_get(row, "binding_action_plan"),
        "execution_intent_binding_action_plan_invalid",
    )
    try:
        bound_plan = validate_action_plan_v2(
            bound_plan_value,
            require_executable=True,
            reject_media=True,
        )
    except ActionPlanV2Error as exc:
        raise ExecutionIntentValidationError(
            f"execution_intent_binding_{exc.code}"
        ) from exc
    expected_plan = (
        full_plan
        if kind == "full"
        else _derive_subset_plan(full_plan, requested)
    )
    bound_plan_hash = _require_hash(
        _row_get(row, "binding_action_plan_hash"),
        "execution_intent_binding_action_plan_hash_invalid",
    )
    evidence_plan_hash = _require_hash(
        _row_get(row, "binding_evidence_action_plan_hash"),
        "execution_intent_binding_evidence_plan_hash_invalid",
    )
    execution_evidence_id = _require_uuid(
        _row_get(row, "binding_execution_evidence_id"),
        "execution_intent_binding_evidence_id_invalid",
    )
    execution_evidence_kind = _require_string(
        _row_get(row, "binding_execution_evidence_kind"),
        "execution_intent_binding_evidence_kind_invalid",
        max_length=32,
    )
    if (
        execution_evidence_kind not in _EVIDENCE_KINDS
        or expected_evidence_kind not in _EVIDENCE_KINDS
    ):
        raise ExecutionIntentValidationError(
            "execution_intent_binding_evidence_kind_invalid"
        )
    exact_execution_evidence_id = _optional_uuid(
        _row_get(row, "binding_exact_execution_evidence_id"),
        "execution_intent_binding_exact_evidence_id_invalid",
    )
    oauth_calibration_id = _optional_uuid(
        _row_get(row, "binding_oauth_calibration_id"),
        "execution_intent_binding_oauth_calibration_id_invalid",
    )
    execution_path_id = _require_string(
        _row_get(row, "binding_execution_path_id"),
        "execution_intent_binding_execution_path_invalid",
        max_length=128,
    )
    target_hash = _require_hash(
        _row_get(row, "binding_target_hash"),
        "execution_intent_binding_target_hash_invalid",
    )
    config_hash = _require_hash(
        _row_get(row, "binding_config_hash"),
        "execution_intent_binding_config_hash_invalid",
    )
    execution_revision = _require_positive_int(
        _row_get(row, "binding_execution_revision"),
        "execution_intent_binding_execution_revision_invalid",
    )
    lease_id = _require_uuid(
        _row_get(row, "binding_account_lease_id"),
        "execution_intent_binding_lease_id_invalid",
    )
    lease_generation = _require_positive_int(
        _row_get(row, "binding_account_lease_generation"),
        "execution_intent_binding_lease_generation_invalid",
    )
    rule_snapshot_id = _require_positive_int(
        _row_get(row, "binding_rule_snapshot_id"),
        "execution_intent_binding_rule_snapshot_id_invalid",
    )
    rule_hash = _require_hash(
        _row_get(row, "binding_rule_hash"),
        "execution_intent_binding_rule_hash_invalid",
    )
    binding_hash = _require_hash(
        _row_get(row, "binding_hash"),
        "execution_intent_binding_hash_invalid",
    )
    if (
        binding_task_id != task_id
        or binding_intent_id != root["intent_id"]
        or binding_lottery_id != lottery_id
        or binding_account_id != account_id
        or requested_hash != expected_requested_hash
        or bound_plan.required_actions != requested
        or bound_plan.plan_hash != bound_plan_hash
        or canonical_json_bytes(bound_plan.plan)
        != canonical_json_bytes(expected_plan.plan)
        or bound_plan_hash != expected_plan.plan_hash
        or evidence_plan_hash != root["full_action_plan_hash"]
        or rule_snapshot_id != root["rule_snapshot_id"]
        or rule_hash != root["rule_hash"]
        or execution_path_id != root["execution_path_id"]
        or target_hash != root["target_hash"]
        or execution_evidence_id
        != str(task.get("execution_evidence_id") or "").strip()
        or execution_evidence_id
        != str(_row_get(row, "task_execution_evidence_id") or "").strip()
        or execution_evidence_kind != expected_evidence_kind
        or (
            execution_evidence_kind == "exact_execution_evidence"
            and (
                exact_execution_evidence_id != execution_evidence_id
                or oauth_calibration_id is not None
            )
        )
        or (
            execution_evidence_kind == "oauth_account_calibration"
            and (
                oauth_calibration_id != execution_evidence_id
                or exact_execution_evidence_id is not None
            )
        )
        or execution_path_id
        != str(task.get("execution_path_id") or "").strip()
        or execution_path_id
        != str(_row_get(row, "task_execution_path_id") or "").strip()
        or target_hash != str(task.get("target_hash") or "").strip()
        or target_hash != str(_row_get(row, "task_target_hash") or "").strip()
        or config_hash != str(task.get("config_hash") or "").strip()
        or config_hash != str(_row_get(row, "task_config_hash") or "").strip()
        or execution_revision
        != _message_positive_int(
            task.get("execution_revision"),
            "execution_intent_binding_execution_revision_invalid",
        )
        or execution_revision
        != _require_positive_int(
            _row_get(row, "account_execution_revision"),
            "execution_intent_binding_execution_revision_invalid",
        )
        or lease_id != str(task.get("account_lease_id") or "").strip()
        or lease_id
        != str(_row_get(row, "task_account_lease_id") or "").strip()
        or lease_generation
        != _message_positive_int(
            task.get("account_lease_generation"),
            "execution_intent_binding_lease_generation_invalid",
        )
        or lease_generation
        != _require_positive_int(
            _row_get(row, "task_account_lease_generation"),
            "execution_intent_binding_lease_generation_invalid",
        )
    ):
        raise ExecutionIntentValidationError(
            "execution_intent_binding_mismatch"
        )
    message_requested = _normalize_requested_actions(
        tuple(root["full_required_actions"]),
        task.get("requested_actions"),
        strict_subset=(kind == "repair"),
    )
    if (
        str(task.get("execution_intent_kind") or "").strip() != kind
        or str(task.get("execution_intent_binding_hash") or "").strip()
        != binding_hash
        or message_requested != requested
        or str(task.get("requested_actions_hash") or "").strip()
        != requested_hash
        or str(task.get("requested_action_plan_hash") or "").strip()
        != bound_plan_hash
        or str(task.get("execution_evidence_kind") or "").strip()
        != execution_evidence_kind
        or str(task.get("exact_execution_evidence_id") or "").strip()
        != str(exact_execution_evidence_id or "")
        or str(task.get("oauth_calibration_id") or "").strip()
        != str(oauth_calibration_id or "")
    ):
        raise ExecutionIntentValidationError(
            "execution_intent_message_binding_mismatch"
        )
    hash_payload = {
        "contract_version": EXECUTION_INTENT_CONTRACT_VERSION,
        "task_id": task_id,
        "execution_intent_id": root["intent_id"],
        "execution_intent_hash": root["intent_hash"],
        "lottery_id": lottery_id,
        "account_id": account_id,
        "binding_kind": kind,
        "requested_actions": list(requested),
        "requested_actions_hash": requested_hash,
        "bound_action_plan": bound_plan.plan,
        "bound_action_plan_hash": bound_plan_hash,
        "evidence_action_plan_hash": evidence_plan_hash,
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
        "account_lease_id": lease_id,
        "account_lease_generation": lease_generation,
    }
    if _canonical_hash(hash_payload) != binding_hash:
        raise ExecutionIntentValidationError(
            "execution_intent_binding_hash_mismatch"
        )
    return ValidatedTaskExecutionIntent(
        intent_id=root["intent_id"],
        intent_hash=root["intent_hash"],
        binding_kind=kind,
        binding_hash=binding_hash,
        requested_actions=requested,
        action_plan=bound_plan,
    )


def validate_task_execution_intent(
    task: Mapping[str, Any],
    row: Any,
    *,
    task_id: str,
    account_id: int,
    lottery_id: int,
    platform: str,
    full_plan: ValidatedActionPlanV2,
    expected_evidence_kind: str,
) -> ValidatedTaskExecutionIntent:
    """Validate a new durable binding or a narrowly scoped legacy full task."""

    root_present = _db_contract_present(row, "root")
    binding_present = _db_contract_present(row, "binding")
    message_present = _message_contract_present(task)
    if not root_present and not binding_present and not message_present:
        # Migration intentionally cannot reconstruct old queued full intents.
        # Absence alone is fail-open, so require the immutable legacy Outbox
        # authority and Core fanout provenance before preserving that narrow
        # compatibility path.
        _validate_legacy_full_authority(
            task,
            row,
            task_id=task_id,
            account_id=account_id,
            lottery_id=lottery_id,
            platform=platform,
            full_plan=full_plan,
        )
        return ValidatedTaskExecutionIntent(
            intent_id=None,
            intent_hash=None,
            binding_kind="legacy_full",
            binding_hash=None,
            requested_actions=full_plan.required_actions,
            action_plan=full_plan,
            legacy=True,
        )
    if not root_present:
        raise ExecutionIntentValidationError("execution_intent_root_missing")
    if not binding_present:
        raise ExecutionIntentValidationError("execution_intent_binding_missing")
    if not message_present:
        raise ExecutionIntentValidationError("execution_intent_message_missing")
    root = _validate_root(
        task,
        row,
        task_id=task_id,
        account_id=account_id,
        lottery_id=lottery_id,
        platform=platform,
        full_plan=full_plan,
    )
    return _validate_binding(
        task,
        row,
        root=root,
        task_id=task_id,
        account_id=account_id,
        lottery_id=lottery_id,
        expected_evidence_kind=expected_evidence_kind,
    )
