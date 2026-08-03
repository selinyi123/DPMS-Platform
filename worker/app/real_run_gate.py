"""Fail-closed worker-side validation for queued real-run tasks.

Dispatch-time approval is necessary but not sufficient: a task can wait in the
stream while the global switch, a circuit breaker, or its policy decision
changes.  The worker calls :func:`enforce_real_run_gate` immediately before it
starts a real-run task and may inject the same call into an executor's
``before_action`` hook to re-check the gate before every external mutation.

This module deliberately takes the database object as an argument.  That keeps
the gate independent from the Core service and makes the fail-closed behaviour
fully testable without a database or an external platform.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol


REAL_RUN_POLICY_KEY = "real_run_gate"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


class GateDatabase(Protocol):
    async def fetch_one(self, query: str, values: Mapping[str, Any] | None = None): ...

    async def fetch_all(self, query: str, values: Mapping[str, Any] | None = None): ...


class RealRunGateBlocked(RuntimeError):
    """Raised when a worker cannot prove that a real-run action is allowed."""

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


_TASK_DECISION_QUERY = """
SELECT
  tr.task_id,
  tr.account_id,
  tr.lottery_id,
  tr.task_mode,
  tr.decision_id,
  tr.policy_version AS task_policy_version,
  a.id AS bound_account_id,
  a.platform AS account_platform,
  l.id AS bound_lottery_id,
  l.platform AS lottery_platform,
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


async def enforce_real_run_gate(
    task: Mapping[str, Any],
    *,
    db: GateDatabase,
) -> RealRunGateSnapshot:
    """Prove that ``task`` is still allowed to perform a real external action.

    Any database error or missing authoritative record blocks execution.  A
    platform-specific breaker row is optional by the existing Core convention:
    absence means no platform breaker has been opened.  The seeded global
    breaker row is mandatory, so losing the breaker table/state fails closed.
    """

    task_id, account_id, lottery_id, platform = _required_task_binding(task)
    try:
        setting = await db.fetch_one(
            "SELECT setting_value FROM runtime_settings WHERE setting_key = :key",
            {"key": "real_run_enabled"},
        )
        if setting is None or _row_get(setting, "setting_value") is None:
            raise RealRunGateBlocked("runtime_setting_missing")
        if not _parse_bool(_row_get(setting, "setting_value")):
            raise RealRunGateBlocked("real_run_disabled")

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
        )
    except RealRunGateBlocked:
        raise
    except Exception as exc:
        raise RealRunGateBlocked("gate_database_error") from exc
