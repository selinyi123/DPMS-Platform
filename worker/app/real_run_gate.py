"""Fail-closed worker-side validation for queued real-run tasks.

Dispatch-time approval is necessary but not sufficient: a task may wait in the
stream while the global switch, a circuit breaker, or its policy decision
changes. The worker calls :func:`enforce_real_run_gate` immediately before it
starts a real task and again before every external mutation.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from app.action_plan import (
    ActionPlanV2Error,
    BILIBILI_API_EXECUTION_PATH,
    ValidatedActionPlanV2,
    canonical_json_bytes,
    compute_bilibili_api_config_hash,
    compute_rule_hash,
    compute_target_hash,
    validate_action_plan_v2,
)
from app.bilibili.preflight import API_PREFLIGHT_KIND, validate_preflight_observation
from app.bilibili.runtime import extract_bilibili_dynamic_id


REAL_RUN_POLICY_KEY = "real_run_gate"
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_EXPLICIT_FALSE = frozenset({"0", "false", "no", "off"})
_SELECTOR_PATHS_AWAITING_BOUND_CONFIG_EVIDENCE = frozenset(
    {"weibo", "douyin"}
)
_BILIBILI_API_EXECUTION_PATH = BILIBILI_API_EXECUTION_PATH
_SUPPORTED_REAL_RUN_PLATFORMS = frozenset({"bilibili"}) | (
    _SELECTOR_PATHS_AWAITING_BOUND_CONFIG_EVIDENCE
) | frozenset({"xiaohongshu"})


class GateDatabase(Protocol):
    async def fetch_one(self, query: str, values: Mapping[str, Any] | None = None): ...

    async def fetch_all(self, query: str, values: Mapping[str, Any] | None = None): ...

    async def execute(self, query: str, values: Mapping[str, Any] | None = None): ...


class RealRunGateBlocked(RuntimeError):
    """Raised when a worker cannot prove that a real action is still allowed."""

    def __init__(self, code: str, detail: str | None = None) -> None:
        self.code = code
        self.detail = detail
        message = f"real_run_gate_blocked:{code}"
        if detail:
            message = f"{message}:{detail}"
        super().__init__(message)


@dataclass(frozen=True)
class RealRunGateSnapshot:
    task_id: str
    account_id: int
    lottery_id: int
    platform: str
    decision_id: str
    policy_version: int
    stage: str
    action_plan: ValidatedActionPlanV2
    execution_evidence_id: str
    account_lease_id: str
    account_lease_generation: int


_TASK_DECISION_QUERY = """
SELECT
  tr.task_id,
  tr.account_id,
  tr.lottery_id,
  tr.status AS task_status,
  tr.task_mode,
  tr.worker_id AS task_worker_id,
  tr.decision_id,
  tr.policy_version AS task_policy_version,
  tr.rule_snapshot_id AS task_rule_snapshot_id,
  tr.rule_hash AS task_rule_hash,
  tr.action_plan_hash AS task_action_plan_hash,
  tr.target_hash AS task_target_hash,
  tr.config_hash AS task_config_hash,
  tr.execution_evidence_id AS task_execution_evidence_id,
  tr.execution_path_id AS task_execution_path_id,
  tr.account_lease_id AS task_account_lease_id,
  tr.account_lease_generation AS task_account_lease_generation,
  tr.reconciliation_required AS task_reconciliation_required,
  a.id AS bound_account_id,
  a.platform AS account_platform,
  a.status AS account_status,
  a.execution_revision AS account_execution_revision,
  l.id AS bound_lottery_id,
  l.platform AS lottery_platform,
  l.status AS lottery_status,
  l.execution_lock AS lottery_execution_lock,
  l.raw_url AS lottery_raw_url,
  l.canonical_url AS lottery_canonical_url,
  l.action_plan AS lottery_action_plan,
  l.authoritative_rule_snapshot_id AS lottery_rule_snapshot_id,
  l.rule_hash AS lottery_rule_hash,
  l.action_plan_hash AS lottery_action_plan_hash,
  l.rule_text AS lottery_rule_text,
  rs.platform AS rule_snapshot_platform,
  rs.rule_text AS snapshot_rule_text,
  rs.is_complete AS rule_snapshot_complete,
  rs.attested_by AS rule_snapshot_attested_by,
  rs.attested_at AS rule_snapshot_attested_at,
  rs.rule_hash AS snapshot_rule_hash,
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
  shadow.config_hash AS evidence_shadow_config_hash,
  aol.account_id AS lease_account_id,
  aol.lease_id,
  aol.generation AS lease_generation,
  aol.operation_kind AS lease_operation_kind,
  aol.owner_id AS lease_owner_id,
  aol.task_id AS lease_task_id,
  CASE WHEN aol.expires_at > NOW() THEN 1 ELSE 0 END AS lease_active,
  CASE WHEN aol.released_at IS NULL THEN 1 ELSE 0 END AS lease_unreleased,
  CASE WHEN aol.generation = (
    SELECT MAX(newest.generation)
    FROM account_operation_leases newest
    WHERE newest.account_id = tr.account_id
  ) THEN 1 ELSE 0 END AS lease_latest_generation,
  (
    SELECT COUNT(*)
    FROM account_operation_leases live
    WHERE live.account_id = tr.account_id
      AND live.released_at IS NULL
      AND live.expires_at > NOW()
  ) AS active_account_lease_count,
  pd.decision_id AS policy_decision_id,
  pd.policy_key AS decision_policy_key,
  pd.policy_version AS decision_policy_version,
  pd.subject_type AS decision_subject_type,
  pd.subject_id AS decision_subject_id,
  pd.outcome AS decision_outcome,
  pv.active AS policy_active
FROM task_runs tr
LEFT JOIN accounts a ON a.id = tr.account_id
LEFT JOIN lotteries l ON l.id = tr.lottery_id
LEFT JOIN lottery_rule_snapshots rs
  ON rs.id = l.authoritative_rule_snapshot_id AND rs.lottery_id = l.id
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
LEFT JOIN account_operation_leases aol
  ON aol.account_id = tr.account_id AND aol.lease_id = tr.account_lease_id
LEFT JOIN policy_decisions pd ON pd.decision_id = tr.decision_id
LEFT JOIN policy_versions pv
  ON pv.policy_key = pd.policy_key AND pv.version = pd.policy_version
WHERE tr.task_id = :task_id
"""


def _row_get(row, key: str, default=None):
    if row is None:
        return default
    if hasattr(row, "get"):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, TypeError):
        return default


def _parse_bool(value: Any) -> bool:
    return str(value or "").strip().lower() in _TRUTHY


def _process_real_run_enabled() -> bool:
    """Require an explicit process-local opt-in for every consume check.

    The database switch is shared durable state and can remain enabled across
    container restarts.  It must not override a newly deployed worker whose
    own ``REAL_RUN_ENABLED`` environment value is missing or disabled.
    """

    return _parse_bool(os.environ.get("REAL_RUN_ENABLED"))


def _review_required(value: Any) -> bool:
    """Treat malformed review flags as requiring review (fail closed)."""

    if isinstance(value, bool):
        return value
    if value is None:
        return True
    if isinstance(value, (int, float)):
        return value != 0
    normalized = str(value).strip().lower()
    if normalized in _EXPLICIT_FALSE:
        return False
    if normalized in _TRUTHY:
        return True
    return True


def _json_object(value: Any, *, code: str) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="strict")
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RealRunGateBlocked(code) from exc
        if isinstance(parsed, dict):
            return parsed
    raise RealRunGateBlocked(code)


def _exact_utf8_text(value: Any, *, code: str) -> str:
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise RealRunGateBlocked(code) from exc
    if not isinstance(value, str) or not value:
        raise RealRunGateBlocked(code)
    return value


async def _validate_target_and_action_plan(
    task: Mapping[str, Any],
    row: Any,
) -> ValidatedActionPlanV2:
    message_raw_url = str(task.get("raw_url") or "").strip()
    message_canonical_url = str(task.get("canonical_url") or "").strip()
    lottery_raw_url = str(_row_get(row, "lottery_raw_url") or "").strip()
    lottery_canonical_url = str(_row_get(row, "lottery_canonical_url") or "").strip()
    if (
        not message_raw_url
        or not message_canonical_url
        or message_raw_url != lottery_raw_url
        or message_canonical_url != lottery_canonical_url
    ):
        raise RealRunGateBlocked("task_target_mismatch")

    try:
        current_plan = validate_action_plan_v2(
            _row_get(row, "lottery_action_plan"), reject_media=True
        )
    except ActionPlanV2Error as exc:
        raise RealRunGateBlocked(f"lottery_{exc.code}") from exc
    try:
        message_plan = validate_action_plan_v2(task.get("action_plan"), reject_media=True)
    except ActionPlanV2Error as exc:
        raise RealRunGateBlocked(f"task_{exc.code}") from exc
    if (
        current_plan.plan_hash != message_plan.plan_hash
        or canonical_json_bytes(current_plan.plan) != canonical_json_bytes(message_plan.plan)
    ):
        raise RealRunGateBlocked("action_plan_mismatch")

    try:
        message_snapshot_id = int(task.get("rule_snapshot_id"))
        task_snapshot_id = int(_row_get(row, "task_rule_snapshot_id"))
        lottery_snapshot_id = int(_row_get(row, "lottery_rule_snapshot_id"))
    except (TypeError, ValueError) as exc:
        raise RealRunGateBlocked("rule_snapshot_binding_invalid") from exc
    message_rule_hash = str(task.get("rule_hash") or "").strip()
    message_plan_hash = str(task.get("action_plan_hash") or "").strip()
    message_execution_path = str(task.get("execution_path_id") or "").strip()
    if (
        message_snapshot_id != message_plan.rule_snapshot_id
        or task_snapshot_id != message_plan.rule_snapshot_id
        or lottery_snapshot_id != message_plan.rule_snapshot_id
        or message_rule_hash != message_plan.rule_hash
        or str(_row_get(row, "task_rule_hash") or "").strip() != message_plan.rule_hash
        or str(_row_get(row, "lottery_rule_hash") or "").strip() != message_plan.rule_hash
        or str(_row_get(row, "snapshot_rule_hash") or "").strip() != message_plan.rule_hash
        or message_plan_hash != message_plan.plan_hash
        or str(_row_get(row, "task_action_plan_hash") or "").strip()
        != message_plan.plan_hash
        or str(_row_get(row, "lottery_action_plan_hash") or "").strip()
        != message_plan.plan_hash
        or message_execution_path != message_plan.execution_path_id
        or str(_row_get(row, "task_execution_path_id") or "").strip()
        != message_plan.execution_path_id
    ):
        raise RealRunGateBlocked("action_plan_binding_mismatch")
    if str(message_plan.plan.get("platform") or "").strip().lower() != str(
        task.get("platform") or ""
    ).strip().lower():
        raise RealRunGateBlocked("action_plan_platform_mismatch")
    lottery_rule_text = _exact_utf8_text(
        _row_get(row, "lottery_rule_text"), code="lottery_rule_text_invalid"
    )
    snapshot_rule_text = _exact_utf8_text(
        _row_get(row, "snapshot_rule_text"), code="snapshot_rule_text_invalid"
    )
    if (
        lottery_rule_text != snapshot_rule_text
        or compute_rule_hash(lottery_rule_text) != message_plan.rule_hash
        or str(_row_get(row, "rule_snapshot_platform") or "").strip().lower()
        != str(task.get("platform") or "").strip().lower()
    ):
        raise RealRunGateBlocked("rule_snapshot_binding_invalid")
    if (
        int(_row_get(row, "rule_snapshot_complete", 0) or 0) != 1
        or not str(_row_get(row, "rule_snapshot_attested_by") or "").strip()
        or _row_get(row, "rule_snapshot_attested_at") is None
    ):
        raise RealRunGateBlocked("rule_snapshot_not_attested")
    return message_plan


def _required_task_binding(task: Mapping[str, Any]) -> tuple[str, int, int, str]:
    task_id = str(task.get("task_id") or "").strip()
    platform = str(task.get("platform") or "").strip().lower()
    try:
        account_id = int(task.get("account_id"))
        lottery_id = int(task.get("lottery_id"))
    except (TypeError, ValueError) as exc:
        raise RealRunGateBlocked("invalid_task_binding") from exc
    if not task_id or not platform or account_id <= 0 or lottery_id <= 0:
        raise RealRunGateBlocked("invalid_task_binding")
    return task_id, account_id, lottery_id, platform


def _as_int(value: Any, *, code: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise RealRunGateBlocked(code) from exc


def _valid_sha256(value: Any) -> bool:
    normalized = str(value or "").strip()
    return len(normalized) == 64 and all(ch in "0123456789abcdef" for ch in normalized)


def _validate_execution_evidence(
    task: Mapping[str, Any],
    row: Any,
    *,
    account_id: int,
    lottery_id: int,
    platform: str,
    plan: ValidatedActionPlanV2,
) -> str:
    message_evidence_id = str(task.get("execution_evidence_id") or "").strip()
    task_evidence_id = str(_row_get(row, "task_execution_evidence_id") or "").strip()
    evidence_id = str(_row_get(row, "evidence_id") or "").strip()
    try:
        evidence_account_id = int(_row_get(row, "evidence_account_id"))
        evidence_lottery_id = int(_row_get(row, "evidence_lottery_id"))
        evidence_snapshot_id = int(_row_get(row, "evidence_rule_snapshot_id"))
        execution_revision = int(_row_get(row, "account_execution_revision"))
    except (TypeError, ValueError) as exc:
        raise RealRunGateBlocked("execution_evidence_binding_invalid") from exc
    if execution_revision <= 0:
        raise RealRunGateBlocked("account_execution_revision_invalid")
    expected_target_hash = compute_target_hash(
        str(_row_get(row, "lottery_canonical_url") or "").strip()
    )
    expected_config_hash = compute_bilibili_api_config_hash(execution_revision)
    message_target_hash = str(task.get("target_hash") or "").strip()
    message_config_hash = str(task.get("config_hash") or "").strip()
    if (
        not evidence_id
        or message_evidence_id != evidence_id
        or task_evidence_id != evidence_id
        or evidence_account_id != account_id
        or evidence_lottery_id != lottery_id
        or evidence_snapshot_id != plan.rule_snapshot_id
        or str(_row_get(row, "evidence_platform") or "").strip().lower() != platform
        or str(_row_get(row, "evidence_execution_path_id") or "").strip()
        != plan.execution_path_id
        or str(_row_get(row, "evidence_rule_hash") or "").strip() != plan.rule_hash
        or str(_row_get(row, "evidence_action_plan_hash") or "").strip()
        != plan.plan_hash
        or str(_row_get(row, "evidence_target_hash") or "").strip()
        != expected_target_hash
        or message_target_hash != expected_target_hash
        or str(_row_get(row, "task_target_hash") or "").strip()
        != expected_target_hash
        or str(_row_get(row, "evidence_config_hash") or "").strip()
        != expected_config_hash
        or message_config_hash != expected_config_hash
        or str(_row_get(row, "task_config_hash") or "").strip()
        != expected_config_hash
    ):
        raise RealRunGateBlocked("execution_evidence_binding_invalid")
    if (
        str(_row_get(row, "evidence_status") or "").strip().lower() != "verified"
        or _row_get(row, "evidence_verified_at") is None
        or _row_get(row, "evidence_expires_at") is None
        or int(_row_get(row, "evidence_active", 0) or 0) != 1
        or int(_row_get(row, "evidence_time_bounded", 0) or 0) != 1
    ):
        raise RealRunGateBlocked("execution_evidence_not_active")
    # A single aggregate row is authoritative only when it links two immutable
    # GET-only observations and both source leases covered their observation
    # window before being released.
    if (
        not str(_row_get(row, "evidence_probe_id") or "").strip()
        or not str(_row_get(row, "evidence_shadow_task_id") or "").strip()
        or str(_row_get(row, "evidence_probe_status") or "").strip().lower()
        != "succeeded"
        or _row_get(row, "evidence_probe_finished_at") is None
        or str(_row_get(row, "evidence_shadow_status") or "").strip().lower()
        != "succeeded"
        or str(_row_get(row, "evidence_shadow_task_mode") or "").strip().lower()
        != "shadow_run"
        or str(_row_get(row, "evidence_shadow_target_hash") or "").strip()
        != expected_target_hash
        or str(_row_get(row, "evidence_shadow_config_hash") or "").strip()
        != expected_config_hash
        or int(_row_get(row, "evidence_probe_fresh", 0) or 0) != 1
        or int(_row_get(row, "evidence_shadow_fresh", 0) or 0) != 1
        or int(_row_get(row, "evidence_probe_lease_released", 0) or 0) != 1
        or int(_row_get(row, "evidence_shadow_lease_released", 0) or 0) != 1
        or int(_row_get(row, "evidence_probe_lease_covers_observation", 0) or 0)
        != 1
        or int(_row_get(row, "evidence_shadow_lease_covers_observation", 0) or 0)
        != 1
    ):
        raise RealRunGateBlocked("probe_shadow_evidence_incomplete")
    expected_follow_handle = (
        plan.follow_target_handle if "followed" in plan.required_actions else None
    )
    try:
        dynamic_id = extract_bilibili_dynamic_id(
            str(_row_get(row, "lottery_raw_url") or "").strip(),
            str(_row_get(row, "lottery_canonical_url") or "").strip()
        )
        probe_observation = validate_preflight_observation(
            _json_object(
                _row_get(row, "evidence_probe_observation"),
                code="probe_observation_invalid",
            ),
            expected_dynamic_id=dynamic_id,
            expected_actions=plan.required_actions,
            expected_execution_revision=execution_revision,
            expected_config_hash=expected_config_hash,
            expected_follow_handle=expected_follow_handle,
        )
        shadow_observation = validate_preflight_observation(
            _json_object(
                _row_get(row, "evidence_shadow_observation"),
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
        raise RealRunGateBlocked("probe_shadow_observation_invalid") from exc
    if (
        str(_row_get(row, "evidence_probe_observation_kind") or "").strip()
        != API_PREFLIGHT_KIND
        or str(_row_get(row, "source_probe_observation_kind") or "").strip()
        != API_PREFLIGHT_KIND
        or str(_row_get(row, "evidence_shadow_observation_kind") or "").strip()
        != API_PREFLIGHT_KIND
        or str(_row_get(row, "source_shadow_observation_kind") or "").strip()
        != API_PREFLIGHT_KIND
        or probe_observation.observation_hash
        != str(_row_get(row, "evidence_probe_observation_hash") or "").strip()
        or probe_observation.observation_hash
        != str(_row_get(row, "source_probe_observation_hash") or "").strip()
        or shadow_observation.observation_hash
        != str(_row_get(row, "evidence_shadow_observation_hash") or "").strip()
        or shadow_observation.observation_hash
        != str(_row_get(row, "source_shadow_observation_hash") or "").strip()
    ):
        raise RealRunGateBlocked("probe_shadow_observation_integrity_invalid")
    return evidence_id


def _validate_account_lease(
    task: Mapping[str, Any],
    row: Any,
    *,
    task_id: str,
    account_id: int,
) -> tuple[str, int]:
    message_lease_id = str(task.get("account_lease_id") or "").strip()
    task_lease_id = str(_row_get(row, "task_account_lease_id") or "").strip()
    lease_id = str(_row_get(row, "lease_id") or "").strip()
    try:
        message_generation = int(task.get("account_lease_generation"))
        task_generation = int(_row_get(row, "task_account_lease_generation"))
        lease_generation = int(_row_get(row, "lease_generation"))
        lease_account_id = int(_row_get(row, "lease_account_id"))
    except (TypeError, ValueError) as exc:
        raise RealRunGateBlocked("account_lease_binding_invalid") from exc
    if (
        not lease_id
        or message_lease_id != lease_id
        or task_lease_id != lease_id
        or message_generation <= 0
        or message_generation != task_generation
        or task_generation != lease_generation
        or lease_account_id != account_id
        or str(_row_get(row, "lease_operation_kind") or "").strip().lower()
        != "real_run"
        or str(_row_get(row, "lease_owner_id") or "").strip() != task_id
        or str(_row_get(row, "lease_task_id") or "").strip() != task_id
        or int(_row_get(row, "lease_active", 0) or 0) != 1
        or int(_row_get(row, "lease_unreleased", 0) or 0) != 1
        or int(_row_get(row, "lease_latest_generation", 0) or 0) != 1
        or int(_row_get(row, "active_account_lease_count", 0) or 0) != 1
    ):
        raise RealRunGateBlocked("account_lease_binding_invalid")
    if int(_row_get(row, "task_reconciliation_required", 0) or 0) != 0:
        raise RealRunGateBlocked("task_reconciliation_required")
    return lease_id, lease_generation


async def enforce_real_run_gate(
    task: Mapping[str, Any],
    *,
    db: GateDatabase,
    worker_id: str | None = None,
) -> RealRunGateSnapshot:
    """Prove that ``task`` is still allowed to perform a real external action.

    Any database error or missing authoritative record blocks execution. A
    platform breaker row is optional by the existing Core convention; the
    seeded global breaker row is mandatory.
    """

    task_id, account_id, lottery_id, platform = _required_task_binding(task)
    if not _process_real_run_enabled():
        raise RealRunGateBlocked("process_real_run_disabled")
    try:
        setting = await db.fetch_one(
            "SELECT setting_value FROM runtime_settings WHERE setting_key = :key",
            {"key": "real_run_enabled"},
        )
        if setting is None or _row_get(setting, "setting_value") is None:
            raise RealRunGateBlocked("runtime_setting_missing")
        if not _parse_bool(_row_get(setting, "setting_value")):
            raise RealRunGateBlocked("real_run_disabled")

        if platform not in _SUPPORTED_REAL_RUN_PLATFORMS:
            raise RealRunGateBlocked("unsupported_real_run_platform")
        if platform == "xiaohongshu":
            # Respect the process and durable global opt-ins first, then stop
            # before loading a page, selector config, evidence or credentials.
            raise RealRunGateBlocked("xiaohongshu_no_official_interaction_api")
        if platform in _SELECTOR_PATHS_AWAITING_BOUND_CONFIG_EVIDENCE:
            raise RealRunGateBlocked("selector_evidence_binding_required")

        row = await db.fetch_one(_TASK_DECISION_QUERY, {"task_id": task_id})
        if row is None:
            raise RealRunGateBlocked("task_binding_missing")

        row_task_id = str(_row_get(row, "task_id") or "").strip()
        row_account_id = _as_int(_row_get(row, "account_id"), code="task_binding_missing")
        row_lottery_id = _as_int(_row_get(row, "lottery_id"), code="task_binding_missing")
        bound_account_id = _as_int(_row_get(row, "bound_account_id"), code="task_binding_missing")
        bound_lottery_id = _as_int(_row_get(row, "bound_lottery_id"), code="task_binding_missing")
        account_platform = str(_row_get(row, "account_platform") or "").strip().lower()
        lottery_platform = str(_row_get(row, "lottery_platform") or "").strip().lower()
        if (
            row_task_id != task_id
            or row_account_id != account_id
            or bound_account_id != account_id
            or row_lottery_id != lottery_id
            or bound_lottery_id != lottery_id
            or account_platform != platform
            or lottery_platform != platform
        ):
            raise RealRunGateBlocked("task_binding_mismatch")
        if str(_row_get(row, "task_mode") or "").strip().lower() != "real_run":
            raise RealRunGateBlocked("task_not_real_run")
        plan = await _validate_target_and_action_plan(
            task,
            row,
        )
        if platform == "bilibili" and plan.execution_path_id != _BILIBILI_API_EXECUTION_PATH:
            raise RealRunGateBlocked("execution_path_not_supported")
        evidence_id = _validate_execution_evidence(
            task,
            row,
            account_id=account_id,
            lottery_id=lottery_id,
            platform=platform,
            plan=plan,
        )
        lease_id, lease_generation = _validate_account_lease(
            task,
            row,
            task_id=task_id,
            account_id=account_id,
        )

        task_status = str(_row_get(row, "task_status") or "").strip().lower()
        lottery_status = str(_row_get(row, "lottery_status") or "").strip().lower()
        account_status = str(_row_get(row, "account_status") or "").strip().lower()
        task_worker_id = str(_row_get(row, "task_worker_id") or "").strip()
        if (task_status, lottery_status, account_status) == ("queued", "claimed", "ready"):
            if task_worker_id:
                raise RealRunGateBlocked("task_worker_mismatch")
            stage = "preclaim"
        elif (task_status, lottery_status, account_status) == (
            "running",
            "running",
            "executing",
        ):
            current_worker_id = str(worker_id or "").strip()
            if not current_worker_id:
                raise RealRunGateBlocked("worker_identity_missing")
            if task_worker_id != current_worker_id:
                raise RealRunGateBlocked("task_worker_mismatch")
            stage = "running"
        else:
            raise RealRunGateBlocked("task_state_mismatch")
        if str(_row_get(row, "lottery_execution_lock") or "").strip() != task_id:
            raise RealRunGateBlocked("lottery_lock_mismatch")

        decision_id = str(_row_get(row, "decision_id") or "").strip()
        policy_decision_id = str(_row_get(row, "policy_decision_id") or "").strip()
        if not decision_id or policy_decision_id != decision_id:
            raise RealRunGateBlocked("policy_decision_missing")
        if str(_row_get(row, "decision_policy_key") or "") != REAL_RUN_POLICY_KEY:
            raise RealRunGateBlocked("policy_decision_invalid")
        if str(_row_get(row, "decision_outcome") or "").strip().lower() != "allow":
            raise RealRunGateBlocked("policy_decision_denied")
        if str(_row_get(row, "decision_subject_type") or "").strip().lower() != "lottery":
            raise RealRunGateBlocked("policy_subject_mismatch")
        if str(_row_get(row, "decision_subject_id") or "").strip() != str(lottery_id):
            raise RealRunGateBlocked("policy_subject_mismatch")

        task_policy_version = _as_int(
            _row_get(row, "task_policy_version"), code="policy_version_missing"
        )
        decision_policy_version = _as_int(
            _row_get(row, "decision_policy_version"), code="policy_version_missing"
        )
        if task_policy_version != decision_policy_version:
            raise RealRunGateBlocked("policy_version_mismatch")
        if not _parse_bool(_row_get(row, "policy_active")):
            raise RealRunGateBlocked("policy_inactive")

        global_scope = "global"
        platform_scope = f"platform:{platform}"
        breaker_rows = await db.fetch_all(
            """SELECT scope, status, reason
               FROM circuit_breakers
               WHERE scope IN (:global_scope, :platform_scope)""",
            {"global_scope": global_scope, "platform_scope": platform_scope},
        )
        global_breaker_found = False
        for breaker in breaker_rows or []:
            scope = str(_row_get(breaker, "scope") or "").strip()
            status = str(_row_get(breaker, "status") or "").strip().lower()
            if scope == global_scope:
                global_breaker_found = True
            if scope in {global_scope, platform_scope} and status != "closed":
                raise RealRunGateBlocked("circuit_breaker_blocked", scope)
        if not global_breaker_found:
            raise RealRunGateBlocked("global_breaker_missing")

        return RealRunGateSnapshot(
            task_id=task_id,
            account_id=account_id,
            lottery_id=lottery_id,
            platform=platform,
            decision_id=decision_id,
            policy_version=task_policy_version,
            stage=stage,
            action_plan=plan,
            execution_evidence_id=evidence_id,
            account_lease_id=lease_id,
            account_lease_generation=lease_generation,
        )
    except RealRunGateBlocked:
        raise
    except Exception as exc:
        raise RealRunGateBlocked("gate_database_error") from exc


async def open_unknown_outcome_breaker(
    *,
    db: GateDatabase,
    platform: str,
    action: str,
) -> None:
    """Quarantine a platform after an external mutation has an unknown result.

    A timeout or 5xx after POST cannot prove whether the action happened. Until
    reconciliation exists, opening the platform breaker prevents another
    account or retry from blindly repeating the same target interaction.
    """

    normalized_platform = str(platform or "").strip().lower()
    normalized_action = str(action or "unknown").strip().lower()[:64]
    if not normalized_platform:
        raise RealRunGateBlocked("unknown_outcome_platform_missing")
    reason = f"{normalized_platform}_{normalized_action}_outcome_unknown"
    try:
        await db.execute(
            """INSERT INTO circuit_breakers (scope, status, reason, opened_at)
               VALUES (:scope, 'open', :reason, NOW())
               ON DUPLICATE KEY UPDATE
                 status = 'open',
                 reason = :reason,
                 opened_at = NOW(),
                 updated_at = NOW()""",
            {"scope": f"platform:{normalized_platform}", "reason": reason},
        )
        persisted = await db.fetch_one(
            "SELECT status FROM circuit_breakers WHERE scope = :scope",
            {"scope": f"platform:{normalized_platform}"},
        )
        if str(_row_get(persisted, "status") or "").strip().lower() != "open":
            raise RuntimeError("unknown_outcome_breaker_not_persisted")
    except Exception as exc:
        raise RealRunGateBlocked("unknown_outcome_breaker_write_failed") from exc
