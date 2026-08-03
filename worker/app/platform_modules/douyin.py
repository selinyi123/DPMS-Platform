"""Douyin Android device-agent execution module."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Mapping

from app.action_plan import (
    ActionPlanV2Error,
    canonical_json_bytes,
    compute_rule_hash,
    compute_target_hash,
    validate_action_plan_v2,
)
from app.adapters.douyin import DouyinAdapter
from app.douyin_device_client import (
    DouyinDeviceActionOutcomeUnknown,
    DouyinDeviceActionRejected,
    DouyinDeviceClient,
    DouyinDevicePreflightBlocked,
)
from app.platform_modules.base import (
    ExecutionPath,
    PlatformModule,
    RealRunEvidenceBinding,
)
from app.platform_modules.contracts.douyin import (
    DOUYIN_ACTION_PLAN_CONTRACT,
    DOUYIN_ACTION_ORDER,
    DOUYIN_DEVICE_EXECUTION_PATH,
    DOUYIN_MANUAL_EXECUTION_PATH,
)
from app.platform_modules.evidence import RealRunGateBlocked, json_object, row_value
from app.platform_modules.errors import ExternalActionOutcomeUnknown
from app.platform_modules.shared_execution import execute_browser_observation_shadow
from app.services.execution_evidence import materialize_for_probe
from shared.douyin_device_contract import (
    DOUYIN_DEVICE_CONTRACT_VERSION,
    DOUYIN_DEVICE_PROBE_OBSERVATION_KIND,
    DOUYIN_DEVICE_SHADOW_OBSERVATION_KIND,
    DouyinDeviceContractError,
    compute_douyin_device_config_hash,
    compute_douyin_exact_text_hash,
    format_douyin_device_observed_at,
    hash_douyin_device_observation,
    normalize_douyin_device_public_config,
    validate_douyin_device_observation_binding,
)


_DOUYIN_CANONICAL_URI = re.compile(
    r"canonical://douyin/(?:video|note)/[0-9]{8,32}\Z", re.ASCII
)


class DouyinDeviceExecutionBlocked(RuntimeError):
    quarantine_account = True
    account_status = "cooling"


def _plan_text_hashes(plan) -> tuple[str, str]:
    follow = plan.follow_target_handle if "followed" in plan.required_actions else ""
    comment = (
        plan.payload_for("commented").get("text", "")
        if "commented" in plan.required_actions
        else ""
    )
    return (
        compute_douyin_exact_text_hash(follow),
        compute_douyin_exact_text_hash(comment),
    )


def _device_observation(
    *,
    evidence_id: str,
    observation_kind: str,
    lottery_id: int,
    account_id: int,
    execution_revision: int,
    target_hash: str,
    plan,
    config_hash: str,
    public_config: Mapping[str, str],
    snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    actions = list(plan.required_actions)
    follow_hash, comment_hash = _plan_text_hashes(plan)
    action_states = snapshot.get("action_states")
    device_names = {
        "followed": "follow",
        "liked": "like",
        "commented": "comment",
        "favorited": "favorite",
    }
    checks = {
        action: bool(
            isinstance(action_states, Mapping)
            and isinstance(action_states.get(device_names[action]), Mapping)
            and action_states[device_names[action]].get("calibrated") is True
        )
        for action in actions
    }
    return {
        "contract_version": DOUYIN_DEVICE_CONTRACT_VERSION,
        "platform": "douyin",
        "execution_path_id": DOUYIN_DEVICE_EXECUTION_PATH,
        "lottery_id": lottery_id,
        "account_id": account_id,
        "execution_revision": execution_revision,
        "target_hash": target_hash,
        "observed_target_hash": (
            target_hash
            if snapshot.get("target_identity_verified") is True
            else ""
        ),
        "rule_snapshot_id": plan.rule_snapshot_id,
        "rule_hash": plan.rule_hash,
        "action_plan_hash": plan.plan_hash,
        "config_hash": config_hash,
        "required_actions": actions,
        "follow_target_handle_hash": follow_hash,
        "comment_text_hash": comment_hash,
        "observation_kind": observation_kind,
        "observed_at": format_douyin_device_observed_at(
            datetime.now(timezone.utc)
        ),
        "evidence_id": evidence_id,
        "side_effects": False,
        "agent_id": public_config["agent_id"],
        "manifest_sha256": public_config["manifest_sha256"],
        "device_serial_sha256": public_config["device_serial_sha256"],
        "account_id_sha256": public_config["account_id_sha256"],
        "package": str(snapshot.get("package") or ""),
        "package_ok": snapshot.get("package_ok") is True,
        "risk_blocked": snapshot.get("blocked") is True,
        "target_identity_verified": (
            snapshot.get("target_identity_verified") is True
        ),
        "follow_target_verified": bool(
            "followed" in actions
            and snapshot.get("follow_target_verified") is True
        ),
        "capability_checks": checks,
    }


def validate_douyin_probe_authority(message: Mapping[str, Any]) -> bool:
    try:
        revision = int(message.get("execution_revision"))
    except (TypeError, ValueError):
        return False
    return bool(
        revision > 0
        and message.get("execution_path_id") == DOUYIN_DEVICE_EXECUTION_PATH
        and all(
            isinstance(message.get(key), str)
            and re.fullmatch(r"[0-9a-f]{64}", message[key], re.ASCII)
            for key in ("target_hash", "rule_hash", "action_plan_hash", "config_hash")
        )
    )


async def validate_douyin_probe_claim(*, runtime, probe, row, authoritative) -> None:
    try:
        plan = validate_action_plan_v2(
            row["lottery_action_plan"], require_executable=True, reject_media=True
        )
        revision = int(row["execution_revision"] or 0)
        config_row = await runtime.database.fetch_one(
            """SELECT config_json FROM adapter_selector_configs
               WHERE platform = 'douyin' FOR UPDATE"""
        )
        selector_config = json_object(
            row_value(config_row, "config_json"),
            code="douyin_device_config_invalid",
        )
        config_hash = compute_douyin_device_config_hash(revision, selector_config)
        target_hash = compute_target_hash(authoritative["canonical_url"])
        message_revision = int(probe.get("execution_revision"))
        message_snapshot_id = int(probe.get("rule_snapshot_id"))
    except (ActionPlanV2Error, DouyinDeviceContractError) as exc:
        raise ValueError(f"adapter_probe_{exc.code}") from exc
    except (TypeError, ValueError, KeyError, RealRunGateBlocked) as exc:
        raise ValueError("adapter_probe_douyin_binding_invalid") from exc
    if (
        plan.execution_path_id != DOUYIN_DEVICE_EXECUTION_PATH
        or authoritative["execution_path_id"] != DOUYIN_DEVICE_EXECUTION_PATH
        or str(probe.get("execution_path_id") or "") != DOUYIN_DEVICE_EXECUTION_PATH
        or authoritative["target_hash"] != target_hash
        or str(probe.get("target_hash") or "") != target_hash
        or authoritative["rule_snapshot_id"] != plan.rule_snapshot_id
        or message_snapshot_id != plan.rule_snapshot_id
        or int(row["authoritative_rule_snapshot_id"] or 0) != plan.rule_snapshot_id
        or authoritative["rule_hash"] != plan.rule_hash
        or str(probe.get("rule_hash") or "") != plan.rule_hash
        or str(row["lottery_rule_hash"] or "") != plan.rule_hash
        or str(row["snapshot_rule_hash"] or "") != plan.rule_hash
        or authoritative["action_plan_hash"] != plan.plan_hash
        or str(probe.get("action_plan_hash") or "") != plan.plan_hash
        or str(row["lottery_action_plan_hash"] or "") != plan.plan_hash
        or authoritative["config_hash"] != config_hash
        or str(probe.get("config_hash") or "") != config_hash
        or message_revision != revision
        or revision <= 0
        or int(row["snapshot_complete"] or 0) != 1
        or not str(row["snapshot_attested_by"] or "").strip()
        or row["snapshot_attested_at"] is None
    ):
        raise ValueError("adapter_probe_douyin_binding_mismatch")
    lottery_rule = row["lottery_rule_text"]
    snapshot_rule = row["snapshot_rule_text"]
    if isinstance(lottery_rule, bytes):
        lottery_rule = lottery_rule.decode("utf-8", errors="strict")
    if isinstance(snapshot_rule, bytes):
        snapshot_rule = snapshot_rule.decode("utf-8", errors="strict")
    if (
        not isinstance(lottery_rule, str)
        or lottery_rule != snapshot_rule
        or compute_rule_hash(lottery_rule) != plan.rule_hash
    ):
        raise ValueError("adapter_probe_rule_snapshot_mismatch")
    if probe.get("action_plan") is not None:
        message_plan = validate_action_plan_v2(
            probe["action_plan"], require_executable=True, reject_media=True
        )
        if canonical_json_bytes(message_plan.plan) != canonical_json_bytes(plan.plan):
            raise ValueError("adapter_probe_action_plan_mismatch")
    authoritative.update(
        action_plan=plan.plan,
        execution_revision=revision,
        selector_config=selector_config,
    )


async def _read_device_snapshot(binding: Mapping[str, Any], plan):
    public_config = normalize_douyin_device_public_config(
        binding.get("selector_config") or {}
    )
    client = DouyinDeviceClient.from_environment()
    try:
        await client.health(
            expected_identity=public_config,
            required_actions=plan.required_actions,
        )
        snapshot = await client.snapshot(
            operation_key=str(
                binding.get("probe_id") or binding.get("task_id") or ""
            ),
            target_hash=str(binding.get("target_hash") or ""),
            required_actions=plan.required_actions,
            comment=(
                plan.payload_for("commented").get("text")
                if "commented" in plan.required_actions
                else None
            ),
            follow_target_handle=(
                plan.follow_target_handle
                if "followed" in plan.required_actions
                else ""
            ),
            expected_identity=public_config,
        )
        return public_config, snapshot
    finally:
        await client.aclose()


async def execute_douyin_device_probe(binding: dict, pool, *, runtime):
    del pool
    if (
        binding.get("platform") != "douyin"
        or binding.get("execution_path_id") != DOUYIN_DEVICE_EXECUTION_PATH
    ):
        raise RuntimeError("douyin_device_probe_binding_invalid")
    plan = validate_action_plan_v2(
        binding.get("action_plan"), require_executable=True, reject_media=True
    )
    public_config, snapshot = await _read_device_snapshot(binding, plan)
    observation = _device_observation(
        evidence_id=str(binding.get("probe_id") or ""),
        observation_kind=DOUYIN_DEVICE_PROBE_OBSERVATION_KIND,
        lottery_id=int(binding["lottery_id"]),
        account_id=int(binding["account_id"]),
        execution_revision=int(binding["execution_revision"]),
        target_hash=str(binding["target_hash"]),
        plan=plan,
        config_hash=str(binding["config_hash"]),
        public_config=public_config,
        snapshot=snapshot,
    )
    observation_hash = hash_douyin_device_observation(observation)
    return runtime.ProbeObservation(
        result=observation,
        observation_kind=DOUYIN_DEVICE_PROBE_OBSERVATION_KIND,
        observation_hash=observation_hash,
        success_event_type="AdapterDouyinDeviceProbeSucceeded",
        success_event_payload={
            "probe_kind": DOUYIN_DEVICE_PROBE_OBSERVATION_KIND,
            "observation_hash": observation_hash,
            "required_actions": list(plan.required_actions),
            "side_effects": False,
        },
        materialize_execution_evidence=True,
    )


async def materialize_douyin_terminal_probe(*, probe_id: str, runtime) -> None:
    await materialize_for_probe(db=runtime.database, probe_id=probe_id)


DOUYIN_REAL_RUN_EVIDENCE_QUERY = """
SELECT
  e.id AS evidence_id, e.lottery_id AS evidence_lottery_id,
  e.account_id AS evidence_account_id, e.platform AS evidence_platform,
  e.rule_snapshot_id AS evidence_rule_snapshot_id,
  e.execution_path_id AS evidence_execution_path_id,
  e.target_hash AS evidence_target_hash, e.rule_hash AS evidence_rule_hash,
  e.action_plan_hash AS evidence_action_plan_hash,
  e.config_hash AS evidence_config_hash, e.probe_id AS evidence_probe_id,
  e.shadow_task_id AS evidence_shadow_task_id,
  e.probe_observation_kind AS evidence_probe_observation_kind,
  e.probe_observation_hash AS evidence_probe_observation_hash,
  e.shadow_observation_kind AS evidence_shadow_observation_kind,
  e.shadow_observation_hash AS evidence_shadow_observation_hash,
  e.status AS evidence_status, e.verified_at AS evidence_verified_at,
  e.expires_at AS evidence_expires_at,
  CASE WHEN e.expires_at > NOW() THEN 1 ELSE 0 END AS evidence_active,
  CASE WHEN e.verified_at >= GREATEST(ac.finished_at, shadow.finished_at)
         AND e.verified_at <= NOW()
         AND e.expires_at <= LEAST(DATE_ADD(ac.finished_at, INTERVAL 24 HOUR),
                                    DATE_ADD(shadow.finished_at, INTERVAL 24 HOUR))
         AND e.expires_at > NOW() THEN 1 ELSE 0 END AS evidence_time_bounded,
  ac.status AS evidence_probe_status, ac.result AS evidence_probe_observation,
  ac.observation_kind AS source_probe_observation_kind,
  ac.observation_hash AS source_probe_observation_hash,
  ac.finished_at AS evidence_probe_finished_at,
  CASE WHEN ac.finished_at >= DATE_SUB(NOW(), INTERVAL 24 HOUR)
         AND ac.finished_at <= NOW() THEN 1 ELSE 0 END AS evidence_probe_fresh,
  CASE WHEN probe_lease.released_at >= ac.finished_at
         AND probe_lease.released_at <= NOW()
         AND probe_lease.operation_kind = 'adapter_probe'
         AND probe_lease.owner_id = ac.probe_id AND probe_lease.task_id IS NULL
       THEN 1 ELSE 0 END AS evidence_probe_lease_released,
  CASE WHEN ac.started_at IS NOT NULL AND probe_lease.acquired_at <= ac.started_at
         AND ac.started_at <= ac.finished_at AND probe_lease.expires_at >= ac.finished_at
         AND probe_lease.released_at >= ac.finished_at
       THEN 1 ELSE 0 END AS evidence_probe_lease_covers_observation,
  shadow.status AS evidence_shadow_status,
  shadow.task_mode AS evidence_shadow_task_mode,
  shadow.preflight_observation AS evidence_shadow_observation,
  shadow.preflight_observation_kind AS source_shadow_observation_kind,
  shadow.preflight_observation_hash AS source_shadow_observation_hash,
  shadow.finished_at AS evidence_shadow_finished_at,
  CASE WHEN shadow.finished_at >= DATE_SUB(NOW(), INTERVAL 24 HOUR)
         AND shadow.finished_at <= NOW() THEN 1 ELSE 0 END AS evidence_shadow_fresh,
  CASE WHEN shadow_lease.released_at >= shadow.finished_at
         AND shadow_lease.released_at <= NOW()
         AND shadow_lease.operation_kind = 'shadow_run'
         AND shadow_lease.owner_id = shadow.task_id
         AND shadow_lease.task_id = shadow.task_id
       THEN 1 ELSE 0 END AS evidence_shadow_lease_released,
  CASE WHEN shadow.started_at IS NOT NULL
         AND shadow_lease.acquired_at <= shadow.started_at
         AND shadow.started_at <= shadow.finished_at
         AND shadow_lease.expires_at >= shadow.finished_at
         AND shadow_lease.released_at >= shadow.finished_at
       THEN 1 ELSE 0 END AS evidence_shadow_lease_covers_observation,
  shadow.target_hash AS evidence_shadow_target_hash,
  shadow.config_hash AS evidence_shadow_config_hash
FROM task_runs tr
LEFT JOIN execution_evidence_bindings e
  ON e.id = tr.execution_evidence_id AND e.lottery_id = tr.lottery_id
 AND e.account_id = tr.account_id AND e.rule_snapshot_id = tr.rule_snapshot_id
 AND e.execution_path_id = tr.execution_path_id AND e.target_hash = tr.target_hash
 AND e.rule_hash = tr.rule_hash AND e.action_plan_hash = tr.action_plan_hash
 AND e.config_hash = tr.config_hash
LEFT JOIN adapter_calibrations ac
  ON ac.probe_id = e.probe_id AND ac.lottery_id = e.lottery_id
 AND ac.account_id = e.account_id AND ac.platform = e.platform
 AND ac.rule_snapshot_id = e.rule_snapshot_id
 AND ac.execution_path_id = e.execution_path_id AND ac.target_hash = e.target_hash
 AND ac.rule_hash = e.rule_hash AND ac.action_plan_hash = e.action_plan_hash
 AND ac.config_hash = e.config_hash
 AND ac.observation_kind = e.probe_observation_kind
 AND ac.observation_hash = e.probe_observation_hash
LEFT JOIN task_runs shadow
  ON shadow.task_id = e.shadow_task_id AND shadow.lottery_id = e.lottery_id
 AND shadow.account_id = e.account_id AND shadow.rule_snapshot_id = e.rule_snapshot_id
 AND shadow.execution_path_id = e.execution_path_id
 AND shadow.target_hash = e.target_hash AND shadow.rule_hash = e.rule_hash
 AND shadow.action_plan_hash = e.action_plan_hash AND shadow.config_hash = e.config_hash
 AND shadow.preflight_observation_kind = e.shadow_observation_kind
 AND shadow.preflight_observation_hash = e.shadow_observation_hash
LEFT JOIN account_operation_leases probe_lease
  ON probe_lease.lease_id = ac.account_lease_id
 AND probe_lease.account_id = ac.account_id
 AND probe_lease.generation = ac.account_lease_generation
LEFT JOIN account_operation_leases shadow_lease
  ON shadow_lease.lease_id = shadow.account_lease_id
 AND shadow_lease.account_id = shadow.account_id
 AND shadow_lease.generation = shadow.account_lease_generation
WHERE tr.task_id = :task_id
"""


_EVIDENCE_FIELDS = tuple(
    match.group(1)
    for match in re.finditer(r"\bAS\s+([a-z_]+)", DOUYIN_REAL_RUN_EVIDENCE_QUERY, re.I)
)


async def load_douyin_real_run_evidence_context(*, db, task_id: str) -> dict[str, Any]:
    row = await db.fetch_one(DOUYIN_REAL_RUN_EVIDENCE_QUERY, {"task_id": task_id})
    return {field: row_value(row, field) for field in _EVIDENCE_FIELDS}


def _expected_observation(plan, account_id, lottery_id, revision, target_hash, config_hash, public_config):
    follow_hash, comment_hash = _plan_text_hashes(plan)
    return {
        "expected_lottery_id": lottery_id,
        "expected_account_id": account_id,
        "expected_execution_revision": revision,
        "expected_target_hash": target_hash,
        "expected_rule_snapshot_id": plan.rule_snapshot_id,
        "expected_rule_hash": plan.rule_hash,
        "expected_action_plan_hash": plan.plan_hash,
        "expected_config_hash": config_hash,
        "expected_actions": plan.required_actions,
        "expected_follow_target_handle_hash": follow_hash,
        "expected_comment_text_hash": comment_hash,
        "expected_public_config": public_config,
    }


def douyin_real_run_precondition(task: Mapping[str, Any]) -> str | None:
    if str(task.get("platform") or "").strip().lower() != "douyin":
        return "douyin_task_platform_mismatch"
    if not _DOUYIN_CANONICAL_URI.fullmatch(str(task.get("canonical_url") or "")):
        return "douyin_video_or_note_target_required"
    return None


def validate_douyin_real_run_evidence(
    *, task, row, account_id, lottery_id, platform, plan, execution_plan=None
) -> RealRunEvidenceBinding:
    runtime_plan = execution_plan or plan
    if (
        platform != "douyin"
        or plan.execution_path_id != DOUYIN_DEVICE_EXECUTION_PATH
        or runtime_plan.execution_path_id != DOUYIN_DEVICE_EXECUTION_PATH
    ):
        raise RealRunGateBlocked("douyin_execution_path_not_supported")
    evidence_id = str(task.get("execution_evidence_id") or "")
    try:
        revision = int(row_value(row, "account_execution_revision"))
        public_config = normalize_douyin_device_public_config(
            task.get("selector_config") or {}
        )
        config_hash = compute_douyin_device_config_hash(
            revision, task.get("selector_config") or {}
        )
        target_hash = compute_target_hash(str(task.get("canonical_url") or ""))
    except (TypeError, ValueError, DouyinDeviceContractError) as exc:
        raise RealRunGateBlocked("douyin_device_task_binding_invalid") from exc
    scalar_checks = (
        evidence_id
        and evidence_id == str(row_value(row, "task_execution_evidence_id") or "")
        and evidence_id == str(row_value(row, "evidence_id") or "")
        and int(row_value(row, "evidence_lottery_id") or 0) == lottery_id
        and int(row_value(row, "evidence_account_id") or 0) == account_id
        and str(row_value(row, "evidence_platform") or "") == "douyin"
        and int(row_value(row, "evidence_rule_snapshot_id") or 0) == plan.rule_snapshot_id
        and str(row_value(row, "evidence_execution_path_id") or "") == DOUYIN_DEVICE_EXECUTION_PATH
        and str(row_value(row, "evidence_target_hash") or "") == target_hash
        and str(row_value(row, "evidence_rule_hash") or "") == plan.rule_hash
        and str(row_value(row, "evidence_action_plan_hash") or "") == plan.plan_hash
        and str(row_value(row, "evidence_config_hash") or "") == config_hash
        and str(task.get("target_hash") or "") == target_hash
        and str(task.get("config_hash") or "") == config_hash
        and int(task.get("execution_revision") or 0) == revision
        and int(row_value(row, "account_credential_present") or 0) == 1
        and str(row_value(row, "evidence_status") or "") == "verified"
        and int(row_value(row, "evidence_active") or 0) == 1
        and int(row_value(row, "evidence_time_bounded") or 0) == 1
        and str(row_value(row, "evidence_probe_status") or "") == "succeeded"
        and int(row_value(row, "evidence_probe_fresh") or 0) == 1
        and int(row_value(row, "evidence_probe_lease_released") or 0) == 1
        and int(row_value(row, "evidence_probe_lease_covers_observation") or 0) == 1
        and str(row_value(row, "evidence_shadow_status") or "") == "succeeded"
        and str(row_value(row, "evidence_shadow_task_mode") or "") == "shadow_run"
        and int(row_value(row, "evidence_shadow_fresh") or 0) == 1
        and int(row_value(row, "evidence_shadow_lease_released") or 0) == 1
        and int(row_value(row, "evidence_shadow_lease_covers_observation") or 0) == 1
    )
    if not scalar_checks:
        raise RealRunGateBlocked("douyin_exact_device_evidence_required")
    expected = _expected_observation(
        runtime_plan, account_id, lottery_id, revision, target_hash, config_hash, public_config
    )
    try:
        validate_douyin_device_observation_binding(
            row_value(row, "evidence_probe_observation"),
            expected_observation_kind=DOUYIN_DEVICE_PROBE_OBSERVATION_KIND,
            expected_evidence_id=str(row_value(row, "evidence_probe_id") or ""),
            source_observation_kind=str(row_value(row, "source_probe_observation_kind") or ""),
            source_observation_hash=str(row_value(row, "source_probe_observation_hash") or ""),
            evidence_observation_kind=str(row_value(row, "evidence_probe_observation_kind") or ""),
            evidence_observation_hash=str(row_value(row, "evidence_probe_observation_hash") or ""),
            **expected,
        )
        validate_douyin_device_observation_binding(
            row_value(row, "evidence_shadow_observation"),
            expected_observation_kind=DOUYIN_DEVICE_SHADOW_OBSERVATION_KIND,
            expected_evidence_id=str(row_value(row, "evidence_shadow_task_id") or ""),
            source_observation_kind=str(row_value(row, "source_shadow_observation_kind") or ""),
            source_observation_hash=str(row_value(row, "source_shadow_observation_hash") or ""),
            evidence_observation_kind=str(row_value(row, "evidence_shadow_observation_kind") or ""),
            evidence_observation_hash=str(row_value(row, "evidence_shadow_observation_hash") or ""),
            **expected,
        )
    except DouyinDeviceContractError as exc:
        raise RealRunGateBlocked(exc.code) from exc
    return RealRunEvidenceBinding(evidence_id=evidence_id, execution_revision=revision)


async def _persist_shadow_observation(task, plan, public_config, snapshot, *, runtime):
    task_id = str(task.get("task_id") or "")
    account_id = int(task.get("account_id"))
    lottery_id = int(task.get("lottery_id"))
    async with runtime.database.transaction():
        current = await runtime.database.fetch_one(
            """SELECT tr.task_id, tr.status, tr.worker_id, tr.account_id, tr.lottery_id,
                      tr.execution_path_id, tr.rule_snapshot_id, tr.target_hash,
                      tr.rule_hash, tr.action_plan_hash, tr.config_hash,
                      tr.reconciliation_required, a.platform AS account_platform,
                      a.execution_revision, lease.operation_kind, lease.owner_id,
                      lease.task_id AS lease_task_id,
                      CASE WHEN lease.expires_at > NOW() THEN 1 ELSE 0 END AS lease_active,
                      CASE WHEN lease.released_at IS NULL THEN 1 ELSE 0 END AS lease_unreleased
               FROM task_runs tr JOIN accounts a ON a.id = tr.account_id
               LEFT JOIN account_operation_leases lease
                 ON lease.lease_id = tr.account_lease_id
                AND lease.account_id = tr.account_id
                AND lease.generation = tr.account_lease_generation
               WHERE tr.task_id = :task_id FOR UPDATE""",
            {"task_id": task_id},
        )
        revision = int(runtime.row_get(current, "execution_revision") or 0)
        target_hash = compute_target_hash(str(task.get("canonical_url") or ""))
        config_hash = compute_douyin_device_config_hash(
            revision, task.get("selector_config") or {}
        )
        if (
            not current
            or str(runtime.row_get(current, "status") or "") != "running"
            or str(runtime.row_get(current, "worker_id") or "") != runtime.WORKER_ID
            or int(runtime.row_get(current, "account_id") or 0) != account_id
            or int(runtime.row_get(current, "lottery_id") or 0) != lottery_id
            or str(runtime.row_get(current, "account_platform") or "") != "douyin"
            or str(runtime.row_get(current, "execution_path_id") or "") != DOUYIN_DEVICE_EXECUTION_PATH
            or int(runtime.row_get(current, "rule_snapshot_id") or 0) != plan.rule_snapshot_id
            or str(runtime.row_get(current, "target_hash") or "") != target_hash
            or str(runtime.row_get(current, "rule_hash") or "") != plan.rule_hash
            or str(runtime.row_get(current, "action_plan_hash") or "") != plan.plan_hash
            or str(runtime.row_get(current, "config_hash") or "") != config_hash
            or str(task.get("target_hash") or "") != target_hash
            or str(task.get("config_hash") or "") != config_hash
            or str(runtime.row_get(current, "operation_kind") or "") != "shadow_run"
            or str(runtime.row_get(current, "owner_id") or "") != task_id
            or str(runtime.row_get(current, "lease_task_id") or "") != task_id
            or int(runtime.row_get(current, "lease_active") or 0) != 1
            or int(runtime.row_get(current, "lease_unreleased") or 0) != 1
            or int(runtime.row_get(current, "reconciliation_required") or 0) != 0
        ):
            raise runtime.TaskOwnershipLost("douyin_device_shadow_binding_changed")
        observation = _device_observation(
            evidence_id=task_id,
            observation_kind=DOUYIN_DEVICE_SHADOW_OBSERVATION_KIND,
            lottery_id=lottery_id,
            account_id=account_id,
            execution_revision=revision,
            target_hash=target_hash,
            plan=plan,
            config_hash=config_hash,
            public_config=public_config,
            snapshot=snapshot,
        )
        observation_hash = hash_douyin_device_observation(observation)
        await runtime.database.execute(
            """UPDATE task_runs SET preflight_observation = :observation,
                      preflight_observation_kind = :kind,
                      preflight_observation_hash = :observation_hash
                 WHERE task_id = :task_id AND status = 'running'
                   AND worker_id = :worker_id""",
            {
                "task_id": task_id,
                "worker_id": runtime.WORKER_ID,
                "observation": runtime.canonical_json_bytes(observation).decode("utf-8"),
                "kind": DOUYIN_DEVICE_SHADOW_OBSERVATION_KIND,
                "observation_hash": observation_hash,
            },
        )
    event_id = await runtime.record_event(
        aggregate="task",
        aggregate_id=task_id,
        event_type="TaskDouyinDeviceShadowObserved",
        payload={
            "account_id": account_id,
            "lottery_id": lottery_id,
            "observation_kind": DOUYIN_DEVICE_SHADOW_OBSERVATION_KIND,
            "observation_hash": observation_hash,
            "required_actions": list(plan.required_actions),
            "side_effects": False,
        },
        correlation_id=task_id,
    )
    if not event_id:
        raise RuntimeError("douyin_device_shadow_event_persistence_failed")


async def execute_douyin_device_shadow(task, adapter, pool, *, runtime):
    del adapter, pool
    plan = runtime.validate_action_plan_v2(
        task.get("action_plan"), require_executable=True, reject_media=True
    )
    if plan.execution_path_id != DOUYIN_DEVICE_EXECUTION_PATH:
        raise RuntimeError("douyin_device_shadow_binding_invalid")
    public_config, snapshot = await _read_device_snapshot(task, plan)
    await _persist_shadow_observation(task, plan, public_config, snapshot, runtime=runtime)
    return None


def _same_gate(left, right, runtime) -> bool:
    return bool(
        left.action_plan.plan_hash == right.action_plan.plan_hash
        and runtime.gate_execution_action_plan(left).plan_hash
        == runtime.gate_execution_action_plan(right).plan_hash
        and runtime.gate_requested_actions(left) == runtime.gate_requested_actions(right)
        and left.execution_evidence_id == right.execution_evidence_id
        and left.execution_revision == right.execution_revision
        and left.execution_intent_kind == right.execution_intent_kind
    )


async def execute_douyin_device_real(task, adapter, pool, *, runtime):
    del adapter, pool
    task_id = str(task.get("task_id") or "")
    account_id = int(task.get("account_id"))
    lottery_id = int(task.get("lottery_id"))
    queued_plan = runtime.validate_action_plan_v2(
        task.get("action_plan"), require_executable=True, reject_media=True
    )
    if queued_plan.execution_path_id != DOUYIN_DEVICE_EXECUTION_PATH:
        raise RuntimeError("douyin_execution_path_not_supported")
    if (await runtime.get_latest_phase(task_id) or "init") == "completed":
        return
    gate = await runtime.enforce_task_real_run_gate(task, require_running=True)
    plan = runtime.gate_execution_action_plan(gate)
    if (
        gate.platform != "douyin"
        or plan.plan_hash != queued_plan.plan_hash
        or runtime.gate_requested_actions(gate) != plan.required_actions
    ):
        raise runtime.RealRunGateBlocked("douyin_device_execution_binding_invalid")
    public_config = normalize_douyin_device_public_config(
        task.get("selector_config") or {}
    )
    target_hash = compute_target_hash(str(task.get("canonical_url") or ""))
    follow_handle = plan.follow_target_handle if "followed" in plan.required_actions else ""
    client = DouyinDeviceClient.from_environment()
    active_intent = None

    async def quarantine_unknown(action: str, cause: BaseException) -> None:
        nonlocal active_intent
        if active_intent is not None:
            try:
                await runtime.await_safety_settlement(
                    runtime.mark_action_intent_unknown(
                        db=runtime.database,
                        intent=active_intent,
                        reason=f"douyin_device_{action}_outcome_unknown",
                    )
                )
            except BaseException as exc:
                runtime.structured_log(
                    "error", "douyin_device_intent_unknown_write_failed",
                    task_id=task_id, action=action, exception=exc,
                )
        await runtime.await_safety_settlement(
            runtime.quarantine_external_action_outcome(
                task_id=task_id,
                account_id=account_id,
                platform="douyin",
                action=action,
                cause=cause,
            )
        )

    try:
        await client.health(
            expected_identity=public_config,
            required_actions=plan.required_actions,
        )
        await client.snapshot(
            operation_key=f"{task_id}:preflight",
            target_hash=target_hash,
            required_actions=plan.required_actions,
            comment=(
                plan.payload_for("commented").get("text")
                if "commented" in plan.required_actions
                else None
            ),
            follow_target_handle=follow_handle,
            expected_identity=public_config,
        )
        for action in plan.required_actions:
            current_gate = await runtime.enforce_task_real_run_gate(
                task, require_running=True
            )
            if not _same_gate(gate, current_gate, runtime):
                raise runtime.RealRunGateBlocked(
                    "douyin_device_binding_changed_during_execution"
                )
            await runtime.refresh_task_lease(task_id)
            await runtime.renew_account_operation_lease(
                db=runtime.database,
                task_id=task_id,
                account_id=account_id,
                lottery_id=lottery_id,
                worker_id=runtime.WORKER_ID,
                execution_intent_kind=current_gate.execution_intent_kind,
            )
            renewed_gate = await runtime.enforce_task_real_run_gate(
                task, require_running=True
            )
            if not _same_gate(gate, renewed_gate, runtime):
                raise runtime.RealRunGateBlocked(
                    "douyin_device_binding_changed_during_execution"
                )
            action_payload = plan.payload_for(action)
            active_intent = await runtime.prepare_and_start_action_intent(
                db=runtime.database,
                task_id=task_id,
                account_id=account_id,
                lottery_id=lottery_id,
                worker_id=runtime.WORKER_ID,
                execution_intent_kind=renewed_gate.execution_intent_kind,
                action=action,
                payload={
                    "platform": "douyin",
                    "execution_path_id": DOUYIN_DEVICE_EXECUTION_PATH,
                    "execution_evidence_id": gate.execution_evidence_id,
                    "execution_revision": gate.execution_revision,
                    "target_hash": target_hash,
                    "device_agent": public_config,
                    "action_payload": action_payload,
                },
            )
            operation_key = f"{active_intent.intent_id}:{active_intent.attempt_no}"
            try:
                receipt = await client.execute(
                    operation_key=operation_key,
                    target_hash=target_hash,
                    action=action,
                    comment=(action_payload.get("text") if action == "commented" else None),
                    follow_target_handle=follow_handle,
                    required_actions=plan.required_actions,
                    expected_identity=public_config,
                )
            except DouyinDeviceActionRejected as exc:
                await runtime.await_safety_settlement(
                    runtime.settle_action_intent(
                        db=runtime.database,
                        intent=active_intent,
                        succeeded=False,
                        outcome="risk" if exc.risk else "rejected",
                        error_message=exc.reason,
                    )
                )
                active_intent = None
                raise DouyinDeviceExecutionBlocked(str(exc)) from exc
            except (DouyinDeviceActionOutcomeUnknown, BaseException) as exc:
                await quarantine_unknown(action, exc)
                raise ExternalActionOutcomeUnknown("douyin", action, exc) from exc
            try:
                await runtime.settle_action_intent(
                    db=runtime.database,
                    intent=active_intent,
                    succeeded=True,
                    outcome="ok",
                    remote_ref=receipt.operation_key,
                )
            except BaseException as exc:
                await quarantine_unknown(action, exc)
                raise ExternalActionOutcomeUnknown("douyin", action, exc) from exc
            active_intent = None
        event_id = await runtime.record_event(
            aggregate="task",
            aggregate_id=task_id,
            event_type="DouyinDeviceRealRunExecuted",
            payload={
                "account_id": account_id,
                "lottery_id": lottery_id,
                "execution_evidence_id": gate.execution_evidence_id,
                "actions": list(plan.required_actions),
            },
            correlation_id=task_id,
        )
        if not event_id:
            raise RuntimeError("douyin_device_completion_event_failed")
        await runtime.save_phase(task_id, account_id, lottery_id, "completed")
    except (DouyinDevicePreflightBlocked, DouyinDeviceContractError) as exc:
        raise DouyinDeviceExecutionBlocked(str(exc)) from exc
    finally:
        await client.aclose()


DOUYIN = PlatformModule(
    platform_id="douyin",
    adapter_factory=lambda selectors=None: DouyinAdapter(selector_config=selectors),
    probe_handler=execute_douyin_device_probe,
    probe_authority_validator=validate_douyin_probe_authority,
    probe_claim_validator=validate_douyin_probe_claim,
    probe_terminal_materializer=materialize_douyin_terminal_probe,
    action_order=tuple(DOUYIN_ACTION_ORDER),
    real_target_kinds=frozenset({"video", "note"}),
    execution_paths=(
        ExecutionPath(
            path_id=DOUYIN_MANUAL_EXECUTION_PATH,
            credential_kind="browser_session",
            supported_modes=frozenset({"shadow_run"}),
            shadow_executor="browser_observation",
            shadow_handler=execute_browser_observation_shadow,
            selector_binding_modes=frozenset({"shadow_run"}),
            unsupported_mode_error="douyin_manual_shadow_only",
        ),
        ExecutionPath(
            path_id=DOUYIN_DEVICE_EXECUTION_PATH,
            credential_kind="device_agent",
            supported_modes=frozenset({"dry_run", "shadow_run", "real_run"}),
            shadow_executor="douyin_device_observation",
            real_executor="douyin_device",
            shadow_handler=execute_douyin_device_shadow,
            real_handler=execute_douyin_device_real,
            selector_binding_modes=frozenset({"shadow_run", "real_run"}),
            dry_run_requires_executable_plan=True,
            confirmed_intent_settlement=True,
            execution_evidence_kind="exact_execution_evidence",
        ),
    ),
    default_execution_path=DOUYIN_DEVICE_EXECUTION_PATH,
    invalid_execution_path_error=DOUYIN_ACTION_PLAN_CONTRACT.execution_path_error,
    capability_block_reason="douyin_device_agent_calibration_required",
    real_run_task_precondition=douyin_real_run_precondition,
    real_run_evidence_context_loader=load_douyin_real_run_evidence_context,
    real_run_evidence_validator=validate_douyin_real_run_evidence,
)
