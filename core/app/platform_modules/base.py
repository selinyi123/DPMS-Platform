"""Stable contracts for isolated lottery platform modules.

Platform modules own business capabilities (target shapes, discovery sources,
action order, and execution paths).  Database access, outbox delivery, audit,
leases, and evidence storage intentionally remain shared infrastructure.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Awaitable, Callable, Mapping


TargetCanonicalizer = Callable[[str], Awaitable[str]]
ParsedTargetValidator = Callable[[Any, str], "LotteryTargetValidation"]
DiscoveryHandler = Callable[..., Awaitable[list[dict]]]
DiscoverySessionFactory = Callable[["PlatformModule"], "PlatformDiscoverySession"]
DiscoverySourceConfigValidator = Callable[[str, str], str]
ActionPlanPostValidator = Callable[..., None]
ActionPlanAuthoringHandler = Callable[..., tuple[dict[str, Any], list[str]]]
RuntimeCapabilityBuilder = Callable[[tuple[str, ...], str], dict[str, Any]]
DispatchPlanBindingHandler = Callable[..., dict[str, Any] | None]
PublicIngressRequirementHandler = Callable[..., bool]
AccountRequiredActionsHandler = Callable[..., tuple[str, ...]]
AccountCandidateValidator = Callable[..., bool]
ExactExecutionEvidenceRevalidator = Callable[..., Awaitable[None]]
RealRunReadinessProvider = Callable[..., Awaitable[dict[str, Any]]]
REAL_RUN_EVIDENCE_KINDS = frozenset(
    {
        "exact_execution_evidence",
        "oauth_account_calibration",
    }
)


def extract_discovery_urls(value: str) -> list[str]:
    """Shared, side-effect-free URL-list parsing for platform source modules."""

    urls = re.findall(r"https?://[^\s,\uFF0C]+", value or "")
    return list(dict.fromkeys(url.strip() for url in urls))


@dataclass(frozen=True)
class LotteryTargetValidation:
    valid: bool
    kind: str | None = None
    reason: str | None = None


class PlatformCapabilityError(ValueError):
    """A request asks one platform module for an unsupported capability."""

    def __init__(
        self,
        code: str,
        *,
        platform: str,
        capability: str,
        allowed: tuple[str, ...] = (),
    ) -> None:
        self.code = str(code)
        self.platform = str(platform)
        self.capability = str(capability)
        self.allowed = tuple(allowed)
        super().__init__(self.code)

    def as_detail(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "platform": self.platform,
            "capability": self.capability,
            "allowed": list(self.allowed),
        }


class PlatformPolicyConflict(ValueError):
    """Platform-owned policy rejected a request at the shared API boundary."""

    def __init__(self, detail: Any, *, status_code: int = 409) -> None:
        self.detail = detail
        self.status_code = int(status_code)
        super().__init__(str(detail))


def parse_stored_json(value: Any) -> Any:
    """Decode database JSON without importing the readiness service.

    Platform modules are imported by ``app.action_plan`` itself, so importing
    the readiness service here would create an import cycle.  This deliberately
    mirrors the legacy API helper's tolerant database decoding contract.
    """

    if isinstance(value, (dict, list)) or value is None:
        return value
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    try:
        import json

        return json.loads(value)
    except Exception:
        return value


def build_manual_shadow_plan_binding(
    lottery,
    *,
    platform: str,
    execution_path_id: str,
    platform_label: str,
    execution_revision: int,
    selector_config: Mapping[str, Any],
) -> dict[str, Any]:
    """Shared hash mechanics for a platform-owned manual shadow policy."""

    from app.action_plan import (
        ActionPlanV2Error,
        compute_config_hash,
        compute_target_hash,
        validate_action_plan_v2,
    )

    try:
        plan = validate_action_plan_v2(
            parse_stored_json(lottery["action_plan"]),
            require_executable=False,
        )
        snapshot_id = int(lottery["authoritative_rule_snapshot_id"] or 0)
    except (ActionPlanV2Error, TypeError, ValueError, KeyError) as exc:
        code = (
            exc.code
            if isinstance(exc, ActionPlanV2Error)
            else "action_plan_binding_invalid"
        )
        raise PlatformPolicyConflict(
            {
                "message": (
                    f"{platform_label} manual-assisted Action Plan v2 "
                    "is not shadow-ready"
                ),
                "blockers": [code],
            }
        ) from exc
    if (
        plan.plan.get("platform") != platform
        or plan.execution_path_id != execution_path_id
        or snapshot_id != plan.rule_snapshot_id
        or str(lottery["rule_hash"] or "") != plan.rule_hash
        or str(lottery["action_plan_hash"] or "") != plan.plan_hash
    ):
        raise PlatformPolicyConflict(
            {
                "message": (
                    f"{platform_label} manual-assisted plan binding changed; "
                    "review again"
                ),
                "blockers": ["action_plan_rule_binding_mismatch"],
            }
        )
    if type(execution_revision) is not int or execution_revision <= 0:
        raise PlatformPolicyConflict(
            {
                "message": f"{platform_label} account revision is invalid",
                "blockers": ["execution_revision_invalid"],
            }
        )
    return {
        "rule_snapshot_id": plan.rule_snapshot_id,
        "rule_hash": plan.rule_hash,
        "action_plan_hash": plan.plan_hash,
        "execution_path_id": execution_path_id,
        "target_hash": compute_target_hash(str(lottery["canonical_url"] or "")),
        "config_hash": compute_config_hash(
            {
                "execution_path_id": execution_path_id,
                "execution_revision": execution_revision,
                "selector_config": dict(selector_config or {}),
            }
        ),
        "execution_revision": execution_revision,
        "required_actions": plan.required_actions,
        "follow_target_handle": plan.follow_target_handle,
        "action_plan": plan.plan,
    }


@dataclass(frozen=True)
class ExecutionPathMetadata:
    path_id: str
    adapter_kind: str
    task_modes: frozenset[str]
    real_actions: bool
    blocker: str | None = None
    credential_kind: str | None = None
    execution_evidence_kind: str | None = None

    def __post_init__(self) -> None:
        if self.real_actions != (
            self.execution_evidence_kind in REAL_RUN_EVIDENCE_KINDS
        ):
            raise ValueError(
                f"execution_path_evidence_kind_invalid:{self.path_id}"
            )


class PlatformDiscoverySession:
    """Per-run state and lifecycle hooks for one platform's discovery."""

    def __init__(self, platform_module: "PlatformModule") -> None:
        self.platform_module = platform_module

    async def should_defer(self, source: Mapping[str, Any]) -> bool:
        return False

    async def fetch_candidates(self, source: Mapping[str, Any]) -> list[dict]:
        return await self.platform_module.fetch_discovery_candidates(source)

    async def after_candidates(
        self,
        source: Mapping[str, Any],
        candidates: list[dict],
    ) -> int:
        return 0

    async def finalize(self) -> None:
        return None


@dataclass(frozen=True)
class PlatformModule:
    platform_id: str
    canonical_hosts: frozenset[str]
    discovery_source_types: frozenset[str]
    action_order: tuple[str, ...]
    execution_paths: tuple[ExecutionPathMetadata, ...]
    default_execution_path_id: str
    canonicalize_target_handler: TargetCanonicalizer
    validate_parsed_target_handler: ParsedTargetValidator
    target_import_short_link_hosts: frozenset[str] = frozenset()
    target_import_short_link_limit: int = 1
    target_import_short_link_error: str = (
        "target_import_short_link_batch_unsupported"
    )
    external_action_aliases: tuple[tuple[str, str], ...] = ()
    discovery_handler: DiscoveryHandler | None = None
    discovery_session_factory: DiscoverySessionFactory | None = None
    discovery_source_type_error: str | None = None
    discovery_source_config_validator: DiscoverySourceConfigValidator | None = None
    execution_mode: str = "api"
    adapter_status: str = "configured"
    configuration_kind: str = "execution"
    real_run_supported: bool = True
    real_run_blocker: str | None = None
    dry_run_supported: bool = True
    notes: str = ""
    invalid_execution_path_blocker: str = "platform_execution_path_not_bound"
    always_execution_blockers: tuple[str, ...] = ()
    credential_bound_execution_paths: bool = False
    shadow_account_execution_path_id: str | None = None
    shadow_required_configured_phases: frozenset[str] = frozenset()
    shadow_phase_contracts: Mapping[str, str] = field(
        default_factory=lambda: MappingProxyType({})
    )
    # A real selector path may require post-action state selectors while its
    # legacy read-only/manual shadow path intentionally accepts observation-
    # only shapes.  Keeping the two contracts separate preserves that
    # compatibility without weakening the mutation adapter gate.
    real_phase_contracts: Mapping[str, str] = field(
        default_factory=lambda: MappingProxyType({})
    )
    media_submission_blocker: str = "media_submission_unsupported"
    non_executable_path_errors: tuple[tuple[str, str], ...] = ()
    always_non_executable_error: str | None = None
    validate_action_plan_execution_path: bool = False
    require_all_actions_error: str | None = None
    allow_empty_repost_text: bool = False
    max_text_utf16_units: int | None = None
    text_too_long_error_template: str | None = None
    empty_content_requirement_errors: tuple[tuple[str, str], ...] = ()
    manual_follow_target_binding: bool = False
    action_plan_authoring_handler: ActionPlanAuthoringHandler | None = None
    action_plan_post_validator: ActionPlanPostValidator | None = None
    runtime_capability_builder: RuntimeCapabilityBuilder | None = None
    discovery_score_bonus: int = 0
    public_ingress_requirement_handler: PublicIngressRequirementHandler | None = None
    account_required_actions_handler: AccountRequiredActionsHandler | None = None
    account_candidate_validator: AccountCandidateValidator | None = None
    real_run_readiness_provider: RealRunReadinessProvider | None = None
    requires_exact_real_run_evidence: bool = False
    exact_execution_evidence_revalidator: (
        ExactExecutionEvidenceRevalidator | None
    ) = None
    dispatch_plan_binding_handler: DispatchPlanBindingHandler | None = None
    strategy_real_target_kinds: frozenset[str] = frozenset()
    strategy_target_kind_error: str = "invalid_lottery_target"
    probe_requires_plan_binding: bool = False
    probe_plan_error_message: str = "Platform probe requires a reviewed Action Plan"
    probe_ignored_blockers: frozenset[str] = frozenset()

    def supports_discovery_source(self, source_type: str) -> bool:
        return str(source_type or "").strip().casefold() in self.discovery_source_types

    def validate_discovery_source(self, source_type: str) -> str:
        normalized = str(source_type or "").strip().casefold()
        if normalized not in self.discovery_source_types:
            raise PlatformCapabilityError(
                "platform_discovery_source_type_not_supported",
                platform=self.platform_id,
                capability=normalized or "discovery_source_type",
                allowed=tuple(sorted(self.discovery_source_types)),
            )
        return normalized

    def validate_discovery_source_value(self, source_value: str) -> str:
        normalized = str(source_value or "").strip()
        if not normalized:
            raise PlatformCapabilityError(
                "platform_discovery_source_value_required",
                platform=self.platform_id,
                capability="source_value",
            )
        return normalized

    def validate_discovery_source_config(
        self,
        source_type: str,
        source_value: str,
    ) -> tuple[str, str]:
        """Validate one persisted source as a platform-owned atomic contract."""

        normalized_type = self.validate_discovery_source(source_type)
        normalized_value = self.validate_discovery_source_value(source_value)
        if self.discovery_source_config_validator is not None:
            normalized_value = self.discovery_source_config_validator(
                normalized_type,
                normalized_value,
            )
        if normalized_type == "url_list":
            # URL extraction is shared parsing mechanics. Whether an extracted
            # URL is actionable remains authoritative in this platform module's
            # own target validator.
            from app.utils.lottery_targets import validate_lottery_target

            urls = extract_discovery_urls(normalized_value)
            if not urls or not all(
                validate_lottery_target(self.platform_id, url).valid
                for url in urls
            ):
                raise PlatformCapabilityError(
                    "platform_discovery_url_list_target_required",
                    platform=self.platform_id,
                    capability="source_value",
                )
        return normalized_type, normalized_value

    async def fetch_discovery_candidates(
        self,
        source: Mapping[str, Any],
        *,
        keyword_search_budget: Any = None,
    ) -> list[dict]:
        source_type = self.validate_discovery_source(str(source.get("source_type") or ""))
        if self.discovery_handler is not None:
            return await self.discovery_handler(
                source,
                keyword_search_budget=keyword_search_budget,
            )
        if source_type == "url_list":
            # URL extraction is shared mechanics; the platform module remains
            # authoritative for whether this source type exists at all.
            from app.services.discovery import extract_urls

            return [
                {"raw_url": url}
                for url in extract_urls(str(source.get("source_value") or ""))
            ]
        raise PlatformCapabilityError(
            "platform_discovery_handler_missing",
            platform=self.platform_id,
            capability=source_type,
            allowed=tuple(sorted(self.discovery_source_types)),
        )

    async def canonicalize_target(self, raw_url: str) -> str:
        return await self.canonicalize_target_handler(raw_url)

    def create_discovery_session(self) -> PlatformDiscoverySession:
        if self.discovery_session_factory is not None:
            return self.discovery_session_factory(self)
        return PlatformDiscoverySession(self)

    def validate_parsed_target(self, parsed: Any, host: str) -> LotteryTargetValidation:
        return self.validate_parsed_target_handler(parsed, host)

    @property
    def execution_path_map(self) -> Mapping[str, ExecutionPathMetadata]:
        return MappingProxyType({path.path_id: path for path in self.execution_paths})

    def execution_path_blockers(self, path_id: str) -> list[str]:
        blockers: list[str] = []
        path = self.execution_path_map.get(str(path_id or "").strip())
        if path is None:
            blockers.append(self.invalid_execution_path_blocker)
        elif path.blocker:
            blockers.append(path.blocker)
        blockers.extend(self.always_execution_blockers)
        return list(dict.fromkeys(blockers))

    def execution_evidence_kind_for(self, path_id: str) -> str | None:
        path = self.execution_path_map.get(str(path_id or "").strip())
        return path.execution_evidence_kind if path is not None else None

    def normalize_external_action(self, action: str) -> str | None:
        """Map a platform journal action to one canonical DPMS phase."""

        normalized = str(action or "").strip().casefold()
        if normalized in self.action_order:
            return normalized
        canonical = dict(self.external_action_aliases).get(normalized)
        return canonical if canonical in self.action_order else None

    def non_executable_error(self, path_id: str) -> str | None:
        if self.always_non_executable_error:
            return self.always_non_executable_error
        return dict(self.non_executable_path_errors).get(str(path_id or ""))

    def validate_action_plan_path(self, path_id: str) -> None:
        if (
            self.validate_action_plan_execution_path
            and str(path_id or "") not in self.execution_path_map
        ):
            raise PlatformCapabilityError(
                self.invalid_execution_path_blocker,
                platform=self.platform_id,
                capability=str(path_id or "execution_path"),
                allowed=tuple(self.execution_path_map),
            )

    def shadow_phases_for_actions(
        self,
        required_actions: tuple[str, ...] | list[str],
    ) -> tuple[str, ...]:
        selected = set(required_actions)
        return tuple(
            phase
            for phase in self.action_order
            if phase in selected
            and phase in self.shadow_required_configured_phases
        )

    def missing_shadow_configured_phases(
        self,
        required_actions: tuple[str, ...] | list[str],
        phase_is_configured: Callable[[str], bool],
    ) -> tuple[str, ...]:
        return tuple(
            phase
            for phase in self.shadow_phases_for_actions(required_actions)
            if not phase_is_configured(phase)
        )

    def build_runtime_capability_requirements(
        self,
        required_actions: tuple[str, ...],
        path_id: str,
    ) -> dict[str, Any] | None:
        if self.runtime_capability_builder is None:
            return None
        return self.runtime_capability_builder(required_actions, path_id)

    def apply_action_plan_authoring_policy(self, **context):
        content_requirements = context["content_requirements"]
        payload_validation_errors = list(
            context.get("payload_validation_errors") or ()
        )
        if self.manual_follow_target_binding:
            from app.action_plan import bind_manual_follow_target

            content_requirements = bind_manual_follow_target(
                context["required_actions"],
                context["action_payloads"],
                content_requirements,
            )
        if self.action_plan_authoring_handler is not None:
            handler_context = dict(context)
            handler_context.update(
                content_requirements=content_requirements,
                payload_validation_errors=payload_validation_errors,
            )
            content_requirements, payload_validation_errors = (
                self.action_plan_authoring_handler(
                    **handler_context
                )
            )
        return content_requirements, list(
            dict.fromkeys(payload_validation_errors)
        )

    def account_execution_path_for_dispatch(
        self,
        *,
        task_mode: str,
        stored_execution_path: str,
        operation_kind: str = "dispatch",
    ) -> str:
        if not self.credential_bound_execution_paths:
            return ""
        if task_mode == "shadow_run" and self.shadow_account_execution_path_id:
            return self.shadow_account_execution_path_id
        return str(stored_execution_path or "")

    def build_dispatch_plan_binding(self, **context) -> dict[str, Any] | None:
        if self.dispatch_plan_binding_handler is None:
            return None
        return self.dispatch_plan_binding_handler(**context)

    async def revalidate_exact_execution_evidence(self, **context) -> None:
        if self.exact_execution_evidence_revalidator is None:
            return None
        await self.exact_execution_evidence_revalidator(**context)

    def requires_public_ingress(self, **context) -> bool:
        if self.public_ingress_requirement_handler is None:
            return False
        return bool(self.public_ingress_requirement_handler(**context))

    def account_required_actions_for_dispatch(self, **context) -> tuple[str, ...]:
        if self.account_required_actions_handler is None:
            return ()
        return tuple(self.account_required_actions_handler(**context))

    def account_candidate_supports_execution(self, **context) -> bool:
        if self.account_candidate_validator is None:
            return True
        return bool(self.account_candidate_validator(**context))

    async def validate_real_run_readiness(self, **context) -> dict[str, Any]:
        if self.real_run_readiness_provider is None:
            raise PlatformCapabilityError(
                "platform_real_run_readiness_provider_missing",
                platform=self.platform_id,
                capability="real_run_readiness",
            )
        return await self.real_run_readiness_provider(**context)

    def strategy_target_is_real_valid(self, target) -> bool:
        if not target.valid:
            return False
        return not self.strategy_real_target_kinds or (
            target.kind in self.strategy_real_target_kinds
        )

    def strategy_target_error(self, target) -> str:
        return str(target.reason or self.strategy_target_kind_error)
