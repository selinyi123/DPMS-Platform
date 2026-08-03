"""Worker-side platform execution contracts.

Platform modules own platform-specific routing metadata while the task runner
continues to own shared claims, leases, evidence, intents, and settlement.  The
objects in this module are deliberately immutable so one platform cannot
modify another platform's capabilities at runtime.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping

from app.platform_modules.services import (
    ProbeExecutionServices,
    TaskExecutionServices,
    require_probe_services,
    require_task_services,
)


ExecutionHandler = Callable[[dict, object, object], Awaitable[Any]]
ShadowClaimValidator = Callable[..., Awaitable[None]]
ProbeHandler = Callable[[dict, object], Awaitable[Any]]
ProbeAuthorityValidator = Callable[[Mapping[str, Any]], bool]
ProbeClaimValidator = Callable[..., Awaitable[None]]
ProbeTerminalMaterializer = Callable[..., Awaitable[None]]
RealRunTaskPrecondition = Callable[[Mapping[str, Any]], str | None]
RealRunEvidenceContextLoader = Callable[..., Awaitable[Any]]
RealRunEvidenceValidator = Callable[..., "RealRunEvidenceBinding"]
REAL_RUN_EVIDENCE_KINDS = frozenset(
    {
        "exact_execution_evidence",
        "oauth_account_calibration",
    }
)


class PlatformRoutingError(ValueError):
    """A task cannot be routed through the selected platform contract."""

    def __init__(self, code: str) -> None:
        self.code = str(code or "platform_route_invalid")
        super().__init__(self.code)


@dataclass(frozen=True)
class RealRunEvidenceBinding:
    """Platform-owned result consumed by the shared real-run gate."""

    evidence_id: str
    execution_revision: int
    oauth_capabilities: dict[str, Any] | None = None
    account_identity: str | None = None


def _plan_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return {}
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


@dataclass(frozen=True)
class ExecutionPath:
    """One independently selectable execution path for a platform."""

    path_id: str
    credential_kind: str
    supported_modes: frozenset[str]
    shadow_executor: str | None = None
    real_executor: str | None = None
    shadow_handler: ExecutionHandler | None = None
    real_handler: ExecutionHandler | None = None
    shadow_claim_validator: ShadowClaimValidator | None = None
    selector_binding_modes: frozenset[str] = frozenset()
    unsupported_mode_error: str | None = None
    dry_run_requires_executable_plan: bool = False
    confirmed_intent_settlement: bool = False
    execution_evidence_kind: str | None = None

    def __post_init__(self) -> None:
        if not self.path_id or self.path_id != self.path_id.strip():
            raise ValueError("execution_path_id_invalid")
        if (
            not self.credential_kind
            or self.credential_kind != self.credential_kind.strip()
        ):
            raise ValueError(f"execution_path_credential_kind_invalid:{self.path_id}")
        if not self.supported_modes or not self.supported_modes.issubset(
            {"dry_run", "shadow_run", "real_run"}
        ):
            raise ValueError(f"execution_path_modes_invalid:{self.path_id}")
        if "shadow_run" in self.supported_modes and not self.shadow_executor:
            raise ValueError(f"execution_path_shadow_executor_missing:{self.path_id}")
        if "real_run" in self.supported_modes and not self.real_executor:
            raise ValueError(f"execution_path_real_executor_missing:{self.path_id}")
        if "shadow_run" in self.supported_modes and not callable(self.shadow_handler):
            raise ValueError(f"execution_path_shadow_handler_missing:{self.path_id}")
        if "real_run" in self.supported_modes and not callable(self.real_handler):
            raise ValueError(f"execution_path_real_handler_missing:{self.path_id}")
        if (
            self.shadow_claim_validator is not None
            and (
                "shadow_run" not in self.supported_modes
                or not callable(self.shadow_claim_validator)
            )
        ):
            raise ValueError(
                f"execution_path_shadow_claim_validator_invalid:{self.path_id}"
            )
        if (
            self.confirmed_intent_settlement
            and "real_run" not in self.supported_modes
        ):
            raise ValueError(
                f"execution_path_intent_settlement_mode_invalid:{self.path_id}"
            )
        supports_real_run = "real_run" in self.supported_modes
        if supports_real_run != (
            self.execution_evidence_kind in REAL_RUN_EVIDENCE_KINDS
        ):
            raise ValueError(
                f"execution_path_evidence_kind_invalid:{self.path_id}"
            )

    def executor_for(self, mode: str) -> str:
        normalized_mode = str(mode or "").strip().lower()
        if normalized_mode not in self.supported_modes:
            raise PlatformRoutingError(
                self.unsupported_mode_error
                or f"execution_path_mode_not_supported:{self.path_id}:{normalized_mode}"
            )
        if normalized_mode == "dry_run":
            return "dry_run"
        if normalized_mode == "shadow_run" and self.shadow_executor:
            return self.shadow_executor
        if normalized_mode == "real_run" and self.real_executor:
            return self.real_executor
        raise PlatformRoutingError(
            self.unsupported_mode_error
            or f"execution_path_executor_missing:{self.path_id}:{normalized_mode}"
        )

    def handler_for(self, mode: str) -> ExecutionHandler:
        """Return the platform-owned callable for a side-effecting run mode."""

        normalized_mode = str(mode or "").strip().lower()
        # Reuse the public compatibility route validation so unsupported modes
        # retain their existing, platform-specific error codes.
        self.executor_for(normalized_mode)
        if normalized_mode == "shadow_run" and callable(self.shadow_handler):
            return self.shadow_handler
        if normalized_mode == "real_run" and callable(self.real_handler):
            return self.real_handler
        raise PlatformRoutingError(
            self.unsupported_mode_error
            or f"execution_path_handler_missing:{self.path_id}:{normalized_mode}"
        )

    async def execute(
        self,
        mode: str,
        task: dict,
        adapter: object,
        pool: object,
        *,
        runtime: TaskExecutionServices | None = None,
    ) -> Any:
        """Invoke platform-owned control flow with injected shared services."""

        services = require_task_services(runtime)
        return await self.handler_for(mode)(
            task,
            adapter,
            pool,
            runtime=services,
        )

    async def validate_shadow_claim(
        self,
        *,
        runtime: TaskExecutionServices | None = None,
        **context: Any,
    ) -> None:
        """Run optional platform-owned immutable claim validation."""

        if self.shadow_claim_validator is not None:
            await self.shadow_claim_validator(
                runtime=require_task_services(runtime),
                **context,
            )


@dataclass(frozen=True)
class PlatformModule:
    """Immutable platform-owned adapter, phase, credential, and route metadata."""

    platform_id: str
    adapter_factory: Callable[[dict | None], object]
    probe_handler: ProbeHandler
    action_order: tuple[str, ...]
    real_target_kinds: frozenset[str]
    execution_paths: tuple[ExecutionPath, ...]
    default_execution_path: str
    invalid_execution_path_error: str | None = None
    capability_block_reason: str | None = None
    probe_done_selector_phases: frozenset[str] = frozenset()
    intent_action_mapper: Callable[[list[str]], list[str]] | None = None
    mode_execution_path_overrides: tuple[tuple[str, str], ...] = ()
    real_run_block_reason: str | None = None
    real_run_task_precondition: RealRunTaskPrecondition | None = None
    real_run_evidence_context_loader: RealRunEvidenceContextLoader | None = None
    real_run_evidence_validator: RealRunEvidenceValidator | None = None
    probe_authority_validator: ProbeAuthorityValidator | None = None
    probe_claim_validator: ProbeClaimValidator | None = None
    probe_terminal_materializer: ProbeTerminalMaterializer | None = None

    def __post_init__(self) -> None:
        platform = str(self.platform_id or "").strip().lower()
        if not platform or platform != self.platform_id:
            raise ValueError("platform_id_invalid")
        if (
            not self.action_order
            or any(
                not isinstance(action, str) or not action
                for action in self.action_order
            )
            or len(set(self.action_order)) != len(self.action_order)
        ):
            raise ValueError(f"platform_action_order_invalid:{platform}")
        if not callable(self.probe_handler):
            raise ValueError(f"platform_probe_handler_invalid:{platform}")
        if (
            self.probe_authority_validator is not None
            and not callable(self.probe_authority_validator)
        ):
            raise ValueError(
                f"platform_probe_authority_validator_invalid:{platform}"
            )
        if (
            self.probe_claim_validator is not None
            and not callable(self.probe_claim_validator)
        ):
            raise ValueError(
                f"platform_probe_claim_validator_invalid:{platform}"
            )
        if (
            self.probe_terminal_materializer is not None
            and not callable(self.probe_terminal_materializer)
        ):
            raise ValueError(
                f"platform_probe_terminal_materializer_invalid:{platform}"
            )
        if type(self.real_target_kinds) is not frozenset:
            raise ValueError(f"platform_real_target_kinds_mutable:{platform}")
        path_ids = tuple(path.path_id for path in self.execution_paths)
        if not path_ids or len(set(path_ids)) != len(path_ids):
            raise ValueError(f"platform_execution_paths_invalid:{platform}")
        if self.default_execution_path not in path_ids:
            raise ValueError(f"platform_default_execution_path_invalid:{platform}")
        if self.invalid_execution_path_error is not None and (
            not self.invalid_execution_path_error
            or self.invalid_execution_path_error
            != self.invalid_execution_path_error.strip()
        ):
            raise ValueError(f"platform_execution_path_error_invalid:{platform}")
        if (
            type(self.probe_done_selector_phases) is not frozenset
            or not self.probe_done_selector_phases.issubset(self.action_order)
        ):
            raise ValueError(f"platform_probe_done_selector_phases_invalid:{platform}")
        override_modes = tuple(mode for mode, _ in self.mode_execution_path_overrides)
        if len(set(override_modes)) != len(override_modes):
            raise ValueError(f"platform_mode_override_duplicate:{platform}")
        for mode, override_path_id in self.mode_execution_path_overrides:
            if mode not in {"dry_run", "shadow_run", "real_run"}:
                raise ValueError(f"platform_mode_override_invalid:{platform}")
            if override_path_id not in path_ids:
                raise ValueError(f"platform_mode_override_path_invalid:{platform}")
        supports_real_run = any(
            "real_run" in path.supported_modes for path in self.execution_paths
        )
        if self.real_run_block_reason is not None and (
            not self.real_run_block_reason
            or self.real_run_block_reason != self.real_run_block_reason.strip()
            or supports_real_run
        ):
            raise ValueError(f"platform_real_run_block_reason_invalid:{platform}")
        if self.real_run_task_precondition is not None and not callable(
            self.real_run_task_precondition
        ):
            raise ValueError(
                f"platform_real_run_precondition_invalid:{platform}"
            )
        if supports_real_run != callable(
            self.real_run_evidence_context_loader
        ):
            raise ValueError(
                f"platform_real_run_evidence_context_loader_invalid:{platform}"
            )
        if supports_real_run != callable(self.real_run_evidence_validator):
            raise ValueError(
                f"platform_real_run_evidence_validator_invalid:{platform}"
            )

    @property
    def supported_modes(self) -> frozenset[str]:
        modes: set[str] = set()
        for path in self.execution_paths:
            modes.update(path.supported_modes)
        return frozenset(modes)

    def create_adapter(self, selector_config: dict | None = None):
        return self.adapter_factory(selector_config)

    async def execute_probe(
        self,
        binding: dict,
        pool: object,
        *,
        runtime: ProbeExecutionServices | None = None,
    ) -> Any:
        """Invoke the probe strategy owned by this platform descriptor."""

        services = require_probe_services(runtime)
        return await self.probe_handler(
            binding,
            pool,
            runtime=services,
        )

    async def validate_probe_claim(
        self,
        *,
        runtime: ProbeExecutionServices | None = None,
        **context: Any,
    ) -> bool:
        """Run an exact platform claim contract when one is registered."""

        if self.probe_claim_validator is None:
            return False
        await self.probe_claim_validator(
            runtime=require_probe_services(runtime),
            **context,
        )
        return True

    def validate_probe_authority(
        self,
        authoritative_message: Mapping[str, Any],
    ) -> bool:
        """Validate optional platform fields on a durable probe envelope."""

        if self.probe_authority_validator is None:
            return True
        return self.probe_authority_validator(authoritative_message) is True

    async def materialize_terminal_probe(
        self,
        probe_id: str,
        *,
        runtime: ProbeExecutionServices | None = None,
    ) -> None:
        """Repair platform-owned terminal evidence, if the platform has it."""

        if self.probe_terminal_materializer is not None:
            await self.probe_terminal_materializer(
                probe_id=probe_id,
                runtime=require_probe_services(runtime),
            )

    def execution_path(self, path_id: str) -> ExecutionPath:
        normalized = str(path_id or "").strip()
        for path in self.execution_paths:
            if path.path_id == normalized:
                return path
        raise PlatformRoutingError(
            self.invalid_execution_path_error
            or f"{self.platform_id}_execution_path_not_supported"
        )

    def execution_path_id_from_plan(self, action_plan: Any) -> str:
        plan = _plan_object(action_plan)
        value = str(plan.get("execution_path_id") or "").strip()
        return value or self.default_execution_path

    def route(self, mode: str, action_plan: Any) -> tuple[ExecutionPath, str]:
        normalized_mode = str(mode or "").strip().lower()
        # Validate the persisted plan path even when a mode intentionally uses
        # a different runtime channel. This prevents an unknown path from
        # acquiring authority through a mode override.
        self.execution_path(self.execution_path_id_from_plan(action_plan))
        runtime_path_id = dict(self.mode_execution_path_overrides).get(
            normalized_mode,
            self.execution_path_id_from_plan(action_plan),
        )
        runtime_path = self.execution_path(runtime_path_id)
        return runtime_path, runtime_path.executor_for(normalized_mode)

    def requires_selector_binding(self, mode: str, action_plan: Any) -> bool:
        normalized_mode = str(mode or "").strip().lower()
        path, _ = self.route(normalized_mode, action_plan)
        return normalized_mode in path.selector_binding_modes

    def execution_evidence_kind_for(self, action_plan: Any) -> str | None:
        """Return the evidence authority owned by the persisted real path."""

        return self.execution_path(
            self.execution_path_id_from_plan(action_plan)
        ).execution_evidence_kind

    def selected_phases(self, action_plan: Any) -> list[str]:
        plan = _plan_object(action_plan)
        raw_actions = plan.get("required_actions")
        if not isinstance(raw_actions, list):
            return []
        # Queue data is untrusted; membership comparison tolerates non-hashable
        # entries and preserves the legacy fail-closed parser behavior.
        return [action for action in self.action_order if action in raw_actions]

    def phases(self, action_plan: Any) -> list[str]:
        return self.selected_phases(action_plan) or list(self.action_order)

    def expected_intent_actions(self, phases: list[str]) -> list[str]:
        if self.intent_action_mapper is None:
            return list(phases)
        return list(self.intent_action_mapper(list(phases)))

    def real_run_precondition_code(
        self, task: Mapping[str, Any]
    ) -> str | None:
        if self.real_run_block_reason:
            return self.real_run_block_reason
        if self.real_run_task_precondition is None:
            return None
        return self.real_run_task_precondition(task)

    async def load_real_run_evidence_context(
        self,
        *,
        db: object,
        task_id: str,
    ) -> Mapping[str, Any]:
        """Load only this platform's authoritative evidence projection."""

        if self.real_run_evidence_context_loader is None:
            raise PlatformRoutingError("unsupported_real_run_platform")
        result = await self.real_run_evidence_context_loader(
            db=db,
            task_id=task_id,
        )
        if result is None:
            return {}
        if not isinstance(result, Mapping):
            raise PlatformRoutingError(
                "platform_real_run_evidence_context_invalid"
            )
        return dict(result)

    def validate_real_run_evidence(self, **context: Any) -> RealRunEvidenceBinding:
        if self.real_run_evidence_validator is None:
            raise PlatformRoutingError("unsupported_real_run_platform")
        result = self.real_run_evidence_validator(**context)
        if not isinstance(result, RealRunEvidenceBinding):
            raise PlatformRoutingError("platform_real_run_evidence_result_invalid")
        return result
