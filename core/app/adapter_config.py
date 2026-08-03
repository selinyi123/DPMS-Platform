import json
import os
from base64 import b64decode

from app.platform_modules import (
    PlatformModuleUnavailableError,
    get_platform_module,
)
from app.platform_modules.catalog import (
    PLATFORM_MODULE_SPECS,
    platform_module_spec,
)


PHASES = ("followed", "liked", "commented", "reposted")
STRUCTURED_SELECTOR_PLATFORMS = tuple(PLATFORM_MODULE_SPECS)
API_REAL_ADAPTER_PLATFORMS = tuple(
    platform
    for platform, spec in PLATFORM_MODULE_SPECS.items()
    if "api" in spec.real_adapter_kinds
)
OAUTH_REAL_ADAPTER_PLATFORMS = tuple(
    platform
    for platform, spec in PLATFORM_MODULE_SPECS.items()
    if "oauth" in spec.real_adapter_kinds
)
DEVICE_AGENT_REAL_ADAPTER_PLATFORMS = tuple(
    platform
    for platform, spec in PLATFORM_MODULE_SPECS.items()
    if "device_agent" in spec.real_adapter_kinds
)
MANUAL_ASSISTED_ONLY_PLATFORMS = tuple(
    platform
    for platform, spec in PLATFORM_MODULE_SPECS.items()
    if not spec.real_run_supported
)
OBSERVATION_ONLY_PHASES = {
    platform: spec.action_order
    for platform, spec in PLATFORM_MODULE_SPECS.items()
    if spec.configuration_kind == "observation"
}
# Compatibility alias for callers introduced with the XHS/Douyin modules.
MANUAL_OBSERVATION_PHASES = OBSERVATION_ONLY_PHASES
SELECTOR_ENV = "DPMS_ADAPTER_SELECTORS"
SELECTOR_B64_ENV = "DPMS_ADAPTER_SELECTORS_B64"


def load_selector_config() -> dict:
    raw = decode_b64(os.getenv(SELECTOR_B64_ENV, "").strip()) or os.getenv(SELECTOR_ENV, "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


async def load_runtime_selector_config() -> dict:
    # Keep selector-shape helpers importable without initializing database
    # infrastructure; only the runtime overlay actually needs the DB.
    from app.db import database

    config = load_selector_config()
    try:
        rows = await database.fetch_all("SELECT platform, config_json FROM adapter_selector_configs")
    except Exception:
        return config
    for row in rows:
        parsed = parse_json(row["config_json"])
        if isinstance(parsed, dict):
            config[row["platform"]] = parsed
    return config


def decode_b64(value: str) -> str:
    if not value:
        return ""
    try:
        return b64decode(value).decode("utf-8")
    except Exception:
        return ""


def platform_has_real_adapter(platform: str) -> bool:
    if platform in MANUAL_ASSISTED_ONLY_PLATFORMS:
        return False
    if platform_has_api_real_adapter(platform) or platform_has_oauth_real_adapter(platform):
        return True
    configured = load_selector_config().get(platform, {})
    if platform_has_device_agent_real_adapter(platform):
        return device_agent_config_complete(configured)
    return selector_config_complete(platform, configured)


async def platform_has_real_adapter_async(platform: str) -> bool:
    if platform in MANUAL_ASSISTED_ONLY_PLATFORMS:
        return False
    if platform_has_api_real_adapter(platform) or platform_has_oauth_real_adapter(platform):
        return True
    configured = (await load_runtime_selector_config()).get(platform, {})
    if platform_has_device_agent_real_adapter(platform):
        return device_agent_config_complete(configured)
    return selector_config_complete(platform, configured)


def platform_has_api_real_adapter(platform: str) -> bool:
    return platform in API_REAL_ADAPTER_PLATFORMS


def platform_has_oauth_real_adapter(platform: str) -> bool:
    return platform in OAUTH_REAL_ADAPTER_PLATFORMS


def platform_has_device_agent_real_adapter(platform: str) -> bool:
    return platform in DEVICE_AGENT_REAL_ADAPTER_PLATFORMS


def device_agent_config_complete(configured: dict) -> bool:
    try:
        from shared.douyin_device_contract import (
            normalize_douyin_device_public_config,
        )

        normalize_douyin_device_public_config(configured)
    except (TypeError, ValueError):
        return False
    return True


def platform_has_runtime_real_adapter(selector_config: dict, platform: str) -> bool:
    # A selector configuration is useful for read-only shadow validation, but
    # it is not an official participant-interaction API for a manual-only
    # platform and cannot authorize external mutations.
    if platform in MANUAL_ASSISTED_ONLY_PLATFORMS:
        return False
    if platform_has_api_real_adapter(platform) or platform_has_oauth_real_adapter(platform):
        return True
    configured = selector_config.get(platform, {}) if isinstance(selector_config, dict) else {}
    if platform_has_device_agent_real_adapter(platform):
        return device_agent_config_complete(configured)
    return selector_config_complete(platform, configured)


def platform_real_adapter_kind(selector_config: dict, platform: str) -> str:
    if platform in MANUAL_ASSISTED_ONLY_PLATFORMS:
        return "manual_assisted"
    if platform_has_api_real_adapter(platform):
        return "api"
    if platform_has_oauth_real_adapter(platform):
        return "oauth"
    configured = selector_config.get(platform, {}) if isinstance(selector_config, dict) else {}
    if platform_has_device_agent_real_adapter(platform):
        return "device_agent" if device_agent_config_complete(configured) else "none"
    if selector_config_complete(platform, configured):
        return "selector"
    return "none"


def platform_probe_ready_for_real_actions(platform: str, probe_summary) -> bool:
    """Return whether the available probe covers the platform's execution path.

    The current probe worker inspects browser selectors. That evidence cannot
    qualify Bilibili's HTTP API mutation path. For selector adapters, neither
    the Probe nor Shadow evidence is bound to the exact selector-config version,
    so changing selectors after evidence collection could authorize unrelated
    clicks. Keep both paths fail-closed until versioned evidence is persisted.
    """
    if (
        platform_has_api_real_adapter(platform)
        or platform_has_oauth_real_adapter(platform)
        or platform in STRUCTURED_SELECTOR_PLATFORMS
    ):
        return False
    return bool(
        isinstance(probe_summary, dict)
        and probe_summary.get("ready_for_real_actions") is True
    )


def selector_config_complete(platform: str, configured: dict) -> bool:
    if not isinstance(configured, dict):
        return False
    if platform_has_device_agent_real_adapter(platform):
        return device_agent_config_complete(configured)
    phases = selector_phases_for_platform(platform)
    try:
        platform_module = get_platform_module(platform)
    except PlatformModuleUnavailableError:
        return False
    if platform_module is not None:
        return all(
            selector_real_phase_configured(platform, configured, phase)
            for phase in phases
        )
    return all(bool(configured.get(phase)) for phase in phases)


def selector_phases_for_platform(platform: str) -> tuple[str, ...]:
    spec = platform_module_spec(platform)
    return spec.action_order if spec is not None else PHASES


def selector_phase_configured(platform: str, configured: dict, phase: str) -> bool:
    """Validate one selector phase using its platform-owned contract kind."""

    if not isinstance(configured, dict):
        return False
    try:
        module = get_platform_module(platform)
    except PlatformModuleUnavailableError:
        return False
    if module is None:
        return bool(configured.get(phase))
    if phase not in module.action_order:
        return False
    value = configured.get(phase)
    contract_kind = module.shadow_phase_contracts.get(phase)
    return selector_contract_configured(contract_kind, value)


def selector_real_phase_configured(
    platform: str,
    configured: dict,
    phase: str,
) -> bool:
    """Validate one phase against the mutation adapter's stricter contract."""

    if not isinstance(configured, dict):
        return False
    try:
        module = get_platform_module(platform)
    except PlatformModuleUnavailableError:
        return False
    if module is None or phase not in module.action_order:
        return False
    value = configured.get(phase)
    contract_kind = module.real_phase_contracts.get(
        phase,
        module.shadow_phase_contracts.get(phase),
    )
    return selector_contract_configured(contract_kind, value)


def selector_contract_configured(contract_kind: str | None, value) -> bool:
    """Interpret one generic selector contract declared by a platform module."""

    if contract_kind == "click_or_state":
        if isinstance(value, dict):
            return bool(
                click_selectors(value)
                or selector_values(value.get("done") or value.get("success"))
            )
        return bool(selector_values(value))
    if not isinstance(value, dict):
        return False
    done = selector_values(value.get("done") or value.get("success"))
    if contract_kind == "state_only":
        return bool(done)
    inputs = selector_values(value.get("input") or value.get("inputs"))
    submits = selector_values(value.get("submit") or value.get("submits"))
    if contract_kind == "input_submit":
        return bool(inputs and submits)
    if contract_kind == "input_submit_state":
        return bool(inputs and submits and done)
    if contract_kind == "click_and_state":
        return bool(click_selectors(value) and done)
    return False


def recommended_config_from_probe(probe_result, platform: str) -> dict:
    """The selector config a succeeded probe recommends for ``platform``.

    A probe stores ``_recommended_config`` (built worker-side from the
    selectors it actually saw visible on the page) keyed by platform. This is
    the pure extractor: it pulls that platform's recommendation out of a stored
    probe ``result`` and returns it, or ``{}`` when the probe has no usable
    recommendation. It does NOT decide completeness — callers gate on
    ``selector_config_complete`` so the same bar as a hand-saved config applies.
    """
    result = parse_json(probe_result)
    if not isinstance(result, dict):
        return {}
    recommended = result.get("_recommended_config")
    if not isinstance(recommended, dict):
        return {}
    platform_config = recommended.get(platform)
    return platform_config if isinstance(platform_config, dict) and platform_config else {}


def click_selectors(value) -> list[str]:
    if isinstance(value, dict):
        value = value.get("click") or value.get("selectors") or value.get("buttons")
    return selector_values(value)


def selector_values(value) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def parse_json(value):
    if isinstance(value, (dict, list)) or value is None:
        return value
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    try:
        return json.loads(value)
    except Exception:
        return None
