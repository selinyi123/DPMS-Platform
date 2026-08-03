"""Materialize immutable Probe + Shadow evidence for one exact API contract."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from app.action_plan import (
    BILIBILI_API_EXECUTION_PATH,
    XIAOHONGSHU_BROWSER_EXECUTION_PATH,
    ActionPlanV2Error,
    compute_bilibili_api_config_hash,
    compute_rule_hash,
    compute_target_hash,
    validate_action_plan_v2,
)
from app.bilibili.preflight import (
    API_PREFLIGHT_KIND,
    validate_preflight_observation,
)
from app.bilibili.runtime import extract_bilibili_dynamic_id
from shared.xiaohongshu_browser_contract import (
    XIAOHONGSHU_BROWSER_PROBE_OBSERVATION_KIND,
    XIAOHONGSHU_BROWSER_SHADOW_OBSERVATION_KIND,
    XiaohongshuBrowserContractError,
    compute_xiaohongshu_browser_config_hash,
    compute_xiaohongshu_comment_text_hash,
    validate_xiaohongshu_browser_observation,
)
from shared.douyin_device_contract import (
    DOUYIN_DEVICE_EXECUTION_PATH,
    DOUYIN_DEVICE_PROBE_OBSERVATION_KIND,
    DOUYIN_DEVICE_SHADOW_OBSERVATION_KIND,
    DouyinDeviceContractError,
    compute_douyin_device_config_hash,
    compute_douyin_exact_text_hash,
    normalize_douyin_device_public_config,
    validate_douyin_device_observation,
)


API_PROBE_KIND = API_PREFLIGHT_KIND
class EvidenceDatabase(Protocol):
    def transaction(self): ...

    async def fetch_one(self, query: str, values: Mapping[str, Any] | None = None): ...

    async def execute(self, query: str, values: Mapping[str, Any] | None = None): ...


@dataclass(frozen=True)
class EvidenceContract:
    lottery_id: int
    account_id: int
    platform: str
    rule_snapshot_id: int
    execution_path_id: str
    target_hash: str
    rule_hash: str
    action_plan_hash: str
    config_hash: str


def _row_get(row: Any, key: str, default=None):
    if row is None:
        return default
    if hasattr(row, "get"):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, TypeError):
        return default


def _positive_int(value: Any) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("execution_evidence_binding_invalid") from exc
    if result <= 0:
        raise ValueError("execution_evidence_binding_invalid")
    return result


def _contract_from_row(row: Any) -> EvidenceContract:
    contract = EvidenceContract(
        lottery_id=_positive_int(_row_get(row, "lottery_id")),
        account_id=_positive_int(_row_get(row, "account_id")),
        platform=str(_row_get(row, "platform") or "").strip().lower(),
        rule_snapshot_id=_positive_int(_row_get(row, "rule_snapshot_id")),
        execution_path_id=str(_row_get(row, "execution_path_id") or "").strip(),
        target_hash=str(_row_get(row, "target_hash") or "").strip(),
        rule_hash=str(_row_get(row, "rule_hash") or "").strip(),
        action_plan_hash=str(_row_get(row, "action_plan_hash") or "").strip(),
        config_hash=str(_row_get(row, "config_hash") or "").strip(),
    )
    hashes = (
        contract.target_hash,
        contract.rule_hash,
        contract.action_plan_hash,
        contract.config_hash,
    )
    valid_execution_path = (
        contract.platform == "bilibili"
        and contract.execution_path_id == BILIBILI_API_EXECUTION_PATH
    ) or (
        contract.platform == "xiaohongshu"
        and contract.execution_path_id
        == XIAOHONGSHU_BROWSER_EXECUTION_PATH
    ) or (
        contract.platform == "douyin"
        and contract.execution_path_id == DOUYIN_DEVICE_EXECUTION_PATH
    )
    if (
        not valid_execution_path
        or any(
            len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
            for value in hashes
        )
    ):
        raise ValueError("execution_evidence_binding_invalid")
    return contract


def _same_contract(left: EvidenceContract, right: EvidenceContract) -> bool:
    return left == right


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="strict")
    if isinstance(value, str):
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("execution_evidence_json_invalid")


def _probe_proves_api_path(
    row: Any,
    contract: EvidenceContract | None = None,
    *,
    expected_dynamic_id: str | None = None,
    expected_actions: tuple[str, ...] | None = None,
    expected_execution_revision: int | None = None,
    expected_follow_handle: str | None = None,
    expected_xiaohongshu: Mapping[str, Any] | None = None,
    expected_douyin: Mapping[str, Any] | None = None,
) -> bool:
    if contract is not None and contract.platform == "xiaohongshu":
        return _xiaohongshu_source_proves_browser_path(
            row,
            contract,
            observation_field="result",
            observation_kind_field="observation_kind",
            observation_hash_field="observation_hash",
            expected_kind=XIAOHONGSHU_BROWSER_PROBE_OBSERVATION_KIND,
            evidence_id_field="probe_id",
            expected=expected_xiaohongshu,
        )
    if contract is not None and contract.platform == "douyin":
        return _douyin_source_proves_device_path(
            row,
            contract,
            observation_field="result",
            observation_kind_field="observation_kind",
            observation_hash_field="observation_hash",
            expected_kind=DOUYIN_DEVICE_PROBE_OBSERVATION_KIND,
            evidence_id_field="probe_id",
            expected=expected_douyin,
        )
    try:
        result = _json_object(_row_get(row, "result"))
        dynamic_id = (
            expected_dynamic_id
            if expected_dynamic_id is not None
            else str(result.get("requested_dynamic_id") or "")
        )
        actions = (
            expected_actions
            if expected_actions is not None
            else tuple(result.get("required_actions") or ())
        )
        execution_revision = (
            expected_execution_revision
            if expected_execution_revision is not None
            else result.get("execution_revision")
        )
        config_hash = (
            contract.config_hash if contract else str(result.get("config_hash") or "")
        )
        follow_handle = (
            expected_follow_handle
            if expected_follow_handle is not None
            else str(result.get("follow_target_handle") or "")
        )
        preflight = validate_preflight_observation(
            result,
            expected_dynamic_id=dynamic_id,
            expected_actions=actions,
            expected_execution_revision=execution_revision,
            expected_config_hash=config_hash,
            expected_follow_handle=follow_handle,
        )
    except (TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return bool(
        preflight.observation_hash
        == str(_row_get(row, "observation_hash") or "").strip()
        and str(_row_get(row, "observation_kind") or "").strip()
        == API_PROBE_KIND
    )


def _shadow_proves_api_path(
    row: Any,
    contract: EvidenceContract | None = None,
    *,
    expected_dynamic_id: str | None = None,
    expected_actions: tuple[str, ...] | None = None,
    expected_execution_revision: int | None = None,
    expected_follow_handle: str | None = None,
    expected_xiaohongshu: Mapping[str, Any] | None = None,
    expected_douyin: Mapping[str, Any] | None = None,
) -> bool:
    if contract is not None and contract.platform == "xiaohongshu":
        return _xiaohongshu_source_proves_browser_path(
            row,
            contract,
            observation_field="preflight_observation",
            observation_kind_field="preflight_observation_kind",
            observation_hash_field="preflight_observation_hash",
            expected_kind=XIAOHONGSHU_BROWSER_SHADOW_OBSERVATION_KIND,
            evidence_id_field="task_id",
            expected=expected_xiaohongshu,
        )
    if contract is not None and contract.platform == "douyin":
        return _douyin_source_proves_device_path(
            row,
            contract,
            observation_field="preflight_observation",
            observation_kind_field="preflight_observation_kind",
            observation_hash_field="preflight_observation_hash",
            expected_kind=DOUYIN_DEVICE_SHADOW_OBSERVATION_KIND,
            evidence_id_field="task_id",
            expected=expected_douyin,
        )
    try:
        result = _json_object(_row_get(row, "preflight_observation"))
        dynamic_id = (
            expected_dynamic_id
            if expected_dynamic_id is not None
            else str(result.get("requested_dynamic_id") or "")
        )
        actions = (
            expected_actions
            if expected_actions is not None
            else tuple(result.get("required_actions") or ())
        )
        execution_revision = (
            expected_execution_revision
            if expected_execution_revision is not None
            else result.get("execution_revision")
        )
        config_hash = (
            contract.config_hash if contract else str(result.get("config_hash") or "")
        )
        follow_handle = (
            expected_follow_handle
            if expected_follow_handle is not None
            else str(result.get("follow_target_handle") or "")
        )
        preflight = validate_preflight_observation(
            result,
            expected_dynamic_id=dynamic_id,
            expected_actions=actions,
            expected_execution_revision=execution_revision,
            expected_config_hash=config_hash,
            expected_follow_handle=follow_handle,
        )
    except (TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return bool(
        preflight.observation_hash
        == str(_row_get(row, "preflight_observation_hash") or "").strip()
        and str(_row_get(row, "preflight_observation_kind") or "").strip()
        == API_PROBE_KIND
    )


def _xiaohongshu_source_proves_browser_path(
    row: Any,
    contract: EvidenceContract,
    *,
    observation_field: str,
    observation_kind_field: str,
    observation_hash_field: str,
    expected_kind: str,
    evidence_id_field: str,
    expected: Mapping[str, Any] | None,
) -> bool:
    """Validate one XHS source against either its row or locked authority."""

    try:
        observation = _json_object(_row_get(row, observation_field))
        expected_values = dict(expected or {})
        if not expected_values:
            expected_values = {
                "expected_lottery_id": contract.lottery_id,
                "expected_account_id": contract.account_id,
                "expected_execution_revision": observation.get(
                    "execution_revision"
                ),
                "expected_target_hash": contract.target_hash,
                "expected_rule_snapshot_id": contract.rule_snapshot_id,
                "expected_rule_hash": contract.rule_hash,
                "expected_action_plan_hash": contract.action_plan_hash,
                "expected_config_hash": contract.config_hash,
                "expected_actions": observation.get("required_actions"),
                "expected_follow_target_handle": observation.get(
                    "follow_target_handle", ""
                ),
                "expected_comment_text_hash": observation.get(
                    "comment_text_hash", ""
                ),
            }
        validated = validate_xiaohongshu_browser_observation(
            observation,
            expected_observation_kind=expected_kind,
            expected_evidence_id=str(
                _row_get(row, evidence_id_field) or ""
            ).strip(),
            **expected_values,
        )
    except (
        XiaohongshuBrowserContractError,
        TypeError,
        ValueError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ):
        return False
    return bool(
        validated.observation_hash
        == str(_row_get(row, observation_hash_field) or "").strip()
        and str(_row_get(row, observation_kind_field) or "").strip()
        == expected_kind
    )


def _douyin_source_proves_device_path(
    row: Any,
    contract: EvidenceContract,
    *,
    observation_field: str,
    observation_kind_field: str,
    observation_hash_field: str,
    expected_kind: str,
    evidence_id_field: str,
    expected: Mapping[str, Any] | None,
) -> bool:
    try:
        observation = _json_object(_row_get(row, observation_field))
        expected_values = dict(expected or {})
        if not expected_values:
            expected_values = {
                "expected_lottery_id": contract.lottery_id,
                "expected_account_id": contract.account_id,
                "expected_execution_revision": observation.get("execution_revision"),
                "expected_target_hash": contract.target_hash,
                "expected_rule_snapshot_id": contract.rule_snapshot_id,
                "expected_rule_hash": contract.rule_hash,
                "expected_action_plan_hash": contract.action_plan_hash,
                "expected_config_hash": contract.config_hash,
                "expected_actions": observation.get("required_actions"),
                "expected_follow_target_handle_hash": observation.get(
                    "follow_target_handle_hash", ""
                ),
                "expected_comment_text_hash": observation.get(
                    "comment_text_hash", ""
                ),
                "expected_public_config": {
                    key: observation.get(key)
                    for key in (
                        "agent_id",
                        "manifest_sha256",
                        "device_serial_sha256",
                        "account_id_sha256",
                    )
                },
            }
        validated = validate_douyin_device_observation(
            observation,
            expected_observation_kind=expected_kind,
            expected_evidence_id=str(_row_get(row, evidence_id_field) or "").strip(),
            **expected_values,
        )
    except (
        DouyinDeviceContractError,
        TypeError,
        ValueError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ):
        return False
    return bool(
        validated.observation_hash
        == str(_row_get(row, observation_hash_field) or "").strip()
        and str(_row_get(row, observation_kind_field) or "").strip()
        == expected_kind
    )


def _released_fresh_source(row: Any) -> bool:
    return bool(
        int(_row_get(row, "source_fresh", 0) or 0) == 1
        and int(_row_get(row, "source_lease_released", 0) or 0) == 1
        and int(_row_get(row, "source_lease_covers_observation", 0) or 0) == 1
    )


async def materialize_for_probe(
    *, db: EvidenceDatabase, probe_id: str
) -> str | None:
    row = await db.fetch_one(
        """SELECT ac.probe_id, ac.lottery_id, ac.account_id, ac.platform, ac.rule_snapshot_id,
                  execution_path_id, target_hash, rule_hash, action_plan_hash,
                  config_hash, status, result, observation_kind,
                  observation_hash, finished_at,
                  CASE WHEN ac.finished_at >= DATE_SUB(NOW(), INTERVAL 24 HOUR)
                             AND ac.finished_at <= NOW() THEN 1 ELSE 0 END AS source_fresh,
                  CASE WHEN lease.released_at >= ac.finished_at
                             AND lease.released_at <= NOW()
                             AND lease.owner_id = ac.probe_id
                             AND lease.task_id IS NULL
                             AND lease.operation_kind = 'adapter_probe'
                       THEN 1 ELSE 0 END AS source_lease_released,
                  CASE WHEN ac.started_at IS NOT NULL
                             AND lease.acquired_at <= ac.started_at
                             AND ac.started_at <= ac.finished_at
                             AND lease.expires_at >= ac.finished_at
                             AND lease.released_at >= ac.finished_at
                             AND lease.released_at <= NOW()
                       THEN 1 ELSE 0 END AS source_lease_covers_observation
           FROM adapter_calibrations ac
           LEFT JOIN account_operation_leases lease
             ON lease.lease_id = ac.account_lease_id
            AND lease.account_id = ac.account_id
            AND lease.generation = ac.account_lease_generation
           WHERE ac.probe_id = :probe_id""",
        {"probe_id": str(probe_id or "").strip()},
    )
    if (
        row is None
        or str(_row_get(row, "status") or "").strip().lower() != "succeeded"
        or _row_get(row, "finished_at") is None
        or not _released_fresh_source(row)
        or not _probe_proves_api_path(row, _contract_from_row(row))
    ):
        return None
    return await _materialize(db=db, contract=_contract_from_row(row), probe_id=probe_id)


async def materialize_for_shadow_task(
    *, db: EvidenceDatabase, task_id: str
) -> str | None:
    row = await db.fetch_one(
        """SELECT tr.task_id, tr.lottery_id, tr.account_id, l.platform,
                  tr.rule_snapshot_id, tr.execution_path_id, tr.target_hash,
                  tr.rule_hash, tr.action_plan_hash, tr.config_hash,
                  tr.status, tr.task_mode, tr.preflight_observation,
                  tr.preflight_observation_kind, tr.preflight_observation_hash,
                  tr.finished_at,
                  CASE WHEN tr.finished_at >= DATE_SUB(NOW(), INTERVAL 24 HOUR)
                             AND tr.finished_at <= NOW() THEN 1 ELSE 0 END AS source_fresh,
                  CASE WHEN lease.released_at >= tr.finished_at
                             AND lease.released_at <= NOW()
                             AND lease.owner_id = tr.task_id
                             AND lease.task_id = tr.task_id
                             AND lease.operation_kind = 'shadow_run'
                       THEN 1 ELSE 0 END AS source_lease_released,
                  CASE WHEN tr.started_at IS NOT NULL
                             AND lease.acquired_at <= tr.started_at
                             AND tr.started_at <= tr.finished_at
                             AND lease.expires_at >= tr.finished_at
                             AND lease.released_at >= tr.finished_at
                             AND lease.released_at <= NOW()
                       THEN 1 ELSE 0 END AS source_lease_covers_observation
           FROM task_runs tr
           JOIN lotteries l ON l.id = tr.lottery_id
           LEFT JOIN account_operation_leases lease
             ON lease.lease_id = tr.account_lease_id
            AND lease.account_id = tr.account_id
            AND lease.generation = tr.account_lease_generation
           WHERE tr.task_id = :task_id""",
        {"task_id": str(task_id or "").strip()},
    )
    if (
        row is None
        or str(_row_get(row, "status") or "").strip().lower() != "succeeded"
        or str(_row_get(row, "task_mode") or "").strip().lower() != "shadow_run"
        or _row_get(row, "finished_at") is None
        or not _released_fresh_source(row)
        or not _shadow_proves_api_path(row, _contract_from_row(row))
    ):
        return None
    return await _materialize(db=db, contract=_contract_from_row(row), shadow_task_id=task_id)


async def _materialize(
    *,
    db: EvidenceDatabase,
    contract: EvidenceContract,
    probe_id: str | None = None,
    shadow_task_id: str | None = None,
) -> str | None:
    """Create one immutable 24-hour evidence row for a terminal source pair."""

    async with db.transaction():
        authority = await db.fetch_one(
            """SELECT l.id AS lottery_id, l.platform, l.raw_url, l.canonical_url,
                      l.rule_text AS lottery_rule_text, l.action_plan,
                      l.authoritative_rule_snapshot_id AS rule_snapshot_id,
                      l.rule_hash, l.action_plan_hash,
                      rs.rule_text AS snapshot_rule_text,
                      rs.is_complete, rs.attested_by, rs.attested_at,
                      a.platform AS account_platform, a.status AS account_status,
                      a.execution_revision,
                      selector_config.config_json AS selector_config_json
               FROM lotteries l
               JOIN lottery_rule_snapshots rs
                 ON rs.id = l.authoritative_rule_snapshot_id
                AND rs.lottery_id = l.id
               JOIN accounts a ON a.id = :account_id
               LEFT JOIN adapter_selector_configs selector_config
                 ON selector_config.platform = l.platform
               WHERE l.id = :lottery_id
               FOR UPDATE""",
            {"lottery_id": contract.lottery_id, "account_id": contract.account_id},
        )
        if authority is None:
            return None
        try:
            execution_revision = _positive_int(
                _row_get(authority, "execution_revision")
            )
            canonical_url = str(_row_get(authority, "canonical_url") or "").strip()
            authority_platform = str(
                _row_get(authority, "platform") or ""
            ).strip().lower()
            selector_config = None
            if authority_platform in {"xiaohongshu", "douyin"}:
                selector_config = _json_object(
                    _row_get(authority, "selector_config_json")
                )
                if authority_platform == "xiaohongshu":
                    authority_execution_path = XIAOHONGSHU_BROWSER_EXECUTION_PATH
                    authority_config_hash = compute_xiaohongshu_browser_config_hash(
                        execution_revision,
                        selector_config,
                    )
                else:
                    authority_execution_path = DOUYIN_DEVICE_EXECUTION_PATH
                    authority_config_hash = compute_douyin_device_config_hash(
                        execution_revision,
                        selector_config,
                    )
                dynamic_id = None
            else:
                dynamic_id = extract_bilibili_dynamic_id(
                    _row_get(authority, "raw_url"), canonical_url
                )
                authority_execution_path = BILIBILI_API_EXECUTION_PATH
                authority_config_hash = compute_bilibili_api_config_hash(
                    execution_revision
                )
            authority_contract = EvidenceContract(
                lottery_id=_positive_int(_row_get(authority, "lottery_id")),
                account_id=contract.account_id,
                platform=str(_row_get(authority, "platform") or "").strip().lower(),
                rule_snapshot_id=_positive_int(_row_get(authority, "rule_snapshot_id")),
                execution_path_id=authority_execution_path,
                target_hash=compute_target_hash(canonical_url),
                rule_hash=str(_row_get(authority, "rule_hash") or "").strip(),
                action_plan_hash=str(_row_get(authority, "action_plan_hash") or "").strip(),
                config_hash=authority_config_hash,
            )
            plan = validate_action_plan_v2(_row_get(authority, "action_plan"), reject_media=True)
            lottery_rule = _row_get(authority, "lottery_rule_text")
            snapshot_rule = _row_get(authority, "snapshot_rule_text")
            if isinstance(lottery_rule, bytes):
                lottery_rule = lottery_rule.decode("utf-8", errors="strict")
            if isinstance(snapshot_rule, bytes):
                snapshot_rule = snapshot_rule.decode("utf-8", errors="strict")
        except (
            ActionPlanV2Error,
            DouyinDeviceContractError,
            XiaohongshuBrowserContractError,
            TypeError,
            ValueError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ):
            return None
        if (
            not _same_contract(contract, authority_contract)
            or plan.rule_snapshot_id != contract.rule_snapshot_id
            or plan.rule_hash != contract.rule_hash
            or plan.plan_hash != contract.action_plan_hash
            or plan.execution_path_id != contract.execution_path_id
            or str(plan.plan.get("platform") or "").strip().lower() != contract.platform
            or not isinstance(lottery_rule, str)
            or lottery_rule != snapshot_rule
            or compute_rule_hash(lottery_rule) != contract.rule_hash
            or int(_row_get(authority, "is_complete", 0) or 0) != 1
            or not str(_row_get(authority, "attested_by") or "").strip()
            or _row_get(authority, "attested_at") is None
            or str(_row_get(authority, "account_platform") or "").strip().lower()
            != contract.platform
            or str(_row_get(authority, "account_status") or "").strip().lower()
            != "ready"
        ):
            return None
        if contract.platform == "xiaohongshu":
            comment_text = (
                plan.payload_for("commented").get("text", "")
                if "commented" in plan.required_actions
                else ""
            )
            source_validation = {
                "expected_xiaohongshu": {
                    "expected_lottery_id": contract.lottery_id,
                    "expected_account_id": contract.account_id,
                    "expected_execution_revision": execution_revision,
                    "expected_target_hash": contract.target_hash,
                    "expected_rule_snapshot_id": contract.rule_snapshot_id,
                    "expected_rule_hash": contract.rule_hash,
                    "expected_action_plan_hash": contract.action_plan_hash,
                    "expected_config_hash": contract.config_hash,
                    "expected_actions": plan.required_actions,
                    "expected_follow_target_handle": (
                        plan.follow_target_handle
                        if "followed" in plan.required_actions
                        else ""
                    ),
                    "expected_comment_text_hash": (
                        compute_xiaohongshu_comment_text_hash(comment_text)
                    ),
                }
            }
        elif contract.platform == "douyin":
            follow_text = (
                plan.follow_target_handle
                if "followed" in plan.required_actions
                else ""
            )
            comment_text = (
                plan.payload_for("commented").get("text", "")
                if "commented" in plan.required_actions
                else ""
            )
            source_validation = {
                "expected_douyin": {
                    "expected_lottery_id": contract.lottery_id,
                    "expected_account_id": contract.account_id,
                    "expected_execution_revision": execution_revision,
                    "expected_target_hash": contract.target_hash,
                    "expected_rule_snapshot_id": contract.rule_snapshot_id,
                    "expected_rule_hash": contract.rule_hash,
                    "expected_action_plan_hash": contract.action_plan_hash,
                    "expected_config_hash": contract.config_hash,
                    "expected_actions": plan.required_actions,
                    "expected_follow_target_handle_hash": (
                        compute_douyin_exact_text_hash(follow_text)
                    ),
                    "expected_comment_text_hash": (
                        compute_douyin_exact_text_hash(comment_text)
                    ),
                    "expected_public_config": (
                        normalize_douyin_device_public_config(selector_config)
                    ),
                }
            }
        else:
            source_validation = {
                "expected_dynamic_id": dynamic_id,
                "expected_actions": plan.required_actions,
                "expected_execution_revision": execution_revision,
                "expected_follow_handle": (
                    plan.follow_target_handle
                    if "followed" in plan.required_actions
                    else None
                ),
            }

        if probe_id is None:
            probe = await db.fetch_one(
                """SELECT ac.probe_id, ac.lottery_id, ac.account_id, ac.platform,
                          rule_snapshot_id, execution_path_id, target_hash,
                          rule_hash, action_plan_hash, config_hash,
                          status, result, observation_kind, observation_hash,
                          finished_at,
                          CASE WHEN ac.finished_at >= DATE_SUB(NOW(), INTERVAL 24 HOUR)
                                     AND ac.finished_at <= NOW() THEN 1 ELSE 0 END AS source_fresh,
                           CASE WHEN lease.released_at >= ac.finished_at
                                      AND lease.released_at <= NOW()
                                      AND lease.owner_id = ac.probe_id
                                     AND lease.task_id IS NULL
                                     AND lease.operation_kind = 'adapter_probe'
                               THEN 1 ELSE 0 END AS source_lease_released,
                          CASE WHEN ac.started_at IS NOT NULL
                                     AND lease.acquired_at <= ac.started_at
                                     AND ac.started_at <= ac.finished_at
                                     AND lease.expires_at >= ac.finished_at
                                     AND lease.released_at >= ac.finished_at
                                     AND lease.released_at <= NOW()
                               THEN 1 ELSE 0 END AS source_lease_covers_observation
                   FROM adapter_calibrations ac
                   JOIN account_operation_leases lease
                     ON lease.lease_id = ac.account_lease_id
                    AND lease.account_id = ac.account_id
                    AND lease.generation = ac.account_lease_generation
                   WHERE ac.lottery_id = :lottery_id AND ac.account_id = :account_id
                     AND ac.platform = :platform
                     AND ac.rule_snapshot_id = :rule_snapshot_id
                     AND ac.execution_path_id = :execution_path_id
                     AND ac.target_hash = :target_hash AND ac.rule_hash = :rule_hash
                     AND ac.action_plan_hash = :action_plan_hash
                     AND ac.config_hash = :config_hash AND ac.status = 'succeeded'
                     AND ac.finished_at >= DATE_SUB(NOW(), INTERVAL 24 HOUR)
                      AND ac.finished_at <= NOW()
                      AND lease.released_at >= ac.finished_at
                      AND lease.released_at <= NOW()
                   ORDER BY ac.finished_at DESC, ac.id DESC LIMIT 1
                   FOR UPDATE""",
                contract.__dict__,
            )
            if (
                probe is None
                or not _released_fresh_source(probe)
                or not _probe_proves_api_path(probe, contract, **source_validation)
            ):
                return None
            probe_id = str(_row_get(probe, "probe_id") or "").strip()
        else:
            probe = await db.fetch_one(
                """SELECT ac.probe_id, ac.lottery_id, ac.account_id, ac.platform,
                          rule_snapshot_id, execution_path_id, target_hash,
                          rule_hash, action_plan_hash, config_hash,
                          status, result, observation_kind, observation_hash,
                          finished_at,
                          CASE WHEN ac.finished_at >= DATE_SUB(NOW(), INTERVAL 24 HOUR)
                                     AND ac.finished_at <= NOW() THEN 1 ELSE 0 END AS source_fresh,
                           CASE WHEN lease.released_at >= ac.finished_at
                                      AND lease.released_at <= NOW()
                                      AND lease.owner_id = ac.probe_id
                                     AND lease.task_id IS NULL
                                     AND lease.operation_kind = 'adapter_probe'
                               THEN 1 ELSE 0 END AS source_lease_released,
                          CASE WHEN ac.started_at IS NOT NULL
                                     AND lease.acquired_at <= ac.started_at
                                     AND ac.started_at <= ac.finished_at
                                     AND lease.expires_at >= ac.finished_at
                                     AND lease.released_at >= ac.finished_at
                                     AND lease.released_at <= NOW()
                               THEN 1 ELSE 0 END AS source_lease_covers_observation
                   FROM adapter_calibrations ac
                   JOIN account_operation_leases lease
                     ON lease.lease_id = ac.account_lease_id
                    AND lease.account_id = ac.account_id
                    AND lease.generation = ac.account_lease_generation
                   WHERE ac.probe_id = :probe_id AND ac.status = 'succeeded'
                     AND ac.finished_at >= DATE_SUB(NOW(), INTERVAL 24 HOUR)
                      AND ac.finished_at <= NOW()
                      AND lease.released_at >= ac.finished_at
                      AND lease.released_at <= NOW()
                   FOR UPDATE""",
                {"probe_id": probe_id},
            )
            if (
                probe is None
                or not _same_contract(contract, _contract_from_row(probe))
                or not _released_fresh_source(probe)
                or not _probe_proves_api_path(probe, contract, **source_validation)
            ):
                return None

        if shadow_task_id is None:
            shadow = await db.fetch_one(
                """SELECT tr.task_id, tr.lottery_id, tr.account_id, :platform AS platform,
                          tr.rule_snapshot_id, tr.execution_path_id, tr.target_hash,
                          tr.rule_hash, tr.action_plan_hash, tr.config_hash,
                          tr.status, tr.task_mode, tr.preflight_observation,
                          tr.preflight_observation_kind, tr.preflight_observation_hash,
                          tr.finished_at,
                          CASE WHEN tr.finished_at >= DATE_SUB(NOW(), INTERVAL 24 HOUR)
                                     AND tr.finished_at <= NOW() THEN 1 ELSE 0 END AS source_fresh,
                           CASE WHEN lease.released_at >= tr.finished_at
                                      AND lease.released_at <= NOW()
                                      AND lease.owner_id = tr.task_id
                                     AND lease.task_id = tr.task_id
                                     AND lease.operation_kind = 'shadow_run'
                               THEN 1 ELSE 0 END AS source_lease_released,
                          CASE WHEN tr.started_at IS NOT NULL
                                     AND lease.acquired_at <= tr.started_at
                                     AND tr.started_at <= tr.finished_at
                                     AND lease.expires_at >= tr.finished_at
                                     AND lease.released_at >= tr.finished_at
                                     AND lease.released_at <= NOW()
                               THEN 1 ELSE 0 END AS source_lease_covers_observation
                   FROM task_runs tr
                   JOIN account_operation_leases lease
                     ON lease.lease_id = tr.account_lease_id
                    AND lease.account_id = tr.account_id
                    AND lease.generation = tr.account_lease_generation
                   WHERE tr.lottery_id = :lottery_id AND tr.account_id = :account_id
                     AND tr.rule_snapshot_id = :rule_snapshot_id
                     AND tr.execution_path_id = :execution_path_id
                     AND tr.target_hash = :target_hash AND tr.rule_hash = :rule_hash
                     AND tr.action_plan_hash = :action_plan_hash
                     AND tr.config_hash = :config_hash
                     AND tr.task_mode = 'shadow_run' AND tr.status = 'succeeded'
                     AND tr.finished_at >= DATE_SUB(NOW(), INTERVAL 24 HOUR)
                      AND tr.finished_at <= NOW()
                      AND lease.released_at >= tr.finished_at
                      AND lease.released_at <= NOW()
                   ORDER BY tr.finished_at DESC, tr.id DESC LIMIT 1
                   FOR UPDATE""",
                contract.__dict__,
            )
            if (
                shadow is None
                or not _released_fresh_source(shadow)
                or not _shadow_proves_api_path(shadow, contract, **source_validation)
            ):
                return None
            shadow_task_id = str(_row_get(shadow, "task_id") or "").strip()
        else:
            shadow = await db.fetch_one(
                """SELECT tr.task_id, tr.lottery_id, tr.account_id, l.platform,
                          tr.rule_snapshot_id, tr.execution_path_id, tr.target_hash,
                          tr.rule_hash, tr.action_plan_hash, tr.config_hash,
                          tr.status, tr.task_mode, tr.preflight_observation,
                          tr.preflight_observation_kind, tr.preflight_observation_hash,
                          tr.finished_at,
                          CASE WHEN tr.finished_at >= DATE_SUB(NOW(), INTERVAL 24 HOUR)
                                     AND tr.finished_at <= NOW() THEN 1 ELSE 0 END AS source_fresh,
                           CASE WHEN lease.released_at >= tr.finished_at
                                      AND lease.released_at <= NOW()
                                      AND lease.owner_id = tr.task_id
                                     AND lease.task_id = tr.task_id
                                     AND lease.operation_kind = 'shadow_run'
                               THEN 1 ELSE 0 END AS source_lease_released,
                          CASE WHEN tr.started_at IS NOT NULL
                                     AND lease.acquired_at <= tr.started_at
                                     AND tr.started_at <= tr.finished_at
                                     AND lease.expires_at >= tr.finished_at
                                     AND lease.released_at >= tr.finished_at
                                     AND lease.released_at <= NOW()
                               THEN 1 ELSE 0 END AS source_lease_covers_observation
                   FROM task_runs tr
                   JOIN lotteries l ON l.id = tr.lottery_id
                   JOIN account_operation_leases lease
                     ON lease.lease_id = tr.account_lease_id
                    AND lease.account_id = tr.account_id
                    AND lease.generation = tr.account_lease_generation
                   WHERE tr.task_id = :task_id AND tr.task_mode = 'shadow_run'
                     AND tr.status = 'succeeded'
                     AND tr.finished_at >= DATE_SUB(NOW(), INTERVAL 24 HOUR)
                      AND tr.finished_at <= NOW()
                      AND lease.released_at >= tr.finished_at
                      AND lease.released_at <= NOW()
                   FOR UPDATE""",
                {"task_id": shadow_task_id},
            )
            if (
                shadow is None
                or not _same_contract(contract, _contract_from_row(shadow))
                or not _released_fresh_source(shadow)
                or not _shadow_proves_api_path(shadow, contract, **source_validation)
            ):
                return None

        if not probe_id or not shadow_task_id:
            return None
        probe_observation_hash = str(_row_get(probe, "observation_hash") or "").strip()
        probe_observation_kind = str(_row_get(probe, "observation_kind") or "").strip()
        shadow_observation_hash = str(
            _row_get(shadow, "preflight_observation_hash") or ""
        ).strip()
        shadow_observation_kind = str(
            _row_get(shadow, "preflight_observation_kind") or ""
        ).strip()
        expected_probe_kind = (
            XIAOHONGSHU_BROWSER_PROBE_OBSERVATION_KIND
            if contract.platform == "xiaohongshu"
            else (
                DOUYIN_DEVICE_PROBE_OBSERVATION_KIND
                if contract.platform == "douyin"
                else API_PROBE_KIND
            )
        )
        expected_shadow_kind = (
            XIAOHONGSHU_BROWSER_SHADOW_OBSERVATION_KIND
            if contract.platform == "xiaohongshu"
            else (
                DOUYIN_DEVICE_SHADOW_OBSERVATION_KIND
                if contract.platform == "douyin"
                else API_PROBE_KIND
            )
        )
        if (
            probe_observation_kind != expected_probe_kind
            or shadow_observation_kind != expected_shadow_kind
            or not probe_observation_hash
            or not shadow_observation_hash
        ):
            return None
        evidence_id = str(uuid.uuid4())
        await db.execute(
            """INSERT INTO execution_evidence_bindings
                 (id, lottery_id, account_id, platform, rule_snapshot_id,
                  execution_path_id, target_hash, rule_hash, action_plan_hash,
                   config_hash, probe_id, shadow_task_id, probe_observation_kind,
                   probe_observation_hash, shadow_observation_kind,
                   shadow_observation_hash, status, verified_at, expires_at)
               VALUES
                 (:id, :lottery_id, :account_id, :platform, :rule_snapshot_id,
                  :execution_path_id, :target_hash, :rule_hash, :action_plan_hash,
                   :config_hash, :probe_id, :shadow_task_id, :probe_observation_kind,
                   :probe_observation_hash, :shadow_observation_kind,
                   :shadow_observation_hash, 'verified', NOW(),
                  DATE_ADD(LEAST(:probe_finished_at, :shadow_finished_at), INTERVAL 24 HOUR))
               ON DUPLICATE KEY UPDATE id = id""",
            {
                **contract.__dict__,
                "id": evidence_id,
                "probe_id": probe_id,
                "shadow_task_id": shadow_task_id,
                "probe_observation_kind": probe_observation_kind,
                "probe_observation_hash": probe_observation_hash,
                "shadow_observation_kind": shadow_observation_kind,
                "shadow_observation_hash": shadow_observation_hash,
                "probe_finished_at": _row_get(probe, "finished_at"),
                "shadow_finished_at": _row_get(shadow, "finished_at"),
            },
        )
        persisted = await db.fetch_one(
            """SELECT e.id, e.lottery_id, e.account_id, e.platform,
                      e.rule_snapshot_id, e.execution_path_id, e.target_hash,
                      e.rule_hash, e.action_plan_hash, e.config_hash,
                       e.probe_observation_kind,
                       e.probe_observation_hash, e.shadow_observation_kind,
                       e.shadow_observation_hash, e.status, e.verified_at,
                       e.expires_at,
                       CASE WHEN e.verified_at >= GREATEST(
                              probe.finished_at, shadow.finished_at)
                            AND e.verified_at <= NOW()
                            AND e.expires_at = DATE_ADD(
                              LEAST(probe.finished_at, shadow.finished_at),
                              INTERVAL 24 HOUR)
                            AND e.expires_at > NOW()
                       THEN 1 ELSE 0 END AS expiry_bounded
               FROM execution_evidence_bindings e
               JOIN adapter_calibrations probe ON probe.probe_id = e.probe_id
               JOIN task_runs shadow ON shadow.task_id = e.shadow_task_id
               WHERE e.probe_id = :probe_id AND e.shadow_task_id = :shadow_task_id""",
            {"probe_id": probe_id, "shadow_task_id": shadow_task_id},
        )
        if (
            persisted is None
            or not _same_contract(contract, _contract_from_row(persisted))
            or str(_row_get(persisted, "status") or "").strip().lower() != "verified"
            or _row_get(persisted, "verified_at") is None
            or _row_get(persisted, "expires_at") is None
            or str(_row_get(persisted, "probe_observation_kind") or "").strip()
            != probe_observation_kind
            or str(_row_get(persisted, "probe_observation_hash") or "").strip()
            != probe_observation_hash
            or str(_row_get(persisted, "shadow_observation_kind") or "").strip()
            != shadow_observation_kind
            or str(_row_get(persisted, "shadow_observation_hash") or "").strip()
            != shadow_observation_hash
            or int(_row_get(persisted, "expiry_bounded", 0) or 0) != 1
        ):
            raise RuntimeError("execution_evidence_materialization_not_persisted")
        return str(_row_get(persisted, "id") or "").strip() or None
