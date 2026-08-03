"""Bilibili worker execution module."""

import asyncio
import json
from typing import Any, Mapping

from app.action_plan import (
    ActionPlanV2Error,
    ValidatedActionPlanV2,
    canonical_json_bytes,
    compute_bilibili_api_config_hash,
    compute_rule_hash,
    compute_target_hash,
    validate_action_plan_v2,
)
from app.adapters.bilibili import BilibiliAdapter
from app.bilibili.preflight import (
    API_PREFLIGHT_KIND,
    bilibili_author_handle,
    run_readonly_api_preflight,
    validate_preflight_observation,
)
from app.bilibili.client import (
    BilibiliApiActionOutcomeUnknown,
    BilibiliApiClient,
)
from app.bilibili.config import BiliEngineConfig
from app.bilibili.executor import BilibiliApiExecutor
from app.bilibili.runtime import (
    API_TO_DPMS_PHASE,
    account_status_for_results,
    dpms_phases_to_api_actions,
    extract_bilibili_dynamic_id,
    parse_detail_card,
    validate_card_for_actions,
)
from app.platform_modules.base import (
    ExecutionPath,
    PlatformModule,
    RealRunEvidenceBinding,
)
from app.platform_modules.contracts.bilibili import (
    BILIBILI_ACTION_PLAN_CONTRACT,
    BILIBILI_ACTION_ORDER,
    BILIBILI_API_EXECUTION_PATH,
)
from app.platform_modules.evidence import (
    RealRunGateBlocked,
    json_object,
    required_int,
    row_value,
)
from app.platform_modules.errors import (
    BilibiliActionSettlementFailed,
    BilibiliForwardedTargetRequiresReview,
)
from app.services.execution_evidence import materialize_for_probe


PHASE_ORDER = list(BILIBILI_ACTION_ORDER)


BILIBILI_REAL_RUN_EVIDENCE_QUERY = """
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

_BILIBILI_REAL_RUN_EVIDENCE_FIELDS = (
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


async def load_bilibili_real_run_evidence_context(
    *,
    db,
    task_id: str,
) -> dict[str, Any]:
    """Load the exact Bilibili probe/shadow evidence for one task."""

    row = await db.fetch_one(
        BILIBILI_REAL_RUN_EVIDENCE_QUERY,
        {"task_id": task_id},
    )
    return {
        field: row_value(row, field)
        for field in _BILIBILI_REAL_RUN_EVIDENCE_FIELDS
    }


async def execute_bilibili_api_shadow(
    task: dict,
    adapter,
    pool,
    *,
    runtime,
):
    """Persist one GET-only Bilibili API observation."""

    task_id = str(task.get("task_id") or "").strip()
    account_id = int(task.get("account_id"))
    lottery_id = int(task.get("lottery_id"))
    try:
        plan = runtime.validate_action_plan_v2(
            task.get("action_plan"),
            reject_media=True,
        )
    except runtime.ActionPlanV2Error as exc:
        raise RuntimeError(
            f"bilibili_shadow_{exc.code}"
        ) from exc
    if plan.execution_path_id != BILIBILI_API_EXECUTION_PATH:
        raise RuntimeError(
            "bilibili_shadow_execution_path_not_supported"
        )
    dynamic_id = extract_bilibili_dynamic_id(
        task.get("raw_url"),
        task.get("canonical_url"),
    )
    authority = await runtime.database.fetch_one(
        """SELECT execution_revision, platform, status
           FROM accounts WHERE id = :account_id""",
        {"account_id": account_id},
    )
    execution_revision = runtime._claim_positive_int(
        runtime.row_get(authority, "execution_revision"),
        "bilibili_shadow_execution_revision_invalid",
    )
    config_hash = compute_bilibili_api_config_hash(
        execution_revision
    )
    if (
        str(runtime.row_get(authority, "platform") or "")
        .strip()
        .lower()
        != "bilibili"
        or str(runtime.row_get(authority, "status") or "")
        .strip()
        .lower()
        != "ready"
        or str(task.get("config_hash") or "").strip()
        != config_hash
        or str(task.get("target_hash") or "").strip()
        != runtime.compute_target_hash(
            str(task.get("canonical_url") or "").strip()
        )
    ):
        raise RuntimeError("bilibili_shadow_authority_changed")
    credential = await runtime.load_account_credential(account_id)
    cookie_header = runtime.credential_to_cookie_header(credential)
    if not cookie_header:
        raise RuntimeError(
            f"Account {account_id} has no usable Bilibili Cookie"
        )
    preflight = await run_readonly_api_preflight(
        cookie_header=cookie_header,
        dynamic_id=dynamic_id,
        required_actions=plan.required_actions,
        execution_revision=execution_revision,
        config_hash=config_hash,
        expected_follow_handle=(
            plan.follow_target_handle
            if "followed" in plan.required_actions
            else None
        ),
    )
    observation_json = json.dumps(
        preflight.observation,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    async with runtime.database.transaction():
        current = await runtime.database.fetch_one(
            """SELECT tr.status, tr.worker_id, tr.account_id, tr.lottery_id,
                      tr.execution_path_id, tr.target_hash, tr.rule_hash,
                      tr.action_plan_hash, tr.config_hash,
                      tr.account_lease_id, tr.account_lease_generation,
                      a.execution_revision,
                       CASE WHEN lease.expires_at > NOW() THEN 1 ELSE 0 END AS lease_active,
                       CASE WHEN lease.released_at IS NULL THEN 1 ELSE 0 END AS lease_unreleased,
                       CASE WHEN lease.generation = (
                         SELECT MAX(newest.generation)
                         FROM account_operation_leases newest
                         WHERE newest.account_id = tr.account_id
                       ) THEN 1 ELSE 0 END AS lease_latest_generation,
                       (SELECT COUNT(*) FROM account_operation_leases live
                        WHERE live.account_id = tr.account_id
                          AND live.released_at IS NULL
                          AND live.expires_at > NOW()) AS active_account_lease_count,
                       lease.operation_kind, lease.owner_id, lease.task_id AS lease_task_id
               FROM task_runs tr
               JOIN accounts a ON a.id = tr.account_id
               JOIN account_operation_leases lease
                 ON lease.lease_id = tr.account_lease_id
                AND lease.account_id = tr.account_id
                AND lease.generation = tr.account_lease_generation
               WHERE tr.task_id = :task_id
               FOR UPDATE""",
            {"task_id": task_id},
        )
        if (
            not current
            or str(runtime.row_get(current, "status") or "")
            .strip()
            .lower()
            != "running"
            or str(runtime.row_get(current, "worker_id") or "").strip()
            != runtime.WORKER_ID
            or int(runtime.row_get(current, "account_id") or 0)
            != account_id
            or int(runtime.row_get(current, "lottery_id") or 0)
            != lottery_id
            or str(
                runtime.row_get(current, "execution_path_id") or ""
            ).strip()
            != BILIBILI_API_EXECUTION_PATH
            or str(runtime.row_get(current, "target_hash") or "").strip()
            != str(task.get("target_hash") or "").strip()
            or str(runtime.row_get(current, "rule_hash") or "").strip()
            != plan.rule_hash
            or str(
                runtime.row_get(current, "action_plan_hash") or ""
            ).strip()
            != plan.plan_hash
            or str(runtime.row_get(current, "config_hash") or "").strip()
            != config_hash
            or int(
                runtime.row_get(current, "execution_revision") or 0
            )
            != execution_revision
            or str(
                runtime.row_get(current, "account_lease_id") or ""
            ).strip()
            != str(task.get("account_lease_id") or "").strip()
            or int(
                runtime.row_get(
                    current,
                    "account_lease_generation",
                )
                or 0
            )
            != runtime._claim_positive_int(
                task.get("account_lease_generation"),
                "bilibili_shadow_account_lease_binding_invalid",
            )
            or str(
                runtime.row_get(current, "operation_kind") or ""
            )
            .strip()
            .lower()
            != "shadow_run"
            or str(runtime.row_get(current, "owner_id") or "").strip()
            != task_id
            or str(
                runtime.row_get(current, "lease_task_id") or ""
            ).strip()
            != task_id
            or int(
                runtime.row_get(current, "lease_active", 0) or 0
            )
            != 1
            or int(
                runtime.row_get(current, "lease_unreleased", 0) or 0
            )
            != 1
            or int(
                runtime.row_get(
                    current,
                    "lease_latest_generation",
                    0,
                )
                or 0
            )
            != 1
            or int(
                runtime.row_get(
                    current,
                    "active_account_lease_count",
                    0,
                )
                or 0
            )
            != 1
        ):
            raise runtime.TaskOwnershipLost(
                "bilibili_shadow_binding_changed"
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
                "kind": API_PREFLIGHT_KIND,
                "observation_hash": preflight.observation_hash,
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
                    persisted,
                    "preflight_observation_kind",
                )
                or ""
            ).strip()
            != API_PREFLIGHT_KIND
            or str(
                runtime.row_get(
                    persisted,
                    "preflight_observation_hash",
                )
                or ""
            ).strip()
            != preflight.observation_hash
            or runtime.parse_json_field(
                runtime.row_get(
                    persisted,
                    "preflight_observation",
                )
            )
            != preflight.observation
        ):
            raise RuntimeError(
                "bilibili_shadow_observation_persistence_failed"
            )
    event_id = await runtime.record_event(
        aggregate="task",
        aggregate_id=task_id,
        event_type="TaskApiShadowPreflightObserved",
        payload={
            "account_id": account_id,
            "lottery_id": lottery_id,
            "probe_kind": API_PREFLIGHT_KIND,
            "observation_hash": preflight.observation_hash,
            "target_identity": preflight.observation[
                "target_identity"
            ],
            "required_actions": list(plan.required_actions),
            "side_effects": False,
        },
        correlation_id=task_id,
    )
    if not event_id:
        raise RuntimeError(
            "bilibili_shadow_observation_event_persistence_failed"
        )
    await runtime.save_phase(
        task_id,
        account_id,
        lottery_id,
        "completed",
    )


async def _get_completed_bilibili_phases_owned(
    task_id: str,
    *,
    account_id: int,
    lottery_id: int,
    dynamic_id: str,
    runtime,
) -> set[str]:
    """Load successful actions only when their full task binding is exact."""

    rows = await runtime.database.fetch_all(
        """SELECT account_id, lottery_id, dynamic_id, action, phase
           FROM bilibili_action_ledger
           WHERE task_id = :task_id AND ok = 1 AND outcome = 'ok'""",
        {"task_id": task_id},
    )
    completed: set[str] = set()
    for row in rows:
        phase = str(runtime.row_get(row, "phase", "") or "").strip().lower()
        if phase not in PHASE_ORDER:
            raise RuntimeError("bilibili_action_ledger_phase_invalid")
        action = str(runtime.row_get(row, "action", "") or "").strip().lower()
        try:
            ledger_account_id = int(runtime.row_get(row, "account_id", 0) or 0)
            ledger_lottery_id = int(runtime.row_get(row, "lottery_id", 0) or 0)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("bilibili_action_ledger_binding_invalid") from exc
        if (
            ledger_account_id != account_id
            or ledger_lottery_id != lottery_id
            or str(runtime.row_get(row, "dynamic_id", "") or "").strip() != dynamic_id
            or API_TO_DPMS_PHASE.get(action) != phase
        ):
            raise RuntimeError("bilibili_action_ledger_binding_invalid")
        completed.add(phase)
    return completed


async def _save_bilibili_action_ledger_owned(
    *,
    task_id: str,
    account_id: int,
    lottery_id: int,
    dynamic_id: str,
    action: str,
    phase: str | None,
    code: int | None,
    outcome: str,
    message: str | None,
    ok: bool,
    runtime,
):
    try:
        await runtime.database.execute(
            """INSERT INTO bilibili_action_ledger
                 (task_id, account_id, lottery_id, dynamic_id, action, phase, code, outcome, message, ok, task_mode, source)
               VALUES
                 (:task_id, :account_id, :lottery_id, :dynamic_id, :action, :phase, :code, :outcome, :message, :ok, 'real_run', 'api_real_run')
               ON DUPLICATE KEY UPDATE
                 dynamic_id = :dynamic_id,
                 phase = :phase,
                 code = :code,
                 outcome = :outcome,
                 message = :message,
                 ok = :ok,
                 updated_at = CURRENT_TIMESTAMP""",
            {
                "task_id": task_id,
                "account_id": account_id,
                "lottery_id": lottery_id,
                "dynamic_id": dynamic_id,
                "action": action,
                "phase": phase,
                "code": code,
                "outcome": outcome,
                "message": message,
                "ok": 1 if ok else 0,
            },
        )
    except Exception as exc:
        runtime.structured_log(
            "warning",
            "bilibili_action_ledger_write_failed",
            task_id=task_id,
            lottery_id=lottery_id,
            action=action,
            error=str(exc),
        )
        raise


async def _persist_bilibili_action_result_owned(
    *,
    task_id: str,
    account_id: int,
    lottery_id: int,
    dynamic_id: str,
    action: str,
    action_result,
    runtime,
) -> None:
    """Durably settle one confirmed API result before another mutation starts."""

    phase = API_TO_DPMS_PHASE.get(action)
    await _save_bilibili_action_ledger_owned(
        task_id=task_id,
        account_id=account_id,
        lottery_id=lottery_id,
        dynamic_id=dynamic_id,
        action=action,
        phase=phase,
        code=action_result.code,
        outcome=action_result.outcome.value,
        message=action_result.message,
        ok=action_result.ok,
        runtime=runtime,
    )
    event_id = await runtime.record_event(
        aggregate="task",
        aggregate_id=task_id,
        event_type="BilibiliApiActionCompleted",
        payload={
            "account_id": account_id,
            "lottery_id": lottery_id,
            "dynamic_id": dynamic_id,
            "action": action,
            "code": action_result.code,
            "outcome": action_result.outcome.value,
            "message": action_result.message,
        },
        correlation_id=task_id,
    )
    if not event_id:
        raise RuntimeError("bilibili_action_event_persistence_failed")
    if phase and action_result.ok:
        await runtime.save_phase(task_id, account_id, lottery_id, phase)


async def _execute_bilibili_api_real_owned(
    task: dict,
    *,
    runtime,
):
    task_id = task.get("task_id")
    account_id = int(task.get("account_id"))
    lottery_id = int(task.get("lottery_id"))
    initial_gate = await runtime.enforce_task_real_run_gate(task, require_running=True)
    full_plan = initial_gate.action_plan
    validated_plan = runtime.gate_execution_action_plan(initial_gate)
    phases = list(validated_plan.required_actions)
    dynamic_id = extract_bilibili_dynamic_id(task.get("raw_url"), task.get("canonical_url"))
    current_phase = await runtime.get_latest_phase(task_id) or "init"
    if current_phase == "completed":
        return
    if current_phase not in PHASE_ORDER and current_phase != "init":
        raise RuntimeError(f"task_phase_invalid:{current_phase}")
    completed_phases = await _get_completed_bilibili_phases_owned(
        task_id,
        account_id=account_id,
        lottery_id=lottery_id,
        dynamic_id=dynamic_id,
        runtime=runtime,
    )
    if current_phase != "init" and not completed_phases:
        # Before the per-action ledger existed, the API executor used a
        # different comment/repost order than task_phases. Inferring completed
        # work from that single marker can either skip a required action or
        # repeat a remote mutation, so legacy in-flight work needs explicit
        # reconciliation instead of automatic resume.
        raise RuntimeError("bilibili_legacy_phase_requires_reconciliation")
    remaining_phases = [phase for phase in phases if phase not in completed_phases]
    if not remaining_phases:
        await runtime.save_phase(task_id, account_id, lottery_id, "completed")
        return

    actions = dpms_phases_to_api_actions(remaining_phases)
    cookie_header = runtime.credential_to_cookie_header(
        await runtime.load_account_credential(account_id)
    )
    if not cookie_header:
        raise RuntimeError(f"Account {account_id} has no usable Bilibili Cookie")

    async with BilibiliApiClient(cookie_header, config=BiliEngineConfig()) as client:
        if not await client.check_login():
            await runtime.set_account_status(account_id, "login_required", "bilibili_cookie_invalid")
            raise RuntimeError("Bilibili account Cookie is invalid or expired")

        detail = await client.get_dynamic_detail(dynamic_id)
        if int(detail.get("code", -1)) != 0:
            raise RuntimeError(f"Bilibili dynamic detail failed: code={detail.get('code')}")
        card = parse_detail_card(detail, dynamic_id)
        if card.type == 1:
            # The reviewed URL identifies the wrapper dynamic, while the old
            # executor silently changed follow/comment/repost to ``origin``.
            # Until Action Plan v2 binds the origin id, author and rule hash,
            # any forwarded wrapper must remain fail-closed before mutation.
            raise BilibiliForwardedTargetRequiresReview(
                "bilibili_forwarded_origin_requires_review"
            )
        validate_card_for_actions(card, actions)
        if "followed" in validated_plan.required_actions:
            if bilibili_author_handle(card.uname) != validated_plan.follow_target_handle:
                raise RuntimeError("bilibili_real_follow_target_mismatch")

        current_intents: dict[str, runtime.StartedActionIntent] = {}

        async def before_action(action: str) -> None:
            snapshot = await runtime.enforce_task_real_run_gate(task, require_running=True)
            if (
                snapshot.action_plan.plan_hash != full_plan.plan_hash
                or runtime.gate_execution_action_plan(snapshot).plan_hash
                != validated_plan.plan_hash
                or runtime.gate_requested_actions(snapshot)
                != validated_plan.required_actions
            ):
                raise runtime.RealRunGateBlocked("action_plan_changed_during_execution")
            await runtime.refresh_task_lease(task_id)
            await runtime.renew_account_operation_lease(
                db=runtime.database,
                task_id=task_id,
                account_id=account_id,
                lottery_id=lottery_id,
                worker_id=runtime.WORKER_ID,
                execution_intent_kind=(
                    snapshot.execution_intent_kind
                ),
            )
            # Re-read every authoritative binding after both lease renewals and
            # immediately before the intent transaction. No external request
            # has started at this point.
            renewed_snapshot = await runtime.enforce_task_real_run_gate(
                task, require_running=True
            )
            if (
                renewed_snapshot.action_plan.plan_hash != full_plan.plan_hash
                or runtime.gate_execution_action_plan(renewed_snapshot).plan_hash
                != validated_plan.plan_hash
                or runtime.gate_requested_actions(renewed_snapshot)
                != validated_plan.required_actions
            ):
                raise runtime.RealRunGateBlocked(
                    "action_plan_changed_during_execution"
                )
            dpms_action = API_TO_DPMS_PHASE.get(action)
            if not dpms_action:
                raise RuntimeError("bilibili_action_intent_action_invalid")
            intent = await runtime.prepare_and_start_action_intent(
                db=runtime.database,
                task_id=task_id,
                account_id=account_id,
                lottery_id=lottery_id,
                worker_id=runtime.WORKER_ID,
                execution_intent_kind=(
                    renewed_snapshot.execution_intent_kind
                ),
                action=action,
                payload=validated_plan.payload_for(dpms_action),
            )
            current_intents[action] = intent

        async def after_attempt(action: str, action_result) -> None:
            intent = current_intents.get(action)
            if intent is None:
                raise BilibiliActionSettlementFailed(
                    action,
                    action_result,
                    RuntimeError("bilibili_action_intent_missing"),
                )
            try:
                await runtime.settle_action_intent(
                    db=runtime.database,
                    intent=intent,
                    succeeded=action_result.ok,
                    outcome=action_result.outcome.value,
                    error_message=None if action_result.ok else action_result.message,
                )
            except BaseException as exc:
                raise BilibiliActionSettlementFailed(action, action_result, exc) from exc
            current_intents.pop(action, None)

        async def on_attempt_error(action: str, exc: BaseException) -> None:
            intent = current_intents.get(action)
            if intent is None:
                return
            try:
                await runtime.await_safety_settlement(
                    runtime.mark_action_intent_unknown(
                        db=runtime.database,
                        intent=intent,
                        reason=f"{type(exc).__name__}: remote outcome not proven",
                    )
                )
            except Exception as intent_exc:
                # The platform breaker/emergency barrier below is still the
                # last-resort global fence when the intent settlement itself is
                # unavailable. Do not replace the original network exception.
                runtime.structured_log(
                    "error",
                    "external_action_intent_unknown_write_failed",
                    task_id=task_id,
                    action=action,
                    exception=intent_exc,
                )

        executed_dynamic_id = card.dynamic_id

        async def after_action(action: str, action_result) -> None:
            try:
                await _persist_bilibili_action_result_owned(
                    task_id=task_id,
                    account_id=account_id,
                    lottery_id=lottery_id,
                    dynamic_id=executed_dynamic_id,
                    action=action,
                    action_result=action_result,
                    runtime=runtime,
                )
            except asyncio.CancelledError as exc:
                # The remote response is already classified, so cancellation
                # here means local settlement is incomplete rather than that no
                # action happened. Convert it to the same durable quarantine
                # path as every other confirmed-result persistence failure.
                raise BilibiliActionSettlementFailed(action, action_result, exc) from exc
            except Exception as exc:
                raise BilibiliActionSettlementFailed(action, action_result, exc) from exc

        try:
            result = await BilibiliApiExecutor(
                client,
                client.config,
                before_action=before_action,
                after_attempt=after_attempt,
                on_attempt_error=on_attempt_error,
                after_action=after_action,
            ).participate(card, actions, validated_plan.action_payloads)
        except (BilibiliApiActionOutcomeUnknown, BilibiliActionSettlementFailed) as exc:
            settlement_failed = isinstance(exc, BilibiliActionSettlementFailed)
            reason = (
                f"bilibili_{exc.action}_settlement_failed"
                if settlement_failed
                else f"bilibili_{exc.action}_outcome_unknown"
            )
            quarantine_errors: list[str] = []
            breaker_opened = False
            breaker_failure: Exception | None = None
            emergency_failure: Exception | None = None
            emergency_barrier: str | None = None
            unknown_ledger_recorded = False
            active_intent = current_intents.get(exc.action)
            if active_intent is not None:
                try:
                    await runtime.await_safety_settlement(
                        runtime.mark_action_intent_unknown(
                            db=runtime.database,
                            intent=active_intent,
                            reason=reason,
                        )
                    )
                except Exception as quarantine_exc:
                    quarantine_errors.append(
                        f"intent:{type(quarantine_exc).__name__}"
                    )
            try:
                await runtime.await_safety_settlement(
                    runtime.open_unknown_outcome_breaker(
                        db=runtime.database,
                        platform="bilibili",
                        action=exc.action,
                    )
                )
                breaker_opened = True
            except Exception as quarantine_exc:
                breaker_failure = quarantine_exc
                quarantine_errors.append(f"breaker:{type(quarantine_exc).__name__}")
                runtime.structured_log(
                    "error",
                    "unknown_outcome_breaker_failed",
                    task_id=task_id,
                    action=exc.action,
                    exception=quarantine_exc,
                )
                try:
                    emergency_barrier = await runtime.await_safety_settlement(
                        runtime.emergency_stop_real_runs_and_revoke_lease(
                            task_id=task_id,
                            platform="bilibili",
                            action=exc.action,
                        )
                    )
                except Exception as emergency_exc:
                    emergency_failure = emergency_exc
                    quarantine_errors.append(f"emergency:{type(emergency_exc).__name__}")
            try:
                known_result = exc.action_result if settlement_failed else None
                await runtime.await_safety_settlement(
                    _save_bilibili_action_ledger_owned(
                        task_id=task_id,
                        account_id=account_id,
                        lottery_id=lottery_id,
                        dynamic_id=executed_dynamic_id,
                        action=exc.action,
                        phase=API_TO_DPMS_PHASE.get(exc.action),
                        code=known_result.code if known_result is not None else None,
                        outcome=known_result.outcome.value if known_result is not None else "unknown",
                        message=(
                            f"{known_result.message}; local settlement failed"
                            if known_result is not None
                            else exc.reason
                        ),
                        ok=known_result.ok if known_result is not None else False,
                        runtime=runtime,
                    )
                )
                unknown_ledger_recorded = True
            except Exception as quarantine_exc:
                quarantine_errors.append(f"ledger:{type(quarantine_exc).__name__}")
            try:
                quarantine_event_id = await runtime.await_safety_settlement(
                    runtime.record_event(
                        aggregate="task",
                        aggregate_id=task_id,
                        event_type=(
                            "BilibiliApiActionSettlementFailed"
                            if settlement_failed
                            else "BilibiliApiActionOutcomeUnknown"
                        ),
                        payload={
                            "account_id": account_id,
                            "lottery_id": lottery_id,
                            "dynamic_id": executed_dynamic_id,
                            "action": exc.action,
                            "reason": exc.reason,
                            "remote_outcome": (
                                exc.action_result.outcome.value if settlement_failed else "unknown"
                            ),
                            "remote_code": exc.action_result.code if settlement_failed else None,
                            "platform_breaker_opened": breaker_opened,
                            "emergency_barrier": emergency_barrier,
                            "unknown_ledger_recorded": unknown_ledger_recorded,
                            "quarantine_errors": quarantine_errors,
                        },
                        correlation_id=task_id,
                    )
                )
                if not quarantine_event_id:
                    quarantine_errors.append("event:persistence_failed")
            except Exception as quarantine_exc:
                quarantine_errors.append(f"event:{type(quarantine_exc).__name__}")
                runtime.structured_log(
                    "error",
                    "unknown_outcome_event_failed",
                    task_id=task_id,
                    action=exc.action,
                    exception=quarantine_exc,
                )
            try:
                status_change = (
                    account_status_for_results({exc.action: exc.action_result})
                    if settlement_failed
                    else None
                )
                if status_change:
                    await runtime.await_safety_settlement(
                        runtime.set_account_status(account_id, status_change[0], status_change[1])
                    )
                else:
                    await runtime.await_safety_settlement(
                        runtime.set_account_status(account_id, "cooling", reason)
                    )
            except runtime.AccountStatusPersistenceFailed:
                # Do not downgrade an uncommitted canonical risk settlement to
                # the generic task-failure cleanup below. Keeping the claim and
                # account in their current state lets Recovery apply the same
                # fail-closed path used by direct risk-settlement failures.
                raise
            except Exception as quarantine_exc:
                quarantine_errors.append(f"account:{type(quarantine_exc).__name__}")
                runtime.structured_log(
                    "error",
                    "unknown_outcome_account_quarantine_failed",
                    task_id=task_id,
                    account_id=account_id,
                    action=exc.action,
                    exception=quarantine_exc,
                )
            if not breaker_opened:
                raise runtime.TaskSettlementUnconfirmed(
                    task_id,
                    emergency_failure or breaker_failure or exc,
                ) from exc
            raise

        status_change = account_status_for_results(result.actions)
        if status_change:
            status, reason = status_change
            await runtime.set_account_status(account_id, status, reason)

        try:
            completion_event_id = await runtime.record_event(
                aggregate="task",
                aggregate_id=task_id,
                event_type="BilibiliApiRealRunExecuted",
                payload={
                    "account_id": account_id,
                    "lottery_id": lottery_id,
                    "requested_dynamic_id": dynamic_id,
                    "executed_dynamic_id": result.dynamic_id,
                    "actions": list(result.actions.keys()),
                    "success": result.success,
                    "aborted": result.aborted,
                    "abort_reason": result.abort_reason,
                },
                correlation_id=task_id,
            )
            if not completion_event_id:
                raise RuntimeError("bilibili_completion_event_persistence_failed")
        except asyncio.CancelledError as exc:
            raise runtime.TaskSettlementUnconfirmed(task_id, exc) from exc
        except Exception as exc:
            raise runtime.TaskSettlementUnconfirmed(task_id, exc) from exc

        if not result.success:
            details = "; ".join(
                f"{name}={res.outcome.value}(code={res.code})" for name, res in result.actions.items()
            )
            raise RuntimeError(result.abort_reason or f"bilibili_api_real_run_failed: {details}")
        try:
            await runtime.save_phase(task_id, account_id, lottery_id, "completed")
        except asyncio.CancelledError as exc:
            raise runtime.TaskSettlementUnconfirmed(task_id, exc) from exc
        except runtime.TaskSettlementUnconfirmed:
            raise
        except Exception as exc:
            raise runtime.TaskSettlementUnconfirmed(task_id, exc) from exc


async def execute_bilibili_api_real(
    task: dict,
    adapter,
    pool,
    *,
    runtime,
):
    """Invoke Bilibili's reviewed API mutation strategy."""

    return await _execute_bilibili_api_real_owned(
        task,
        runtime=runtime,
    )


async def validate_bilibili_api_shadow_claim(
    *,
    runtime,
    task_message: dict | None,
    task_row,
    lottery,
    account,
) -> None:
    """Bind one Bilibili API observation to its immutable authority rows."""

    if not isinstance(task_message, dict):
        raise runtime.TaskClaimConflict("shadow_task_message_missing")
    try:
        authoritative_plan = runtime.validate_action_plan_v2(
            runtime.row_get(lottery, "action_plan"),
            reject_media=True,
        )
        message_plan = runtime.validate_action_plan_v2(
            task_message.get("action_plan"),
            reject_media=True,
        )
    except runtime.ActionPlanV2Error as exc:
        raise runtime.TaskClaimConflict(
            f"shadow_task_{exc.code}"
        ) from exc
    if (
        authoritative_plan.execution_path_id
        != BILIBILI_API_EXECUTION_PATH
        or message_plan.execution_path_id
        != BILIBILI_API_EXECUTION_PATH
        or runtime.canonical_json_bytes(authoritative_plan.plan)
        != runtime.canonical_json_bytes(message_plan.plan)
    ):
        raise runtime.TaskClaimConflict(
            "shadow_task_action_plan_mismatch"
        )

    canonical_url = str(
        runtime.row_get(lottery, "canonical_url") or ""
    ).strip()
    target_hash = runtime.compute_target_hash(canonical_url)
    execution_revision = runtime._claim_positive_int(
        runtime.row_get(account, "execution_revision"),
        "shadow_task_execution_revision_invalid",
    )
    if (
        runtime._claim_positive_int(
            task_message.get("execution_revision"),
            "shadow_task_execution_revision_invalid",
        )
        != execution_revision
    ):
        raise runtime.TaskClaimConflict(
            "shadow_task_execution_revision_mismatch"
        )
    config_hash = compute_bilibili_api_config_hash(
        execution_revision
    )
    snapshot_id = runtime._claim_positive_int(
        runtime.row_get(
            lottery,
            "authoritative_rule_snapshot_id",
        ),
        "shadow_task_rule_snapshot_binding_invalid",
    )
    task_snapshot_id = runtime._claim_positive_int(
        runtime.row_get(task_row, "rule_snapshot_id"),
        "shadow_task_rule_snapshot_binding_invalid",
    )
    message_snapshot_id = runtime._claim_positive_int(
        task_message.get("rule_snapshot_id"),
        "shadow_task_rule_snapshot_binding_invalid",
    )
    exact_strings = (
        (
            "platform",
            str(task_message.get("platform") or "").strip().lower(),
            "bilibili",
        ),
        (
            "raw_url",
            str(task_message.get("raw_url") or "").strip(),
            str(runtime.row_get(lottery, "raw_url") or "").strip(),
        ),
        (
            "canonical_url",
            str(task_message.get("canonical_url") or "").strip(),
            canonical_url,
        ),
        (
            "rule_hash",
            str(task_message.get("rule_hash") or "").strip(),
            authoritative_plan.rule_hash,
        ),
        (
            "action_plan_hash",
            str(
                task_message.get("action_plan_hash") or ""
            ).strip(),
            authoritative_plan.plan_hash,
        ),
        (
            "execution_path_id",
            str(
                task_message.get("execution_path_id") or ""
            ).strip(),
            BILIBILI_API_EXECUTION_PATH,
        ),
        (
            "target_hash",
            str(task_message.get("target_hash") or "").strip(),
            target_hash,
        ),
        (
            "config_hash",
            str(task_message.get("config_hash") or "").strip(),
            config_hash,
        ),
    )
    for field, message_value, expected in exact_strings:
        if not message_value or message_value != expected:
            raise runtime.TaskClaimConflict(
                f"shadow_task_{field}_mismatch"
            )
    if (
        snapshot_id != task_snapshot_id
        or task_snapshot_id != message_snapshot_id
        or message_snapshot_id
        != authoritative_plan.rule_snapshot_id
        or str(runtime.row_get(task_row, "rule_hash") or "").strip()
        != authoritative_plan.rule_hash
        or str(runtime.row_get(lottery, "rule_hash") or "").strip()
        != authoritative_plan.rule_hash
        or str(
            runtime.row_get(task_row, "action_plan_hash") or ""
        ).strip()
        != authoritative_plan.plan_hash
        or str(
            runtime.row_get(lottery, "action_plan_hash") or ""
        ).strip()
        != authoritative_plan.plan_hash
        or str(
            runtime.row_get(task_row, "execution_path_id") or ""
        ).strip()
        != BILIBILI_API_EXECUTION_PATH
        or str(runtime.row_get(task_row, "target_hash") or "").strip()
        != target_hash
        or str(runtime.row_get(task_row, "config_hash") or "").strip()
        != config_hash
        or str(runtime.row_get(account, "platform") or "")
        .strip()
        .lower()
        != "bilibili"
    ):
        raise runtime.TaskClaimConflict(
            "shadow_task_api_binding_mismatch"
        )

    lease_id = str(
        runtime.row_get(task_row, "account_lease_id") or ""
    ).strip()
    lease_generation = runtime._claim_positive_int(
        runtime.row_get(
            task_row,
            "account_lease_generation",
        ),
        "shadow_task_account_lease_binding_invalid",
    )
    if (
        str(task_message.get("account_lease_id") or "").strip()
        != lease_id
        or runtime._claim_positive_int(
            task_message.get("account_lease_generation"),
            "shadow_task_account_lease_binding_invalid",
        )
        != lease_generation
    ):
        raise runtime.TaskClaimConflict(
            "shadow_task_account_lease_binding_invalid"
        )
    account_id = runtime._claim_positive_int(
        runtime.row_get(task_row, "account_id"),
        "shadow_task_account_binding_invalid",
    )
    lease = await runtime.database.fetch_one(
        """SELECT lease_id, account_id, generation, operation_kind, owner_id, task_id,
                  CASE WHEN expires_at > NOW() THEN 1 ELSE 0 END AS lease_active,
                  CASE WHEN released_at IS NULL THEN 1 ELSE 0 END AS lease_unreleased,
                  CASE WHEN generation = (
                    SELECT MAX(newest.generation) FROM account_operation_leases newest
                    WHERE newest.account_id = :account_id
                  ) THEN 1 ELSE 0 END AS lease_latest_generation,
                  (SELECT COUNT(*) FROM account_operation_leases live
                   WHERE live.account_id = :account_id AND live.released_at IS NULL
                     AND live.expires_at > NOW()) AS active_account_lease_count
           FROM account_operation_leases
           WHERE lease_id = :lease_id AND account_id = :account_id
             AND generation = :generation
           FOR UPDATE""",
        {
            "lease_id": lease_id,
            "account_id": account_id,
            "generation": lease_generation,
        },
    )
    task_id = str(
        runtime.row_get(task_row, "task_id") or ""
    ).strip()
    if (
        not lease
        or str(runtime.row_get(lease, "lease_id") or "").strip()
        != lease_id
        or runtime._claim_positive_int(
            runtime.row_get(lease, "account_id"),
            "shadow_task_account_lease_binding_invalid",
        )
        != account_id
        or runtime._claim_positive_int(
            runtime.row_get(lease, "generation"),
            "shadow_task_account_lease_binding_invalid",
        )
        != lease_generation
        or str(runtime.row_get(lease, "operation_kind") or "")
        .strip()
        .lower()
        != "shadow_run"
        or str(runtime.row_get(lease, "owner_id") or "").strip()
        != task_id
        or str(runtime.row_get(lease, "task_id") or "").strip()
        != task_id
        or int(runtime.row_get(lease, "lease_active", 0) or 0) != 1
        or int(runtime.row_get(lease, "lease_unreleased", 0) or 0)
        != 1
        or int(
            runtime.row_get(lease, "lease_latest_generation", 0)
            or 0
        )
        != 1
        or int(
            runtime.row_get(lease, "active_account_lease_count", 0)
            or 0
        )
        != 1
        or int(
            runtime.row_get(task_row, "reconciliation_required", 0)
            or 0
        )
        != 0
    ):
        raise runtime.TaskClaimConflict(
            "shadow_task_account_lease_binding_invalid"
        )


def validate_bilibili_probe_authority(
    authoritative_message: Mapping[str, Any],
) -> bool:
    """Bind a durable Bilibili probe envelope to its API config revision."""

    try:
        execution_revision = int(
            authoritative_message["execution_revision"]
        )
    except (KeyError, TypeError, ValueError):
        return False
    return (
        str(authoritative_message.get("config_hash") or "")
        == compute_bilibili_api_config_hash(execution_revision)
    )


async def validate_bilibili_probe_claim(
    *,
    runtime,
    probe: dict,
    row,
    authoritative: dict,
) -> None:
    """Validate Bilibili's immutable API probe binding inside its module."""

    try:
        plan = validate_action_plan_v2(
            row["lottery_action_plan"],
            reject_media=True,
        )
    except ActionPlanV2Error as exc:
        raise ValueError(f"adapter_probe_{exc.code}") from exc
    try:
        message_snapshot_id = int(probe.get("rule_snapshot_id"))
        execution_revision = int(probe.get("execution_revision"))
    except (TypeError, ValueError) as exc:
        raise ValueError("adapter_probe_api_binding_invalid") from exc
    expected_config_hash = compute_bilibili_api_config_hash(
        int(row["execution_revision"] or 0)
    )
    expected_target_hash = compute_target_hash(
        authoritative["canonical_url"]
    )
    message_bindings = {
        "execution_path_id": str(
            probe.get("execution_path_id") or ""
        ).strip(),
        "target_hash": str(probe.get("target_hash") or "").strip(),
        "rule_hash": str(probe.get("rule_hash") or "").strip(),
        "action_plan_hash": str(
            probe.get("action_plan_hash") or ""
        ).strip(),
        "config_hash": str(probe.get("config_hash") or "").strip(),
    }
    if (
        plan.execution_path_id != BILIBILI_API_EXECUTION_PATH
        or authoritative["execution_path_id"]
        != BILIBILI_API_EXECUTION_PATH
        or message_bindings["execution_path_id"]
        != BILIBILI_API_EXECUTION_PATH
        or authoritative["target_hash"] != expected_target_hash
        or message_bindings["target_hash"] != expected_target_hash
        or authoritative["rule_snapshot_id"] != plan.rule_snapshot_id
        or message_snapshot_id != plan.rule_snapshot_id
        or int(row["authoritative_rule_snapshot_id"] or 0)
        != plan.rule_snapshot_id
        or authoritative["rule_hash"] != plan.rule_hash
        or message_bindings["rule_hash"] != plan.rule_hash
        or str(row["lottery_rule_hash"] or "").strip()
        != plan.rule_hash
        or str(row["snapshot_rule_hash"] or "").strip()
        != plan.rule_hash
        or authoritative["action_plan_hash"] != plan.plan_hash
        or message_bindings["action_plan_hash"] != plan.plan_hash
        or str(row["lottery_action_plan_hash"] or "").strip()
        != plan.plan_hash
        or authoritative["config_hash"] != expected_config_hash
        or message_bindings["config_hash"] != expected_config_hash
        or execution_revision != int(row["execution_revision"] or 0)
        or int(row["snapshot_complete"] or 0) != 1
        or not str(row["snapshot_attested_by"] or "").strip()
        or row["snapshot_attested_at"] is None
    ):
        raise ValueError("adapter_probe_api_binding_mismatch")
    lottery_rule_text = row["lottery_rule_text"]
    snapshot_rule_text = row["snapshot_rule_text"]
    if isinstance(lottery_rule_text, bytes):
        lottery_rule_text = lottery_rule_text.decode(
            "utf-8",
            errors="strict",
        )
    if isinstance(snapshot_rule_text, bytes):
        snapshot_rule_text = snapshot_rule_text.decode(
            "utf-8",
            errors="strict",
        )
    if (
        not isinstance(lottery_rule_text, str)
        or lottery_rule_text != snapshot_rule_text
        or compute_rule_hash(lottery_rule_text) != plan.rule_hash
    ):
        raise ValueError("adapter_probe_rule_snapshot_mismatch")
    message_plan = probe.get("action_plan")
    if message_plan is not None:
        try:
            parsed_message_plan = validate_action_plan_v2(
                message_plan,
                reject_media=True,
            )
        except ActionPlanV2Error as exc:
            raise ValueError(
                f"adapter_probe_message_{exc.code}"
            ) from exc
        if canonical_json_bytes(
            parsed_message_plan.plan
        ) != canonical_json_bytes(plan.plan):
            raise ValueError("adapter_probe_action_plan_mismatch")
    authoritative["action_plan"] = plan.plan
    authoritative["execution_revision"] = execution_revision


async def materialize_bilibili_terminal_probe(
    *,
    probe_id: str,
    runtime,
) -> None:
    """Idempotently pair a succeeded Bilibili probe with exact evidence."""

    await materialize_for_probe(
        db=runtime.database,
        probe_id=probe_id,
    )


async def execute_bilibili_api_probe(
    binding: dict,
    pool,
    *,
    runtime,
):
    """Execute the Bilibili-owned immutable GET-only probe contract."""

    if (
        binding.get("platform") != "bilibili"
        or binding.get("execution_path_id")
        != BILIBILI_API_EXECUTION_PATH
    ):
        raise RuntimeError("bilibili_api_probe_binding_invalid")
    credential = await runtime.load_probe_credential(
        binding["account_id"]
    )
    cookie_header = runtime.credential_to_cookie_header(credential)
    if not cookie_header:
        raise RuntimeError("bilibili_api_preflight_cookie_missing")
    plan = validate_action_plan_v2(
        binding["action_plan"],
        reject_media=True,
    )
    dynamic_id = extract_bilibili_dynamic_id(
        binding["target_url"],
        binding["canonical_url"],
    )
    preflight = await run_readonly_api_preflight(
        cookie_header=cookie_header,
        dynamic_id=dynamic_id,
        required_actions=plan.required_actions,
        execution_revision=binding["execution_revision"],
        config_hash=binding["config_hash"],
        expected_follow_handle=(
            plan.follow_target_handle
            if "followed" in plan.required_actions
            else None
        ),
    )
    return runtime.ProbeObservation(
        result=preflight.observation,
        observation_kind=API_PREFLIGHT_KIND,
        observation_hash=preflight.observation_hash,
        success_event_type="AdapterApiProbeSucceeded",
        success_event_payload={
            "probe_kind": API_PREFLIGHT_KIND,
            "observation_hash": preflight.observation_hash,
            "target_identity": preflight.observation[
                "target_identity"
            ],
            "required_actions": list(plan.required_actions),
            "side_effects": False,
        },
        materialize_execution_evidence=True,
    )


def validate_bilibili_real_run_evidence(
    *,
    task,
    row,
    account_id: int,
    lottery_id: int,
    platform: str,
    plan,
    execution_plan=None,
) -> RealRunEvidenceBinding:
    """Own the Bilibili-specific evidence branch of the shared gate."""

    if plan.execution_path_id != BILIBILI_API_EXECUTION_PATH:
        raise RealRunGateBlocked("execution_path_not_supported")
    evidence_id = _validate_bilibili_execution_evidence(
        task,
        row,
        account_id=account_id,
        lottery_id=lottery_id,
        platform=platform,
        plan=plan,
    )
    execution_revision = required_int(
        row_value(row, "account_execution_revision"),
        code="account_execution_revision_invalid",
    )
    return RealRunEvidenceBinding(
        evidence_id=evidence_id,
        execution_revision=execution_revision,
    )


def _validate_bilibili_execution_evidence(
    task: Mapping[str, Any],
    row: Any,
    *,
    account_id: int,
    lottery_id: int,
    platform: str,
    plan: ValidatedActionPlanV2,
) -> str:
    """Validate Bilibili's exact probe/shadow evidence binding."""

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
        execution_revision = int(
            row_value(row, "account_execution_revision")
        )
    except (TypeError, ValueError) as exc:
        raise RealRunGateBlocked("execution_evidence_binding_invalid") from exc
    if execution_revision <= 0:
        raise RealRunGateBlocked("account_execution_revision_invalid")
    expected_target_hash = compute_target_hash(
        str(row_value(row, "lottery_canonical_url") or "").strip()
    )
    expected_config_hash = compute_bilibili_api_config_hash(
        execution_revision
    )
    message_target_hash = str(task.get("target_hash") or "").strip()
    message_config_hash = str(task.get("config_hash") or "").strip()
    if (
        not evidence_id
        or message_evidence_id != evidence_id
        or task_evidence_id != evidence_id
        or evidence_account_id != account_id
        or evidence_lottery_id != lottery_id
        or evidence_snapshot_id != plan.rule_snapshot_id
        or str(row_value(row, "evidence_platform") or "").strip().lower()
        != platform
        or str(
            row_value(row, "evidence_execution_path_id") or ""
        ).strip()
        != plan.execution_path_id
        or str(row_value(row, "evidence_rule_hash") or "").strip()
        != plan.rule_hash
        or str(
            row_value(row, "evidence_action_plan_hash") or ""
        ).strip()
        != plan.plan_hash
        or str(row_value(row, "evidence_target_hash") or "").strip()
        != expected_target_hash
        or message_target_hash != expected_target_hash
        or str(row_value(row, "task_target_hash") or "").strip()
        != expected_target_hash
        or str(row_value(row, "evidence_config_hash") or "").strip()
        != expected_config_hash
        or message_config_hash != expected_config_hash
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
    # A single aggregate row is authoritative only when it links two immutable
    # GET-only observations and both source leases covered their observation
    # window before being released.
    if (
        not str(row_value(row, "evidence_probe_id") or "").strip()
        or not str(
            row_value(row, "evidence_shadow_task_id") or ""
        ).strip()
        or str(
            row_value(row, "evidence_probe_status") or ""
        ).strip().lower()
        != "succeeded"
        or row_value(row, "evidence_probe_finished_at") is None
        or str(
            row_value(row, "evidence_shadow_status") or ""
        ).strip().lower()
        != "succeeded"
        or str(
            row_value(row, "evidence_shadow_task_mode") or ""
        ).strip().lower()
        != "shadow_run"
        or str(
            row_value(row, "evidence_shadow_target_hash") or ""
        ).strip()
        != expected_target_hash
        or str(
            row_value(row, "evidence_shadow_config_hash") or ""
        ).strip()
        != expected_config_hash
        or int(row_value(row, "evidence_probe_fresh", 0) or 0) != 1
        or int(row_value(row, "evidence_shadow_fresh", 0) or 0) != 1
        or int(
            row_value(row, "evidence_probe_lease_released", 0) or 0
        )
        != 1
        or int(
            row_value(row, "evidence_shadow_lease_released", 0) or 0
        )
        != 1
        or int(
            row_value(
                row,
                "evidence_probe_lease_covers_observation",
                0,
            )
            or 0
        )
        != 1
        or int(
            row_value(
                row,
                "evidence_shadow_lease_covers_observation",
                0,
            )
            or 0
        )
        != 1
    ):
        raise RealRunGateBlocked("probe_shadow_evidence_incomplete")
    expected_follow_handle = (
        plan.follow_target_handle
        if "followed" in plan.required_actions
        else None
    )
    try:
        dynamic_id = extract_bilibili_dynamic_id(
            str(row_value(row, "lottery_raw_url") or "").strip(),
            str(row_value(row, "lottery_canonical_url") or "").strip(),
        )
        probe_observation = validate_preflight_observation(
            json_object(
                row_value(row, "evidence_probe_observation"),
                code="probe_observation_invalid",
            ),
            expected_dynamic_id=dynamic_id,
            expected_actions=plan.required_actions,
            expected_execution_revision=execution_revision,
            expected_config_hash=expected_config_hash,
            expected_follow_handle=expected_follow_handle,
        )
        shadow_observation = validate_preflight_observation(
            json_object(
                row_value(row, "evidence_shadow_observation"),
                code="shadow_observation_invalid",
            ),
            expected_dynamic_id=dynamic_id,
            expected_actions=plan.required_actions,
            expected_execution_revision=execution_revision,
            expected_config_hash=expected_config_hash,
            expected_follow_handle=expected_follow_handle,
        )
    except RealRunGateBlocked:
        raise
    except (TypeError, ValueError) as exc:
        raise RealRunGateBlocked(
            "probe_shadow_observation_invalid"
        ) from exc
    if (
        str(
            row_value(row, "evidence_probe_observation_kind") or ""
        ).strip()
        != API_PREFLIGHT_KIND
        or str(
            row_value(row, "source_probe_observation_kind") or ""
        ).strip()
        != API_PREFLIGHT_KIND
        or str(
            row_value(row, "evidence_shadow_observation_kind") or ""
        ).strip()
        != API_PREFLIGHT_KIND
        or str(
            row_value(row, "source_shadow_observation_kind") or ""
        ).strip()
        != API_PREFLIGHT_KIND
        or probe_observation.observation_hash
        != str(
            row_value(row, "evidence_probe_observation_hash") or ""
        ).strip()
        or probe_observation.observation_hash
        != str(
            row_value(row, "source_probe_observation_hash") or ""
        ).strip()
        or shadow_observation.observation_hash
        != str(
            row_value(row, "evidence_shadow_observation_hash") or ""
        ).strip()
        or shadow_observation.observation_hash
        != str(
            row_value(row, "source_shadow_observation_hash") or ""
        ).strip()
    ):
        raise RealRunGateBlocked(
            "probe_shadow_observation_integrity_invalid"
        )
    return evidence_id


BILIBILI = PlatformModule(
    platform_id="bilibili",
    adapter_factory=lambda selectors=None: BilibiliAdapter(selector_config=selectors),
    probe_handler=execute_bilibili_api_probe,
    probe_authority_validator=validate_bilibili_probe_authority,
    probe_claim_validator=validate_bilibili_probe_claim,
    probe_terminal_materializer=materialize_bilibili_terminal_probe,
    action_order=tuple(BILIBILI_ACTION_ORDER),
    # Video/article targets are valid discovery records, but the existing API
    # executor can prove and mutate only a dynamic/opus target.
    real_target_kinds=frozenset({"dynamic"}),
    execution_paths=(
        ExecutionPath(
            path_id=BILIBILI_API_EXECUTION_PATH,
            # Core/Frontend expose every non-Weibo-OAuth account through the
            # shared browser_session credential contract. The API client still
            # consumes the cookie payload stored inside that account.
            credential_kind="browser_session",
            supported_modes=frozenset({"dry_run", "shadow_run", "real_run"}),
            shadow_executor="bilibili_api",
            real_executor="bilibili_api",
            shadow_handler=execute_bilibili_api_shadow,
            real_handler=execute_bilibili_api_real,
            shadow_claim_validator=validate_bilibili_api_shadow_claim,
            confirmed_intent_settlement=True,
            execution_evidence_kind="exact_execution_evidence",
        ),
    ),
    default_execution_path=BILIBILI_API_EXECUTION_PATH,
    invalid_execution_path_error=(
        BILIBILI_ACTION_PLAN_CONTRACT.execution_path_error
    ),
    intent_action_mapper=dpms_phases_to_api_actions,
    real_run_evidence_context_loader=(
        load_bilibili_real_run_evidence_context
    ),
    real_run_evidence_validator=validate_bilibili_real_run_evidence,
)
