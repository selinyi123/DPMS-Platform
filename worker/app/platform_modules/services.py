"""Explicit shared-service capabilities for Worker platform modules.

Platform modules may depend on these immutable facades, but never on the
``task_runner`` or ``adapter_probe`` modules themselves.  Keeping every
capability as a declared field makes the dependency boundary reviewable and
prevents a platform from reaching arbitrary central-orchestrator globals.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True, slots=True)
class TaskExecutionServices:
    """Platform-neutral capabilities available during task execution."""

    database: Any
    WORKER_ID: str
    ActionPlanV2Error: type[BaseException]
    AccountStatusPersistenceFailed: type[BaseException]
    RealRunGateBlocked: type[BaseException]
    StartedActionIntent: type
    TaskClaimConflict: type[BaseException]
    TaskOwnershipLost: type[BaseException]
    TaskSettlementUnconfirmed: type[BaseException]
    _claim_positive_int: Callable[..., int]
    await_safety_settlement: Callable[..., Any]
    canonical_json_bytes: Callable[..., bytes]
    compute_target_hash: Callable[..., str]
    credential_to_cookie_header: Callable[..., str]
    emergency_stop_real_runs_and_revoke_lease: Callable[..., Any]
    enforce_task_real_run_gate: Callable[..., Any]
    execute_browser_observation_shadow: Callable[..., Any]
    execute_real_task: Callable[..., Any]
    gate_execution_action_plan: Callable[..., Any]
    gate_requested_actions: Callable[..., tuple[str, ...]]
    get_latest_phase: Callable[..., Any]
    load_account_credential: Callable[..., Any]
    mark_action_intent_unknown: Callable[..., Any]
    open_unknown_outcome_breaker: Callable[..., Any]
    parse_json_field: Callable[..., Any]
    prepare_and_start_action_intent: Callable[..., Any]
    quarantine_external_action_outcome: Callable[..., Any]
    record_event: Callable[..., Any]
    refresh_task_lease: Callable[..., Any]
    renew_account_operation_lease: Callable[..., Any]
    row_get: Callable[..., Any]
    save_phase: Callable[..., Any]
    set_account_status: Callable[..., Any]
    settle_action_intent: Callable[..., Any]
    structured_log: Callable[..., None]
    validate_action_plan_v2: Callable[..., Any]
    cookie_vault: Any
    CREDENTIAL_AAD: str


@dataclass(frozen=True, slots=True)
class ProbeExecutionServices:
    """Platform-neutral capabilities available during read-only probes."""

    database: Any
    ProbeObservation: type
    credential_to_cookie_header: Callable[..., str]
    execute_browser_observation_probe: Callable[..., Any]
    load_probe_credential: Callable[..., Any]


def require_task_services(value: object | None) -> TaskExecutionServices:
    if not isinstance(value, TaskExecutionServices):
        raise RuntimeError("platform_task_services_required")
    return value


def require_probe_services(value: object | None) -> ProbeExecutionServices:
    if not isinstance(value, ProbeExecutionServices):
        raise RuntimeError("platform_probe_services_required")
    return value


__all__ = (
    "ProbeExecutionServices",
    "TaskExecutionServices",
    "require_probe_services",
    "require_task_services",
)
