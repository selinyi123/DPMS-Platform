"""Fail-closed worker-side validation for queued real-run tasks.

Dispatch-time approval is necessary but not sufficient: a task may wait in the
stream while the global switch, a circuit breaker, or its policy decision
changes. The worker calls :func:`enforce_real_run_gate` immediately before it
starts a real task and again before every external mutation.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from app.action_plan import (
    ActionPlanV2Error,
    ValidatedActionPlanV2,
    canonical_json_bytes,
    compute_rule_hash,
    validate_action_plan_v2,
)
from app.execution_intents import (
    ExecutionIntentValidationError,
    validate_task_execution_intent,
)
from app.task_streams import LEGACY_TASK_STREAM_KEY
from shared.execution_contracts import (
    lease_operation_kind_for_execution_intent,
)
from app.platform_modules.base import PlatformRoutingError
from app.platform_modules.evidence import RealRunGateBlocked
from app.platform_modules.registry import (
    PlatformModuleUnavailableError,
    get_platform_module,
)


REAL_RUN_POLICY_KEY = "real_run_gate"
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_EXPLICIT_FALSE = frozenset({"0", "false", "no", "off"})


def platform_real_run_block_reason(platform: str) -> str | None:
    """Return the authoritative pre-execution capability blocker, if any."""

    try:
        module = get_platform_module(platform)
    except PlatformModuleUnavailableError:
        return "platform_module_unavailable"
    except PlatformRoutingError:
        return "unsupported_real_run_platform"
    return module.real_run_block_reason


class GateDatabase(Protocol):
    async def fetch_one(self, query: str, values: Mapping[str, Any] | None = None): ...

    async def fetch_all(self, query: str, values: Mapping[str, Any] | None = None): ...

    async def execute(self, query: str, values: Mapping[str, Any] | None = None): ...


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
    execution_action_plan: ValidatedActionPlanV2
    requested_actions: tuple[str, ...]
    execution_intent_id: str | None
    execution_intent_kind: str
    execution_intent_binding_hash: str | None
    execution_evidence_id: str
    account_lease_id: str
    account_lease_generation: int
    execution_revision: int
    oauth_capabilities: dict[str, Any] | None
    weibo_uid: str | None


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
  intent_root.contract_version AS root_contract_version,
  intent_root.intent_id AS root_intent_id,
  intent_root.intent_hash AS root_intent_hash,
  intent_root.lottery_id AS root_lottery_id,
  intent_root.source_task_id AS root_source_task_id,
  intent_root.source_account_id AS root_source_account_id,
  intent_root.platform AS root_platform,
  intent_root.raw_url AS root_raw_url,
  intent_root.canonical_url AS root_canonical_url,
  intent_root.full_action_plan AS root_full_action_plan,
  intent_root.full_action_plan_hash AS root_full_action_plan_hash,
  intent_root.full_required_actions AS root_full_required_actions,
  intent_root.full_required_actions_hash AS root_full_required_actions_hash,
  intent_root.rule_snapshot_id AS root_rule_snapshot_id,
  intent_root.rule_hash AS root_rule_hash,
  intent_root.execution_path_id AS root_execution_path_id,
  intent_root.target_hash AS root_target_hash,
  intent_binding.contract_version AS binding_contract_version,
  intent_binding.task_id AS binding_task_id,
  intent_binding.intent_id AS binding_intent_id,
  intent_binding.lottery_id AS binding_lottery_id,
  intent_binding.account_id AS binding_account_id,
  intent_binding.binding_kind AS binding_kind,
  intent_binding.requested_actions AS binding_requested_actions,
  intent_binding.requested_actions_hash AS binding_requested_actions_hash,
  intent_binding.bound_action_plan AS binding_action_plan,
  intent_binding.bound_action_plan_hash AS binding_action_plan_hash,
  intent_binding.evidence_action_plan_hash
    AS binding_evidence_action_plan_hash,
  intent_binding.rule_snapshot_id AS binding_rule_snapshot_id,
  intent_binding.rule_hash AS binding_rule_hash,
  intent_binding.execution_evidence_id AS binding_execution_evidence_id,
  intent_binding.execution_evidence_kind
    AS binding_execution_evidence_kind,
  intent_binding.exact_execution_evidence_id
    AS binding_exact_execution_evidence_id,
  intent_binding.oauth_calibration_id AS binding_oauth_calibration_id,
  intent_binding.execution_path_id AS binding_execution_path_id,
  intent_binding.target_hash AS binding_target_hash,
  intent_binding.config_hash AS binding_config_hash,
  intent_binding.execution_revision AS binding_execution_revision,
  intent_binding.account_lease_id AS binding_account_lease_id,
  intent_binding.account_lease_generation
    AS binding_account_lease_generation,
  intent_binding.binding_hash AS binding_hash,
  legacy_outbox.stream_key AS legacy_outbox_stream_key,
  legacy_outbox.status AS legacy_outbox_status,
  legacy_outbox.dedup_key AS legacy_outbox_dedup_key,
  legacy_outbox.payload AS legacy_outbox_payload,
  a.id AS bound_account_id,
  a.platform AS account_platform,
  a.status AS account_status,
  a.execution_revision AS account_execution_revision,
  CASE WHEN OCTET_LENGTH(a.encrypted_credential) > 0 THEN 1 ELSE 0 END
    AS account_credential_present,
  EXISTS (
    SELECT 1
    FROM account_active_risk_states active_risk
    WHERE active_risk.account_id = tr.account_id
      AND active_risk.active_until > NOW()
  ) AS account_active_risk,
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
LEFT JOIN task_execution_intent_bindings intent_binding
  ON intent_binding.task_id = tr.task_id
LEFT JOIN lottery_execution_intents intent_root
  ON intent_root.lottery_id = intent_binding.lottery_id
 AND intent_root.intent_id = intent_binding.intent_id
LEFT JOIN outbox_events legacy_outbox
  ON legacy_outbox.dedup_key = tr.task_id
 AND legacy_outbox.stream_key = :legacy_task_stream_key
 AND legacy_outbox.status = 'sent'
LEFT JOIN accounts a ON a.id = tr.account_id
LEFT JOIN lotteries l ON l.id = tr.lottery_id
LEFT JOIN lottery_rule_snapshots rs
  ON rs.id = l.authoritative_rule_snapshot_id AND rs.lottery_id = l.id
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


class _EvidenceContextRow:
    """Expose platform evidence over a shared authoritative task row."""

    __slots__ = ("_base", "_evidence")

    def __init__(
        self,
        base: Any,
        evidence: Mapping[str, Any],
    ) -> None:
        self._base = base
        self._evidence = evidence

    def get(self, key: str, default=None):
        if key in self._evidence:
            return self._evidence[key]
        return _row_get(self._base, key, default)


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


def _validate_account_lease(
    task: Mapping[str, Any],
    row: Any,
    *,
    task_id: str,
    account_id: int,
    execution_intent_kind: str,
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
    try:
        expected_operation_kind = (
            lease_operation_kind_for_execution_intent(
                execution_intent_kind
            )
        )
    except ValueError as exc:
        raise RealRunGateBlocked(
            "account_lease_binding_invalid"
        ) from exc
    if (
        not lease_id
        or message_lease_id != lease_id
        or task_lease_id != lease_id
        or message_generation <= 0
        or message_generation != task_generation
        or task_generation != lease_generation
        or lease_account_id != account_id
        or str(_row_get(row, "lease_operation_kind") or "").strip().lower()
        != expected_operation_kind
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

        try:
            platform_module = get_platform_module(platform)
        except PlatformModuleUnavailableError as exc:
            raise RealRunGateBlocked("platform_module_unavailable") from exc
        except PlatformRoutingError as exc:
            raise RealRunGateBlocked("unsupported_real_run_platform") from exc
        precondition_code = platform_module.real_run_precondition_code(task)
        if precondition_code:
            # Respect the process and durable global opt-ins first, then stop
            # before loading a page, selector config, evidence or credentials.
            raise RealRunGateBlocked(precondition_code)

        row = await db.fetch_one(
            _TASK_DECISION_QUERY,
            {
                "task_id": task_id,
                "legacy_task_stream_key": LEGACY_TASK_STREAM_KEY,
            },
        )
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
        if _parse_bool(_row_get(row, "account_active_risk")):
            raise RealRunGateBlocked("recent_account_risk_event")
        if str(_row_get(row, "task_mode") or "").strip().lower() != "real_run":
            raise RealRunGateBlocked("task_not_real_run")
        plan = await _validate_target_and_action_plan(
            task,
            row,
        )
        try:
            execution_intent = validate_task_execution_intent(
                task,
                row,
                task_id=task_id,
                account_id=account_id,
                lottery_id=lottery_id,
                platform=platform,
                full_plan=plan,
                expected_evidence_kind=(
                    platform_module.execution_evidence_kind_for(
                        plan.plan
                    )
                    or ""
                ),
            )
        except ExecutionIntentValidationError as exc:
            raise RealRunGateBlocked(exc.code) from exc
        try:
            evidence_context = (
                await platform_module.load_real_run_evidence_context(
                    db=db,
                    task_id=task_id,
                )
            )
            evidence_binding = platform_module.validate_real_run_evidence(
                task=task,
                row=_EvidenceContextRow(row, evidence_context),
                account_id=account_id,
                lottery_id=lottery_id,
                platform=platform,
                plan=plan,
                execution_plan=execution_intent.action_plan,
            )
        except PlatformRoutingError as exc:
            raise RealRunGateBlocked(exc.code) from exc
        evidence_id = evidence_binding.evidence_id
        execution_revision = evidence_binding.execution_revision
        oauth_capabilities = evidence_binding.oauth_capabilities
        weibo_uid = evidence_binding.account_identity
        lease_id, lease_generation = _validate_account_lease(
            task,
            row,
            task_id=task_id,
            account_id=account_id,
            execution_intent_kind=execution_intent.binding_kind,
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
            execution_action_plan=execution_intent.action_plan,
            requested_actions=execution_intent.requested_actions,
            execution_intent_id=execution_intent.intent_id,
            execution_intent_kind=execution_intent.binding_kind,
            execution_intent_binding_hash=execution_intent.binding_hash,
            execution_evidence_id=evidence_id,
            account_lease_id=lease_id,
            account_lease_generation=lease_generation,
            execution_revision=execution_revision,
            oauth_capabilities=oauth_capabilities,
            weibo_uid=weibo_uid,
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
