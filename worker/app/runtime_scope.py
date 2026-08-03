"""Worker process-role and platform-scope contract."""

from __future__ import annotations

import os
from dataclasses import dataclass

from shared.platform_ids import PLATFORM_IDS
from shared.platform_scope import exact_platform_scope, normalize_platform_scope


WORKER_ROLE_ALL = "all"
WORKER_ROLE_CONTROL = "control"
WORKER_ROLE_PLATFORM = "platform"
WORKER_ROLES = frozenset(
    {WORKER_ROLE_ALL, WORKER_ROLE_CONTROL, WORKER_ROLE_PLATFORM}
)


@dataclass(frozen=True)
class WorkerRuntimePlan:
    """The exact loops one Worker process is authorized to own."""

    role: str
    platforms: tuple[str, ...]
    owns_platform_lanes: bool
    owns_control_loops: bool
    owns_legacy_fanout: bool

    @property
    def scope_label(self) -> str:
        if not self.platforms:
            return "control"
        if self.platforms == PLATFORM_IDS:
            return "all"
        return ",".join(self.platforms)


def build_worker_runtime_plan(
    *,
    role: str | None = None,
    platform_scope: str | None = None,
) -> WorkerRuntimePlan:
    """Validate one mutually-exclusive Worker deployment role."""

    normalized_role = str(
        role if role is not None else os.getenv("DPMS_WORKER_ROLE", "all")
    ).strip().casefold()
    raw_scope = (
        platform_scope
        if platform_scope is not None
        else os.getenv("DPMS_PLATFORM_SCOPE", "all")
    )
    if normalized_role not in WORKER_ROLES:
        raise ValueError(f"worker_role_unsupported:{normalized_role or 'missing'}")

    if normalized_role == WORKER_ROLE_PLATFORM:
        platform = exact_platform_scope(raw_scope)
        return WorkerRuntimePlan(
            role=normalized_role,
            platforms=(platform,),
            owns_platform_lanes=True,
            owns_control_loops=False,
            owns_legacy_fanout=False,
        )

    selected = normalize_platform_scope(raw_scope)
    if selected != PLATFORM_IDS:
        raise ValueError(
            f"worker_{normalized_role}_role_requires_all_platform_scope"
        )
    if normalized_role == WORKER_ROLE_CONTROL:
        return WorkerRuntimePlan(
            role=normalized_role,
            platforms=(),
            owns_platform_lanes=False,
            owns_control_loops=True,
            owns_legacy_fanout=True,
        )
    return WorkerRuntimePlan(
        role=normalized_role,
        platforms=PLATFORM_IDS,
        owns_platform_lanes=True,
        owns_control_loops=True,
        owns_legacy_fanout=True,
    )


def validate_worker_deployment_plan(
    plan: WorkerRuntimePlan,
    *,
    deployment_mode: str,
    configured_instance_id: str | None = None,
) -> WorkerRuntimePlan:
    """Forbid the compatibility monolith in production deployments."""

    if (
        str(deployment_mode or "").strip().casefold() == "production"
        and plan.role == WORKER_ROLE_ALL
    ):
        raise RuntimeError(
            "worker_all_role_forbidden_in_production_use_control_and_platform_workers"
        )
    # The control Worker shares the Core API database. Isolated platform
    # schemas are not yet populated/routed from that API, so starting the
    # control role in this mode would silently strand queued work.
    if (
        str(deployment_mode or "").strip().casefold() == "production"
        and plan.role == WORKER_ROLE_CONTROL
        and str(os.getenv("DPMS_MYSQL_PLATFORM_DATABASE_MODE", "shared"))
        .strip()
        .casefold()
        == "isolated"
    ):
        raise RuntimeError(
            "worker_control_isolated_database_routing_unimplemented"
        )
    if (
        str(deployment_mode or "").strip().casefold() == "production"
        and str(configured_instance_id or "").strip()
    ):
        raise RuntimeError(
            "worker_fixed_instance_id_forbidden_in_production"
        )
    return plan


__all__ = (
    "WORKER_ROLE_ALL",
    "WORKER_ROLE_CONTROL",
    "WORKER_ROLE_PLATFORM",
    "WorkerRuntimePlan",
    "build_worker_runtime_plan",
    "validate_worker_deployment_plan",
)
