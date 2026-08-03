"""Immutable registry for the four isolated lottery platform modules."""

from __future__ import annotations

import importlib
import threading
from types import MappingProxyType
from typing import Iterable, Iterator, Mapping

from app.platform_modules.base import PlatformModule
from app.platform_modules.catalog import (
    PLATFORM_MODULE_SPECS,
    PlatformModuleSpec,
)


SHADOW_PHASE_CONTRACT_KINDS = frozenset(
    {
        "click_and_state",
        "click_or_state",
        "input_submit",
        "input_submit_state",
        "state_only",
    }
)


class PlatformRegistry:
    def __init__(self, modules: Iterable[PlatformModule]) -> None:
        indexed: dict[str, PlatformModule] = {}
        for module in modules:
            key = str(module.platform_id or "").strip().casefold()
            if not key or key != module.platform_id:
                raise ValueError("platform_module_id_invalid")
            if key in indexed:
                raise ValueError(f"platform_module_duplicate:{key}")
            if type(module.discovery_source_types) is not frozenset:
                raise ValueError(f"platform_discovery_source_types_mutable:{key}")
            if type(module.canonical_hosts) is not frozenset:
                raise ValueError(f"platform_canonical_hosts_mutable:{key}")
            if type(module.action_order) is not tuple:
                raise ValueError(f"platform_action_order_mutable:{key}")
            if not module.action_order or len(module.action_order) != len(
                set(module.action_order)
            ):
                raise ValueError(f"platform_action_order_invalid:{key}")
            if (
                type(module.target_import_short_link_hosts) is not frozenset
                or not module.target_import_short_link_hosts.issubset(
                    module.canonical_hosts
                )
                or type(module.target_import_short_link_limit) is not int
                or module.target_import_short_link_limit < 0
                or not isinstance(module.target_import_short_link_error, str)
                or not module.target_import_short_link_error.strip()
            ):
                raise ValueError(
                    f"platform_target_import_short_link_policy_invalid:{key}"
                )
            if type(module.external_action_aliases) is not tuple or any(
                type(alias) is not tuple
                or len(alias) != 2
                or not all(isinstance(value, str) for value in alias)
                for alias in module.external_action_aliases
            ):
                raise ValueError(
                    f"platform_external_action_aliases_mutable:{key}"
                )
            external_aliases = dict(module.external_action_aliases)
            if (
                len(external_aliases) != len(module.external_action_aliases)
                or any(
                    not alias
                    or alias != alias.strip().casefold()
                    or canonical not in module.action_order
                    for alias, canonical in module.external_action_aliases
                )
            ):
                raise ValueError(
                    f"platform_external_action_aliases_invalid:{key}"
                )
            if type(module.execution_paths) is not tuple or any(
                type(path.task_modes) is not frozenset
                for path in module.execution_paths
            ):
                raise ValueError(f"platform_execution_paths_mutable:{key}")
            path_ids = tuple(path.path_id for path in module.execution_paths)
            if len(path_ids) != len(set(path_ids)):
                raise ValueError(f"platform_execution_paths_duplicate:{key}")
            if type(module.shadow_required_configured_phases) is not frozenset:
                raise ValueError(f"platform_shadow_phases_mutable:{key}")
            if (
                type(module.strategy_real_target_kinds) is not frozenset
                or type(module.probe_ignored_blockers) is not frozenset
            ):
                raise ValueError(f"platform_policy_sets_mutable:{key}")
            if not module.shadow_required_configured_phases.issubset(
                module.action_order
            ):
                raise ValueError(f"platform_shadow_phases_invalid:{key}")
            if type(module.shadow_phase_contracts) is not MappingProxyType:
                raise ValueError(f"platform_shadow_phase_contracts_mutable:{key}")
            if not set(module.shadow_phase_contracts).issubset(
                module.action_order
            ) or not set(module.shadow_phase_contracts.values()).issubset(
                SHADOW_PHASE_CONTRACT_KINDS
            ):
                raise ValueError(f"platform_shadow_phase_contracts_invalid:{key}")
            if type(module.real_phase_contracts) is not MappingProxyType:
                raise ValueError(f"platform_real_phase_contracts_mutable:{key}")
            if not set(module.real_phase_contracts).issubset(
                module.action_order
            ) or not set(module.real_phase_contracts.values()).issubset(
                SHADOW_PHASE_CONTRACT_KINDS
            ):
                raise ValueError(f"platform_real_phase_contracts_invalid:{key}")
            if not module.execution_paths:
                raise ValueError(f"platform_execution_paths_required:{key}")
            if module.default_execution_path_id not in path_ids:
                raise ValueError(f"platform_default_execution_path_invalid:{key}")
            if (
                module.credential_bound_execution_paths
                and module.account_candidate_validator is None
            ):
                raise ValueError(
                    f"platform_account_candidate_validator_required:{key}"
                )
            if module.real_run_readiness_provider is None:
                raise ValueError(
                    f"platform_real_run_readiness_provider_required:{key}"
                )
            if (
                module.requires_exact_real_run_evidence
                and module.exact_execution_evidence_revalidator is None
            ):
                raise ValueError(
                    f"platform_exact_evidence_revalidator_required:{key}"
                )
            if (
                module.probe_requires_plan_binding
                and module.dispatch_plan_binding_handler is None
            ):
                raise ValueError(
                    f"platform_probe_plan_binding_handler_required:{key}"
                )
            indexed[key] = module
        self._modules: Mapping[str, PlatformModule] = MappingProxyType(indexed)

    def get(self, platform: str) -> PlatformModule | None:
        return self._modules.get(str(platform or "").strip().casefold())

    def require(self, platform: str) -> PlatformModule:
        module = self.get(platform)
        if module is None:
            raise KeyError(str(platform or ""))
        return module

    def items(self):
        return self._modules.items()

    def values(self):
        return self._modules.values()

    def keys(self):
        return self._modules.keys()

    def __iter__(self) -> Iterator[str]:
        return iter(self._modules)

    def __len__(self) -> int:
        return len(self._modules)


class PlatformModuleUnavailableError(RuntimeError):
    """One installed platform failed to load without poisoning its peers."""

    def __init__(self, platform: str) -> None:
        self.platform = str(platform or "").strip().casefold()
        super().__init__(
            f"platform_module_unavailable:{self.platform or 'missing'}"
        )


class LazyPlatformRegistry:
    """Read-only registry that imports exactly one requested platform.

    Import failures are cached for the process lifetime.  This avoids repeated
    execution of a partially initialized module and makes the failure domain
    explicit: callers targeting that platform fail closed, while registry
    enumeration and all other platforms remain available.
    """

    def __init__(self, specs: Mapping[str, PlatformModuleSpec]) -> None:
        normalized: dict[str, PlatformModuleSpec] = {}
        for key, spec in specs.items():
            platform = str(key or "").strip().casefold()
            if (
                not platform
                or platform != key
                or platform != spec.platform_id
                or platform in normalized
            ):
                raise ValueError("platform_module_spec_invalid")
            normalized[platform] = spec
        self._specs: Mapping[str, PlatformModuleSpec] = MappingProxyType(
            normalized
        )
        self._modules: dict[str, PlatformModule] = {}
        self._failures: dict[str, BaseException] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _validate_spec(module: PlatformModule, spec: PlatformModuleSpec) -> None:
        # Reuse the complete descriptor validation contract before comparing
        # boot-safe metadata.  A stale catalog must fail only this platform.
        PlatformRegistry((module,))
        if (
            module.platform_id != spec.platform_id
            or module.canonical_hosts != spec.canonical_hosts
            or module.discovery_source_types != spec.discovery_source_types
            or module.action_order != spec.action_order
            or module.default_execution_path_id
            != spec.default_execution_path_id
            or module.real_run_supported is not spec.real_run_supported
            or module.real_run_blocker != spec.real_run_blocker
            or module.configuration_kind != spec.configuration_kind
            or frozenset(
                path.adapter_kind
                for path in module.execution_paths
                if path.real_actions
            )
            != spec.real_adapter_kinds
        ):
            raise ValueError(
                f"platform_module_catalog_mismatch:{spec.platform_id}"
            )

    def get(self, platform: str) -> PlatformModule | None:
        key = str(platform or "").strip().casefold()
        spec = self._specs.get(key)
        if spec is None:
            return None
        with self._lock:
            module = self._modules.get(key)
            if module is not None:
                return module
            if key in self._failures:
                raise PlatformModuleUnavailableError(key) from self._failures[
                    key
                ]
            try:
                imported = importlib.import_module(spec.module_name)
                module = getattr(imported, spec.export_name)
                if not isinstance(module, PlatformModule):
                    raise TypeError(f"platform_module_export_invalid:{key}")
                self._validate_spec(module, spec)
            except Exception as exc:
                self._failures[key] = exc
                raise PlatformModuleUnavailableError(key) from exc
            self._modules[key] = module
            return module

    def require(self, platform: str) -> PlatformModule:
        module = self.get(platform)
        if module is None:
            raise KeyError(str(platform or ""))
        return module

    def items(self):
        return tuple((key, self.require(key)) for key in self._specs)

    def values(self):
        return tuple(self.require(key) for key in self._specs)

    def keys(self):
        return self._specs.keys()

    def failure(self, platform: str) -> BaseException | None:
        key = str(platform or "").strip().casefold()
        with self._lock:
            return self._failures.get(key)

    def __iter__(self) -> Iterator[str]:
        return iter(self._specs)

    def __len__(self) -> int:
        return len(self._specs)


PLATFORM_REGISTRY = LazyPlatformRegistry(PLATFORM_MODULE_SPECS)


def get_platform_module(platform: str) -> PlatformModule | None:
    return PLATFORM_REGISTRY.get(platform)


def require_platform_module(platform: str) -> PlatformModule:
    return PLATFORM_REGISTRY.require(platform)


def get_platform_modules() -> Mapping[str, PlatformModule]:
    return MappingProxyType(dict(PLATFORM_REGISTRY.items()))
