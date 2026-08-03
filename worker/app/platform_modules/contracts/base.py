"""Pure Action Plan policy contract shared without browser/runtime imports."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .catalog import ACTION_ORDER, LEGACY_ACTION_ORDER


@dataclass(frozen=True)
class PlatformActionPlanContract:
    platform_id: str
    action_order: tuple[str, ...]
    default_execution_path: str
    allowed_execution_paths: frozenset[str] | None = None
    execution_path_error: str = "execution_path_not_supported"
    plan_must_be_non_executable: bool = False
    non_executable_paths: frozenset[str] = frozenset()
    fixed_required_actions: tuple[str, ...] | None = None
    fixed_required_actions_error: str | None = None
    allow_empty_repost_text: bool = False
    text_utf16_limit: int | None = None
    unique_handle_limit: int | None = None
    unique_handle_limit_error: str | None = None
    requires_empty_repost_content: bool = False
    empty_repost_content_error: str | None = None
    capability_execution_path: str | None = None
    capability_factory: Callable[[tuple[str, ...]], dict] | None = None
    capability_error: str = "weibo_oauth_capability_contract_mismatch"

    def validates_execution_path(self, path_id: str) -> bool:
        return (
            self.allowed_execution_paths is None
            or path_id in self.allowed_execution_paths
        )

    def requires_non_executable_plan(self, path_id: str) -> bool:
        return path_id in self.non_executable_paths

    def runtime_capabilities_for(
        self, path_id: str, actions: tuple[str, ...]
    ) -> dict:
        if (
            self.capability_factory is None
            or path_id != self.capability_execution_path
        ):
            return {}
        return dict(self.capability_factory(tuple(actions)))


LEGACY_FALLBACK_CONTRACT = PlatformActionPlanContract(
    platform_id="*",
    action_order=LEGACY_ACTION_ORDER,
    default_execution_path="bilibili_api_v2",
)
