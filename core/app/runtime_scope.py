"""Core API/control-plane runtime role contract."""

from __future__ import annotations

import os
from dataclasses import dataclass

from shared.platform_ids import PLATFORM_IDS
from shared.platform_scope import normalize_platform_scope


CORE_ROLE_ALL = "all"
CORE_ROLE_CONTROL = "control"
CORE_ROLES = frozenset({CORE_ROLE_ALL, CORE_ROLE_CONTROL})


@dataclass(frozen=True)
class CoreRuntimePlan:
    role: str
    platforms: tuple[str, ...]
    owns_platform_lanes: bool
    owns_shared_loops: bool


def build_core_runtime_plan(
    *,
    role: str | None = None,
    platform_scope: str | None = None,
) -> CoreRuntimePlan:
    normalized_role = str(
        role if role is not None else os.getenv("DPMS_CORE_ROLE", "all")
    ).strip().casefold()
    if normalized_role not in CORE_ROLES:
        raise ValueError(f"core_role_unsupported:{normalized_role or 'missing'}")
    selected = normalize_platform_scope(
        platform_scope
        if platform_scope is not None
        else os.getenv("DPMS_PLATFORM_SCOPE", "all")
    )
    if selected != PLATFORM_IDS:
        raise ValueError(
            f"core_{normalized_role}_role_requires_all_platform_scope"
        )
    return CoreRuntimePlan(
        role=normalized_role,
        platforms=(PLATFORM_IDS if normalized_role == CORE_ROLE_ALL else ()),
        owns_platform_lanes=(normalized_role == CORE_ROLE_ALL),
        owns_shared_loops=True,
    )


def validate_core_deployment_plan(
    plan: CoreRuntimePlan,
    *,
    deployment_mode: str,
) -> CoreRuntimePlan:
    """Forbid the compatibility monolith in production deployments."""

    if (
        str(deployment_mode or "").strip().casefold() == "production"
        and plan.role == CORE_ROLE_ALL
    ):
        raise RuntimeError(
            "core_all_role_forbidden_in_production_use_control_and_platform_runners"
        )
    # The shared control Core still owns one DATABASE_URL. Starting it with
    # isolated platform schemas would make API-created work invisible to the
    # platform runners and hide platform-local results from the dashboard.
    if (
        str(deployment_mode or "").strip().casefold() == "production"
        and plan.role == CORE_ROLE_CONTROL
        and str(os.getenv("DPMS_MYSQL_PLATFORM_DATABASE_MODE", "shared"))
        .strip()
        .casefold()
        == "isolated"
    ):
        raise RuntimeError(
            "core_control_isolated_database_routing_unimplemented"
        )
    return plan


__all__ = (
    "CORE_ROLE_ALL",
    "CORE_ROLE_CONTROL",
    "CoreRuntimePlan",
    "build_core_runtime_plan",
    "validate_core_deployment_plan",
)
