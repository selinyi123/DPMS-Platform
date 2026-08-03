"""Xiaohongshu worker execution module."""

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
from app.adapters.xiaohongshu import XiaohongshuAdapter
from app.platform_modules.base import (
    ExecutionPath,
    PlatformModule,
    RealRunEvidenceBinding,
)
from app.platform_modules.contracts.xiaohongshu import (
    XIAOHONGSHU_ACTION_PLAN_CONTRACT,
    XIAOHONGSHU_BROWSER_EXECUTION_PATH,
    XIAOHONGSHU_MANUAL_EXECUTION_PATH,
    XIAOHONGSHU_REQUIRED_ACTIONS,
)
from app.platform_modules.evidence import (
    RealRunGateBlocked,
    json_object,
    required_int,
    row_value,
)
from app.platform_modules.shared_execution import (
    execute_browser_observation_probe,
    execute_browser_observation_shadow,
    execute_browser_real_task,
)
from app.services.execution_evidence import materialize_for_probe
from shared.xiaohongshu_browser_contract import (
    XIAOHONGSHU_BROWSER_CONTRACT_VERSION,
    XIAOHONGSHU_BROWSER_PROBE_OBSERVATION_KIND,
    XIAOHONGSHU_BROWSER_SHADOW_OBSERVATION_KIND,
    XiaohongshuBrowserContractError,
    compute_xiaohongshu_browser_config_hash,
    compute_xiaohongshu_comment_text_hash,
    format_xiaohongshu_observed_at,
    hash_xiaohongshu_browser_observation,
    validate_xiaohongshu_browser_observation_binding,
)


_XIAOHONGSHU_NOTE_CANONICAL_URI = re.compile(
    r"canonical://xiaohongshu/note/[0-9a-f]{24}\Z",
    re.ASCII,
)


def _xiaohongshu_observation(
    *,
    evidence_id: str,
    observation_kind: str,
    lottery_id: int,
    account_id: int,
    execution_revision: int,
    target_hash: str,
    plan,
    config_hash: str,
    capability_checks: Mapping[str, bool],
) -> dict[str, Any]:
    actions = list(plan.required_actions)
    complete = bool(actions) and all(
        capability_checks.get(action) is True for action in actions
    )
    comment_text = (
        plan.payload_for("commented").get("text", "")
        if "commented" in plan.required_actions
        else ""
    )
    return {
        "contract_version": XIAOHONGSHU_BROWSER_CONTRACT_VERSION,
        "platform": "xiaohongshu",
        "execution_path_id": XIAOHONGSHU_BROWSER_EXECUTION_PATH,
        "lottery_id": lottery_id,
        "account_id": account_id,
        "execution_revision": execution_revision,
        "target_hash": target_hash,
        "observed_target_hash": target_hash,
        "rule_snapshot_id": plan.rule_snapshot_id,
        "rule_hash": plan.rule_hash,
        "action_plan_hash": plan.plan_hash,
        "config_hash": config_hash,
        "required_actions": actions,
        "follow_target_handle": (
            plan.follow_target_handle if "followed" in actions else ""
        ),
        "comment_text_hash": compute_xiaohongshu_comment_text_hash(
            comment_text
        ),
        "observation_kind": observation_kind,
        "observed_at": format_xiaohongshu_observed_at(
            datetime.now(timezone.utc)
        ),
        "evidence_id": evidence_id,
        "side_effects": False,
        # The browser reached the exact note with an account-scoped cookie
        # context, no login/risk redirect, and every requested action control
        # was observable. Any missing control keeps this proof false.
        "account_authenticated": complete,
        "target_identity_verified": True,
        "selector_observation_complete": complete,
        "capability_checks": {
            action: capability_checks.get(action) is True
            for action in actions
        },
    }


def _probe_capability_checks(
    result: Mapping[str, Any],
    adapter: XiaohongshuAdapter,
    actions: tuple[str, ...],
) -> dict[str, bool]:
    summary = result.get("_summary")
    phase_status = (
        summary.get("phase_status", {})
        if isinstance(summary, Mapping)
        else {}
    )
    return {
        action: bool(
            adapter.supports_actions((action,))
            and isinstance(phase_status.get(action), Mapping)
            and phase_status[action].get("ready") is True
        )
        for action in actions
    }


def validate_xiaohongshu_probe_authority(
    authoritative_message: Mapping[str, Any],
) -> bool:
    try:
        execution_revision = int(
            authoritative_message.get("execution_revision")
        )
    except (TypeError, ValueError):
        return False
    hashes = (
        authoritative_message.get("target_hash"),
        authoritative_message.get("rule_hash"),
        authoritative_message.get("action_plan_hash"),
        authoritative_message.get("config_hash"),
    )
    return bool(
        execution_revision > 0
        and authoritative_message.get("execution_path_id")
        == XIAOHONGSHU_BROWSER_EXECUTION_PATH
        and all(
            isinstance(value, str)
            and re.fullmatch(r"[0-9a-f]{64}", value, re.ASCII)
            for value in hashes
        )
    )


async def validate_xiaohongshu_probe_claim(
    *,
    runtime,
    probe: dict,
    row,
    authoritative: dict,
) -> None:
    """Recompute the exact plan/config binding under the probe row lock."""

    try:
        plan = validate_action_plan_v2(
            row["lottery_action_plan"],
            require_executable=True,
            reject_media=True,
        )
        message_snapshot_id = int(probe.get("rule_snapshot_id"))
        message_revision = int(probe.get("execution_revision"))
        execution_revision = int(row["execution_revision"] or 0)
        selector_row = await runtime.database.fetch_one(
            """SELECT config_json FROM adapter_selector_configs
               WHERE platform = 'xiaohongshu' FOR UPDATE"""
        )
        selector_config = json_object(
            row_value(selector_row, "config_json"),
            code="xiaohongshu_selector_config_invalid",
        )
        config_hash = compute_xiaohongshu_browser_config_hash(
            execution_revision,
            selector_config,
        )
        target_hash = compute_target_hash(authoritative["canonical_url"])
    except (ActionPlanV2Error, XiaohongshuBrowserContractError) as exc:
        raise ValueError(f"adapter_probe_{exc.code}") from exc
    except (TypeError, ValueError, KeyError, RealRunGateBlocked) as exc:
        raise ValueError("adapter_probe_xiaohongshu_binding_invalid") from exc
    if (
        plan.execution_path_id != XIAOHONGSHU_BROWSER_EXECUTION_PATH
        or authoritative["execution_path_id"]
        != XIAOHONGSHU_BROWSER_EXECUTION_PATH
        or str(probe.get("execution_path_id") or "").strip()
        != XIAOHONGSHU_BROWSER_EXECUTION_PATH
        or authoritative["target_hash"] != target_hash
        or str(probe.get("target_hash") or "").strip() != target_hash
        or authoritative["rule_snapshot_id"] != plan.rule_snapshot_id
        or message_snapshot_id != plan.rule_snapshot_id
        or int(row["authoritative_rule_snapshot_id"] or 0)
        != plan.rule_snapshot_id
        or authoritative["rule_hash"] != plan.rule_hash
        or str(probe.get("rule_hash") or "").strip() != plan.rule_hash
        or str(row["lottery_rule_hash"] or "").strip() != plan.rule_hash
        or str(row["snapshot_rule_hash"] or "").strip() != plan.rule_hash
        or authoritative["action_plan_hash"] != plan.plan_hash
        or str(probe.get("action_plan_hash") or "").strip()
        != plan.plan_hash
        or str(row["lottery_action_plan_hash"] or "").strip()
        != plan.plan_hash
        or authoritative["config_hash"] != config_hash
        or str(probe.get("config_hash") or "").strip() != config_hash
        or message_revision != execution_revision
        or execution_revision <= 0
        or int(row["snapshot_complete"] or 0) != 1
        or not str(row["snapshot_attested_by"] or "").strip()
        or row["snapshot_attested_at"] is None
        or not XiaohongshuAdapter(
            selector_config=selector_config
        ).supports_actions(plan.required_actions)
    ):
        raise ValueError("adapter_probe_xiaohongshu_binding_mismatch")
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
    message_plan = probe.get("action_plan")
    if message_plan is not None:
        try:
            parsed_message_plan = validate_action_plan_v2(
                message_plan,
                require_executable=True,
                reject_media=True,
            )
        except ActionPlanV2Error as exc:
            raise ValueError(f"adapter_probe_message_{exc.code}") from exc
        if canonical_json_bytes(parsed_message_plan.plan) != canonical_json_bytes(
            plan.plan
        ):
            raise ValueError("adapter_probe_action_plan_mismatch")
    authoritative["action_plan"] = plan.plan
    authoritative["execution_revision"] = execution_revision
    authoritative["selector_config"] = selector_config


async def execute_xiaohongshu_browser_probe(
    binding: dict,
    pool,
    *,
    runtime,
):
    if (
        binding.get("platform") != "xiaohongshu"
        or binding.get("execution_path_id")
        != XIAOHONGSHU_BROWSER_EXECUTION_PATH
    ):
        raise RuntimeError("xiaohongshu_browser_probe_binding_invalid")
    plan = validate_action_plan_v2(
        binding.get("action_plan"),
        require_executable=True,
        reject_media=True,
    )
    generic = await execute_browser_observation_probe(
        binding,
        pool,
        runtime=runtime,
    )
    adapter = XiaohongshuAdapter(
        selector_config=binding.get("selector_config")
    )
    checks = _probe_capability_checks(
        generic.result,
        adapter,
        plan.required_actions,
    )
    observation = _xiaohongshu_observation(
        evidence_id=str(binding.get("probe_id") or "").strip(),
        observation_kind=XIAOHONGSHU_BROWSER_PROBE_OBSERVATION_KIND,
        lottery_id=int(binding["lottery_id"]),
        account_id=int(binding["account_id"]),
        execution_revision=int(binding["execution_revision"]),
        target_hash=str(binding.get("target_hash") or "").strip(),
        plan=plan,
        config_hash=str(binding.get("config_hash") or "").strip(),
        capability_checks=checks,
    )
    observation_hash = hash_xiaohongshu_browser_observation(observation)
    return runtime.ProbeObservation(
        result=observation,
        observation_kind=XIAOHONGSHU_BROWSER_PROBE_OBSERVATION_KIND,
        observation_hash=observation_hash,
        success_event_type="AdapterXiaohongshuBrowserProbeSucceeded",
        success_event_payload={
            "probe_kind": XIAOHONGSHU_BROWSER_PROBE_OBSERVATION_KIND,
            "observation_hash": observation_hash,
            "required_actions": list(plan.required_actions),
            "side_effects": False,
        },
        materialize_execution_evidence=True,
    )


async def materialize_xiaohongshu_terminal_probe(
    *,
    probe_id: str,
    runtime,
) -> None:
    await materialize_for_probe(
        db=runtime.database,
        probe_id=probe_id,
    )


XIAOHONGSHU_REAL_RUN_EVIDENCE_QUERY = """
SELECT
  e.id AS evidence_id,
  e.lottery_id AS evidence_lottery_id,
  e.account_id AS evidence_account_id,
  e.platform AS evidence_platform,
  e.rule_snapshot_id AS evidence_rule_snapshot_id,
  e.execution_path_id AS evidence_execution_path_id,
  e.target_hash AS evidence_target_hash,
  e.rule_hash AS evidence_rule_hash,
  e.action_plan_hash AS evidence_action_plan_hash,
  e.config_hash AS evidence_config_hash,
  e.probe_id AS evidence_probe_id,
  e.shadow_task_id AS evidence_shadow_task_id,
  e.probe_observation_kind AS evidence_probe_observation_kind,
  e.probe_observation_hash AS evidence_probe_observation_hash,
  e.shadow_observation_kind AS evidence_shadow_observation_kind,
  e.shadow_observation_hash AS evidence_shadow_observation_hash,
  e.status AS evidence_status,
  e.verified_at AS evidence_verified_at,
  e.expires_at AS evidence_expires_at,
  CASE WHEN e.expires_at > NOW() THEN 1 ELSE 0 END AS evidence_active,
  CASE WHEN e.verified_at >= GREATEST(ac.finished_at, shadow.finished_at)
             AND e.verified_at <= NOW()
             AND e.expires_at <= LEAST(
               DATE_ADD(ac.finished_at, INTERVAL 24 HOUR),
               DATE_ADD(shadow.finished_at, INTERVAL 24 HOUR)
             )
             AND e.expires_at > NOW()
       THEN 1 ELSE 0 END AS evidence_time_bounded,
  ac.status AS evidence_probe_status,
  ac.result AS evidence_probe_observation,
  ac.observation_kind AS source_probe_observation_kind,
  ac.observation_hash AS source_probe_observation_hash,
  ac.finished_at AS evidence_probe_finished_at,
  CASE WHEN ac.finished_at >= DATE_SUB(NOW(), INTERVAL 24 HOUR)
             AND ac.finished_at <= NOW() THEN 1 ELSE 0 END AS evidence_probe_fresh,
  CASE WHEN probe_lease.released_at >= ac.finished_at
             AND probe_lease.released_at <= NOW()
             AND probe_lease.operation_kind = 'adapter_probe'
             AND probe_lease.owner_id = ac.probe_id
             AND probe_lease.task_id IS NULL
       THEN 1 ELSE 0 END AS evidence_probe_lease_released,
  CASE WHEN ac.started_at IS NOT NULL
             AND probe_lease.acquired_at <= ac.started_at
             AND ac.started_at <= ac.finished_at
             AND probe_lease.expires_at >= ac.finished_at
             AND probe_lease.released_at >= ac.finished_at
             AND probe_lease.released_at <= NOW()
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
             AND shadow_lease.released_at <= NOW()
       THEN 1 ELSE 0 END AS evidence_shadow_lease_covers_observation,
  shadow.target_hash AS evidence_shadow_target_hash,
  shadow.config_hash AS evidence_shadow_config_hash
FROM task_runs tr
LEFT JOIN execution_evidence_bindings e
  ON e.id = tr.execution_evidence_id
 AND e.lottery_id = tr.lottery_id
 AND e.account_id = tr.account_id
 AND e.rule_snapshot_id = tr.rule_snapshot_id
 AND e.execution_path_id = tr.execution_path_id
 AND e.target_hash = tr.target_hash
 AND e.rule_hash = tr.rule_hash
 AND e.action_plan_hash = tr.action_plan_hash
 AND e.config_hash = tr.config_hash
LEFT JOIN adapter_calibrations ac
  ON ac.probe_id = e.probe_id
 AND ac.lottery_id = e.lottery_id
 AND ac.account_id = e.account_id
 AND ac.platform = e.platform
 AND ac.rule_snapshot_id = e.rule_snapshot_id
 AND ac.execution_path_id = e.execution_path_id
 AND ac.target_hash = e.target_hash
 AND ac.rule_hash = e.rule_hash
 AND ac.action_plan_hash = e.action_plan_hash
 AND ac.config_hash = e.config_hash
 AND ac.observation_kind = e.probe_observation_kind
 AND ac.observation_hash = e.probe_observation_hash
LEFT JOIN task_runs shadow
  ON shadow.task_id = e.shadow_task_id
 AND shadow.lottery_id = e.lottery_id
 AND shadow.account_id = e.account_id
 AND shadow.rule_snapshot_id = e.rule_snapshot_id
 AND shadow.execution_path_id = e.execution_path_id
 AND shadow.target_hash = e.target_hash
 AND shadow.rule_hash = e.rule_hash
 AND shadow.action_plan_hash = e.action_plan_hash
 AND shadow.config_hash = e.config_hash
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


_XIAOHONGSHU_REAL_RUN_EVIDENCE_FIELDS = (
    "evidence_id",
    "evidence_lottery_id",
    "evidence_account_id",
    "evidence_platform",
    "evidence_rule_snapshot_id",
    "evidence_execution_path_id",
    "evidence_target_hash",
    "evidence_rule_hash",
    "evidence_action_plan_hash",
    "evidence_config_hash",
    "evidence_probe_id",
    "evidence_shadow_task_id",
    "evidence_probe_observation_kind",
    "evidence_probe_observation_hash",
    "evidence_shadow_observation_kind",
    "evidence_shadow_observation_hash",
    "evidence_status",
    "evidence_verified_at",
    "evidence_expires_at",
    "evidence_active",
    "evidence_time_bounded",
    "evidence_probe_status",
    "evidence_probe_observation",
    "source_probe_observation_kind",
    "source_probe_observation_hash",
    "evidence_probe_finished_at",
    "evidence_probe_fresh",
    "evidence_probe_lease_released",
    "evidence_probe_lease_covers_observation",
    "evidence_shadow_status",
    "evidence_shadow_task_mode",
    "evidence_shadow_observation",
    "source_shadow_observation_kind",
    "source_shadow_observation_hash",
    "evidence_shadow_finished_at",
    "evidence_shadow_fresh",
    "evidence_shadow_lease_released",
    "evidence_shadow_lease_covers_observation",
    "evidence_shadow_target_hash",
    "evidence_shadow_config_hash",
)


async def load_xiaohongshu_real_run_evidence_context(
    *,
    db,
    task_id: str,
) -> dict[str, Any]:
    row = await db.fetch_one(
        XIAOHONGSHU_REAL_RUN_EVIDENCE_QUERY,
        {"task_id": task_id},
    )
    return {
        field: row_value(row, field)
        for field in _XIAOHONGSHU_REAL_RUN_EVIDENCE_FIELDS
    }


def _xiaohongshu_real_run_precondition(task: Mapping[str, Any]) -> str | None:
    if str(task.get("platform") or "").strip().lower() != "xiaohongshu":
        return "xiaohongshu_task_platform_mismatch"
    canonical_url = str(task.get("canonical_url") or "").strip()
    if not _XIAOHONGSHU_NOTE_CANONICAL_URI.fullmatch(canonical_url):
        return "xiaohongshu_note_target_required"
    return None


def _observation_expected_values(
    *,
    plan,
    account_id: int,
    lottery_id: int,
    execution_revision: int,
    target_hash: str,
    config_hash: str,
) -> dict[str, Any]:
    comment_text = (
        plan.payload_for("commented").get("text", "")
        if "commented" in plan.required_actions
        else ""
    )
    return {
        "expected_lottery_id": lottery_id,
        "expected_account_id": account_id,
        "expected_execution_revision": execution_revision,
        "expected_target_hash": target_hash,
        "expected_rule_snapshot_id": plan.rule_snapshot_id,
        "expected_rule_hash": plan.rule_hash,
        "expected_action_plan_hash": plan.plan_hash,
        "expected_config_hash": config_hash,
        "expected_actions": plan.required_actions,
        "expected_follow_target_handle": (
            plan.follow_target_handle
            if "followed" in plan.required_actions
            else ""
        ),
        "expected_comment_text_hash": compute_xiaohongshu_comment_text_hash(
            comment_text
        ),
    }


def validate_xiaohongshu_real_run_evidence(
    *,
    task,
    row,
    account_id: int,
    lottery_id: int,
    platform: str,
    plan,
    execution_plan=None,
) -> RealRunEvidenceBinding:
    """Validate one exact, fresh Probe + Shadow browser evidence pair."""

    if (
        platform != "xiaohongshu"
        or plan.execution_path_id != XIAOHONGSHU_BROWSER_EXECUTION_PATH
    ):
        raise RealRunGateBlocked("execution_path_not_supported")
    execution_revision = required_int(
        row_value(row, "account_execution_revision"),
        code="account_execution_revision_invalid",
    )
    try:
        selector_config = json_object(
            task.get("selector_config"),
            code="xiaohongshu_selector_config_invalid",
        )
        expected_config_hash = compute_xiaohongshu_browser_config_hash(
            execution_revision,
            selector_config,
        )
        expected_target_hash = compute_target_hash(
            str(row_value(row, "lottery_canonical_url") or "").strip()
        )
        observation_expected = _observation_expected_values(
            plan=plan,
            account_id=account_id,
            lottery_id=lottery_id,
            execution_revision=execution_revision,
            target_hash=expected_target_hash,
            config_hash=expected_config_hash,
        )
    except XiaohongshuBrowserContractError as exc:
        raise RealRunGateBlocked(exc.code) from exc

    message_evidence_id = str(task.get("execution_evidence_id") or "").strip()
    task_evidence_id = str(
        row_value(row, "task_execution_evidence_id") or ""
    ).strip()
    evidence_id = str(row_value(row, "evidence_id") or "").strip()
    try:
        evidence_account_id = int(row_value(row, "evidence_account_id"))
        evidence_lottery_id = int(row_value(row, "evidence_lottery_id"))
        evidence_snapshot_id = int(
            row_value(row, "evidence_rule_snapshot_id")
        )
    except (TypeError, ValueError) as exc:
        raise RealRunGateBlocked("execution_evidence_binding_invalid") from exc
    if (
        not evidence_id
        or message_evidence_id != evidence_id
        or task_evidence_id != evidence_id
        or evidence_account_id != account_id
        or evidence_lottery_id != lottery_id
        or evidence_snapshot_id != plan.rule_snapshot_id
        or str(row_value(row, "evidence_platform") or "").strip().lower()
        != platform
        or str(row_value(row, "evidence_execution_path_id") or "").strip()
        != XIAOHONGSHU_BROWSER_EXECUTION_PATH
        or str(row_value(row, "evidence_rule_hash") or "").strip()
        != plan.rule_hash
        or str(row_value(row, "evidence_action_plan_hash") or "").strip()
        != plan.plan_hash
        or str(row_value(row, "evidence_target_hash") or "").strip()
        != expected_target_hash
        or str(task.get("target_hash") or "").strip() != expected_target_hash
        or str(row_value(row, "task_target_hash") or "").strip()
        != expected_target_hash
        or str(row_value(row, "evidence_config_hash") or "").strip()
        != expected_config_hash
        or str(task.get("config_hash") or "").strip() != expected_config_hash
        or str(row_value(row, "task_config_hash") or "").strip()
        != expected_config_hash
    ):
        raise RealRunGateBlocked("execution_evidence_binding_invalid")
    if (
        str(row_value(row, "evidence_status") or "").strip().lower()
        != "verified"
        or row_value(row, "evidence_verified_at") is None
        or row_value(row, "evidence_expires_at") is None
        or int(row_value(row, "evidence_active", 0) or 0) != 1
        or int(row_value(row, "evidence_time_bounded", 0) or 0) != 1
    ):
        raise RealRunGateBlocked("execution_evidence_not_active")

    probe_id = str(row_value(row, "evidence_probe_id") or "").strip()
    shadow_task_id = str(
        row_value(row, "evidence_shadow_task_id") or ""
    ).strip()
    if (
        not probe_id
        or not shadow_task_id
        or str(row_value(row, "evidence_probe_status") or "").strip().lower()
        != "succeeded"
        or row_value(row, "evidence_probe_finished_at") is None
        or str(row_value(row, "evidence_shadow_status") or "").strip().lower()
        != "succeeded"
        or str(row_value(row, "evidence_shadow_task_mode") or "").strip().lower()
        != "shadow_run"
        or str(row_value(row, "evidence_shadow_target_hash") or "").strip()
        != expected_target_hash
        or str(row_value(row, "evidence_shadow_config_hash") or "").strip()
        != expected_config_hash
        or int(row_value(row, "evidence_probe_fresh", 0) or 0) != 1
        or int(row_value(row, "evidence_shadow_fresh", 0) or 0) != 1
        or int(row_value(row, "evidence_probe_lease_released", 0) or 0) != 1
        or int(
            row_value(row, "evidence_probe_lease_covers_observation", 0)
            or 0
        )
        != 1
        or int(row_value(row, "evidence_shadow_lease_released", 0) or 0) != 1
        or int(
            row_value(row, "evidence_shadow_lease_covers_observation", 0)
            or 0
        )
        != 1
    ):
        raise RealRunGateBlocked("probe_shadow_evidence_incomplete")

    try:
        validate_xiaohongshu_browser_observation_binding(
            row_value(row, "evidence_probe_observation"),
            expected_observation_kind=(
                XIAOHONGSHU_BROWSER_PROBE_OBSERVATION_KIND
            ),
            expected_evidence_id=probe_id,
            source_observation_kind=str(
                row_value(row, "source_probe_observation_kind") or ""
            ).strip(),
            source_observation_hash=str(
                row_value(row, "source_probe_observation_hash") or ""
            ).strip(),
            evidence_observation_kind=str(
                row_value(row, "evidence_probe_observation_kind") or ""
            ).strip(),
            evidence_observation_hash=str(
                row_value(row, "evidence_probe_observation_hash") or ""
            ).strip(),
            **observation_expected,
        )
        validate_xiaohongshu_browser_observation_binding(
            row_value(row, "evidence_shadow_observation"),
            expected_observation_kind=(
                XIAOHONGSHU_BROWSER_SHADOW_OBSERVATION_KIND
            ),
            expected_evidence_id=shadow_task_id,
            source_observation_kind=str(
                row_value(row, "source_shadow_observation_kind") or ""
            ).strip(),
            source_observation_hash=str(
                row_value(row, "source_shadow_observation_hash") or ""
            ).strip(),
            evidence_observation_kind=str(
                row_value(row, "evidence_shadow_observation_kind") or ""
            ).strip(),
            evidence_observation_hash=str(
                row_value(row, "evidence_shadow_observation_hash") or ""
            ).strip(),
            **observation_expected,
        )
    except XiaohongshuBrowserContractError as exc:
        raise RealRunGateBlocked(exc.code) from exc
    return RealRunEvidenceBinding(
        evidence_id=evidence_id,
        execution_revision=execution_revision,
    )


async def execute_xiaohongshu_browser_real_task(
    task: dict,
    adapter,
    pool,
    *,
    runtime,
):
    """Bind the reviewed plan before entering the shared browser lifecycle."""

    plan = runtime.validate_action_plan_v2(
        task.get("action_plan"),
        require_executable=True,
        reject_media=True,
    )
    if (
        str(task.get("platform") or "").strip().lower() != "xiaohongshu"
        or plan.execution_path_id != XIAOHONGSHU_BROWSER_EXECUTION_PATH
    ):
        raise RuntimeError("xiaohongshu_browser_task_binding_invalid")
    if not isinstance(adapter, XiaohongshuAdapter):
        raise RuntimeError("xiaohongshu_browser_adapter_required")
    if not adapter.supports_actions(plan.required_actions):
        raise RuntimeError("xiaohongshu_browser_selectors_incomplete")
    if "commented" in plan.required_actions:
        adapter.bind_reviewed_comment_text(
            plan.payload_for("commented").get("text")
        )
    return await execute_browser_real_task(
        task,
        adapter,
        pool,
        runtime=runtime,
    )


async def execute_xiaohongshu_browser_shadow_task(
    task: dict,
    adapter,
    pool,
    *,
    runtime,
):
    """Persist a read-only shared-schema observation for evidence pairing."""

    plan = runtime.validate_action_plan_v2(
        task.get("action_plan"),
        require_executable=True,
        reject_media=True,
    )
    if (
        plan.execution_path_id != XIAOHONGSHU_BROWSER_EXECUTION_PATH
        or not isinstance(adapter, XiaohongshuAdapter)
        or not adapter.supports_actions(plan.required_actions)
    ):
        raise RuntimeError("xiaohongshu_browser_shadow_binding_invalid")
    screenshot_path = await execute_browser_observation_shadow(
        task,
        adapter,
        pool,
        runtime=runtime,
    )
    shadow_context = getattr(
        adapter, "_last_shadow_observation_context", None
    )
    if not isinstance(shadow_context, Mapping):
        raise RuntimeError("xiaohongshu_shadow_observation_missing")
    capability_checks = {
        action: bool(
            adapter.supports_actions((action,))
            and isinstance(
                shadow_context.get("capability_checks"), Mapping
            )
            and shadow_context["capability_checks"].get(action) is True
        )
        for action in plan.required_actions
    }
    task_id = str(task.get("task_id") or "").strip()
    account_id = int(task.get("account_id"))
    lottery_id = int(task.get("lottery_id"))
    selector_config = adapter.configured_selectors
    async with runtime.database.transaction():
        current = await runtime.database.fetch_one(
            """SELECT tr.task_id, tr.status, tr.worker_id, tr.account_id,
                      tr.lottery_id, tr.execution_path_id, tr.rule_snapshot_id,
                      tr.target_hash, tr.rule_hash, tr.action_plan_hash,
                      tr.config_hash, tr.reconciliation_required,
                      a.platform AS account_platform,
                      a.execution_revision,
                      lease.operation_kind, lease.owner_id,
                      lease.task_id AS lease_task_id,
                      CASE WHEN lease.expires_at > NOW() THEN 1 ELSE 0 END
                        AS lease_active,
                      CASE WHEN lease.released_at IS NULL THEN 1 ELSE 0 END
                        AS lease_unreleased,
                      CASE WHEN lease.generation = (
                        SELECT MAX(newest.generation)
                          FROM account_operation_leases newest
                         WHERE newest.account_id = tr.account_id
                      ) THEN 1 ELSE 0 END AS lease_latest_generation,
                      (SELECT COUNT(*) FROM account_operation_leases live
                        WHERE live.account_id = tr.account_id
                          AND live.released_at IS NULL
                          AND live.expires_at > NOW())
                        AS active_account_lease_count
               FROM task_runs tr
               JOIN accounts a ON a.id = tr.account_id
               LEFT JOIN account_operation_leases lease
                 ON lease.lease_id = tr.account_lease_id
                AND lease.account_id = tr.account_id
                AND lease.generation = tr.account_lease_generation
               WHERE tr.task_id = :task_id FOR UPDATE""",
            {"task_id": task_id},
        )
        try:
            execution_revision = int(
                runtime.row_get(current, "execution_revision") or 0
            )
            target_hash = compute_target_hash(
                str(task.get("canonical_url") or "").strip()
            )
            config_hash = compute_xiaohongshu_browser_config_hash(
                execution_revision,
                selector_config,
            )
        except (TypeError, ValueError, XiaohongshuBrowserContractError) as exc:
            raise runtime.TaskOwnershipLost(
                "xiaohongshu_shadow_binding_invalid"
            ) from exc
        if (
            not current
            or runtime.row_get(current, "task_id") != task_id
            or str(runtime.row_get(current, "status") or "").strip().lower()
            != "running"
            or str(runtime.row_get(current, "worker_id") or "").strip()
            != runtime.WORKER_ID
            or int(runtime.row_get(current, "account_id") or 0)
            != account_id
            or int(runtime.row_get(current, "lottery_id") or 0)
            != lottery_id
            or str(runtime.row_get(current, "account_platform") or "")
            .strip()
            .lower()
            != "xiaohongshu"
            or execution_revision <= 0
            or str(runtime.row_get(current, "execution_path_id") or "").strip()
            != XIAOHONGSHU_BROWSER_EXECUTION_PATH
            or int(runtime.row_get(current, "rule_snapshot_id") or 0)
            != plan.rule_snapshot_id
            or str(runtime.row_get(current, "target_hash") or "").strip()
            != target_hash
            or str(runtime.row_get(current, "rule_hash") or "").strip()
            != plan.rule_hash
            or str(runtime.row_get(current, "action_plan_hash") or "").strip()
            != plan.plan_hash
            or str(runtime.row_get(current, "config_hash") or "").strip()
            != config_hash
            or str(task.get("target_hash") or "").strip() != target_hash
            or str(task.get("config_hash") or "").strip() != config_hash
            or str(runtime.row_get(current, "operation_kind") or "")
            .strip()
            .lower()
            != "shadow_run"
            or str(runtime.row_get(current, "owner_id") or "").strip()
            != task_id
            or str(runtime.row_get(current, "lease_task_id") or "").strip()
            != task_id
            or int(runtime.row_get(current, "lease_active", 0) or 0) != 1
            or int(runtime.row_get(current, "lease_unreleased", 0) or 0)
            != 1
            or int(runtime.row_get(current, "lease_latest_generation", 0) or 0)
            != 1
            or int(runtime.row_get(current, "active_account_lease_count", 0) or 0)
            != 1
            or int(runtime.row_get(current, "reconciliation_required", 0) or 0)
            != 0
        ):
            raise runtime.TaskOwnershipLost(
                "xiaohongshu_shadow_binding_changed"
            )
        observation = _xiaohongshu_observation(
            evidence_id=task_id,
            observation_kind=XIAOHONGSHU_BROWSER_SHADOW_OBSERVATION_KIND,
            lottery_id=lottery_id,
            account_id=account_id,
            execution_revision=execution_revision,
            target_hash=target_hash,
            plan=plan,
            config_hash=config_hash,
            capability_checks=capability_checks,
        )
        observation_hash = hash_xiaohongshu_browser_observation(observation)
        observation_json = runtime.canonical_json_bytes(observation).decode(
            "utf-8", errors="strict"
        )
        await runtime.database.execute(
            """UPDATE task_runs
                  SET preflight_observation = :observation,
                      preflight_observation_kind = :kind,
                      preflight_observation_hash = :observation_hash
                WHERE task_id = :task_id AND status = 'running'
                  AND worker_id = :worker_id""",
            {
                "task_id": task_id,
                "worker_id": runtime.WORKER_ID,
                "observation": observation_json,
                "kind": XIAOHONGSHU_BROWSER_SHADOW_OBSERVATION_KIND,
                "observation_hash": observation_hash,
            },
        )
        persisted = await runtime.database.fetch_one(
            """SELECT preflight_observation, preflight_observation_kind,
                      preflight_observation_hash
                 FROM task_runs WHERE task_id = :task_id""",
            {"task_id": task_id},
        )
        if (
            not persisted
            or str(
                runtime.row_get(
                    persisted, "preflight_observation_kind"
                )
                or ""
            ).strip()
            != XIAOHONGSHU_BROWSER_SHADOW_OBSERVATION_KIND
            or str(
                runtime.row_get(
                    persisted, "preflight_observation_hash"
                )
                or ""
            ).strip()
            != observation_hash
            or runtime.parse_json_field(
                runtime.row_get(persisted, "preflight_observation")
            )
            != observation
        ):
            raise RuntimeError(
                "xiaohongshu_shadow_observation_persistence_failed"
            )
    event_id = await runtime.record_event(
        aggregate="task",
        aggregate_id=task_id,
        event_type="TaskXiaohongshuBrowserShadowObserved",
        payload={
            "account_id": account_id,
            "lottery_id": lottery_id,
            "observation_kind": (
                XIAOHONGSHU_BROWSER_SHADOW_OBSERVATION_KIND
            ),
            "observation_hash": observation_hash,
            "required_actions": list(plan.required_actions),
            "side_effects": False,
        },
        correlation_id=task_id,
    )
    if not event_id:
        raise RuntimeError(
            "xiaohongshu_shadow_observation_event_persistence_failed"
        )
    return screenshot_path


XIAOHONGSHU = PlatformModule(
    platform_id="xiaohongshu",
    adapter_factory=lambda selectors=None: XiaohongshuAdapter(
        selector_config=selectors
    ),
    probe_handler=execute_xiaohongshu_browser_probe,
    probe_authority_validator=validate_xiaohongshu_probe_authority,
    probe_claim_validator=validate_xiaohongshu_probe_claim,
    probe_terminal_materializer=materialize_xiaohongshu_terminal_probe,
    action_order=tuple(XIAOHONGSHU_REQUIRED_ACTIONS),
    real_target_kinds=frozenset({"note"}),
    execution_paths=(
        ExecutionPath(
            path_id=XIAOHONGSHU_MANUAL_EXECUTION_PATH,
            credential_kind="browser_session",
            supported_modes=frozenset({"shadow_run"}),
            shadow_executor="browser_observation",
            shadow_handler=execute_browser_observation_shadow,
            selector_binding_modes=frozenset({"shadow_run"}),
            unsupported_mode_error="xiaohongshu_manual_shadow_only",
        ),
        ExecutionPath(
            path_id=XIAOHONGSHU_BROWSER_EXECUTION_PATH,
            credential_kind="browser_session",
            supported_modes=frozenset({"dry_run", "shadow_run", "real_run"}),
            shadow_executor="browser_observation",
            real_executor="xiaohongshu_browser",
            shadow_handler=execute_xiaohongshu_browser_shadow_task,
            real_handler=execute_xiaohongshu_browser_real_task,
            selector_binding_modes=frozenset({"shadow_run", "real_run"}),
            dry_run_requires_executable_plan=True,
            confirmed_intent_settlement=True,
            execution_evidence_kind="exact_execution_evidence",
        ),
    ),
    default_execution_path=XIAOHONGSHU_BROWSER_EXECUTION_PATH,
    invalid_execution_path_error=(
        XIAOHONGSHU_ACTION_PLAN_CONTRACT.execution_path_error
    ),
    capability_block_reason="xiaohongshu_no_official_interaction_api",
    real_run_task_precondition=_xiaohongshu_real_run_precondition,
    real_run_evidence_context_loader=(
        load_xiaohongshu_real_run_evidence_context
    ),
    real_run_evidence_validator=validate_xiaohongshu_real_run_evidence,
)
