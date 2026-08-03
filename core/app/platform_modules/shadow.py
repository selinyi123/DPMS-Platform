"""Pure manual-shadow selector gate shared by the Core dispatch boundary."""

from app.adapter_config import (
    selector_phase_configured,
    selector_real_phase_configured,
)
from app.platform_modules.registry import get_platform_module


def missing_manual_shadow_selector_phases(
    platform: str,
    required_actions,
    selector_config,
    execution_path_id: str = "",
) -> tuple[str, ...]:
    platform_module = get_platform_module(platform)
    if platform_module is None:
        return ()
    actions = (
        tuple(required_actions)
        if isinstance(required_actions, (list, tuple))
        else ()
    )
    configured = selector_config if isinstance(selector_config, dict) else {}
    execution_path = platform_module.execution_path_map.get(
        str(execution_path_id or "").strip()
    )
    if execution_path is not None and execution_path.real_actions:
        # API and device-agent paths obtain their read-only Shadow evidence
        # from the same path-specific adapter used by Probe.  Browser
        # selectors are an execution contract only for selector adapters.
        if execution_path.adapter_kind != "selector":
            return ()
        selected = set(actions)
        return tuple(
            phase
            for phase in platform_module.action_order
            if phase in selected
            and not selector_real_phase_configured(
                platform_module.platform_id,
                configured,
                phase,
            )
        )
    return platform_module.missing_shadow_configured_phases(
        actions,
        lambda phase: selector_phase_configured(
            platform_module.platform_id,
            configured,
            phase,
        ),
    )
