import json
import os
from base64 import b64decode

from app.db import database


PHASES = ("followed", "liked", "commented", "reposted")
STRUCTURED_SELECTOR_PLATFORMS = ("bilibili", "weibo", "xiaohongshu", "douyin")
API_REAL_ADAPTER_PLATFORMS = ("bilibili",)
OAUTH_REAL_ADAPTER_PLATFORMS = ("weibo",)
MANUAL_ASSISTED_ONLY_PLATFORMS = ("xiaohongshu", "douyin")
OBSERVATION_ONLY_PHASES = {
    "weibo": ("followed", "liked", "commented", "favorited", "reposted"),
    "xiaohongshu": ("followed", "liked", "commented", "favorited"),
    "douyin": ("followed", "liked", "commented", "favorited", "reposted"),
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
    return selector_config_complete(platform, configured)


async def platform_has_real_adapter_async(platform: str) -> bool:
    if platform in MANUAL_ASSISTED_ONLY_PLATFORMS:
        return False
    if platform_has_api_real_adapter(platform) or platform_has_oauth_real_adapter(platform):
        return True
    configured = (await load_runtime_selector_config()).get(platform, {})
    return selector_config_complete(platform, configured)


def platform_has_api_real_adapter(platform: str) -> bool:
    return platform in API_REAL_ADAPTER_PLATFORMS


def platform_has_oauth_real_adapter(platform: str) -> bool:
    return platform in OAUTH_REAL_ADAPTER_PLATFORMS


def platform_has_runtime_real_adapter(selector_config: dict, platform: str) -> bool:
    # A selector configuration is useful for read-only shadow validation, but
    # it is not an official participant-interaction API for a manual-only
    # platform and cannot authorize external mutations.
    if platform in MANUAL_ASSISTED_ONLY_PLATFORMS:
        return False
    if platform_has_api_real_adapter(platform) or platform_has_oauth_real_adapter(platform):
        return True
    configured = selector_config.get(platform, {}) if isinstance(selector_config, dict) else {}
    return selector_config_complete(platform, configured)


def platform_real_adapter_kind(selector_config: dict, platform: str) -> str:
    if platform in MANUAL_ASSISTED_ONLY_PLATFORMS:
        return "manual_assisted"
    if platform_has_api_real_adapter(platform):
        return "api"
    if platform_has_oauth_real_adapter(platform):
        return "oauth"
    configured = selector_config.get(platform, {}) if isinstance(selector_config, dict) else {}
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
    phases = selector_phases_for_platform(platform)
    if platform in OBSERVATION_ONLY_PHASES:
        return all(selector_phase_configured(platform, configured, phase) for phase in phases)
    if platform not in STRUCTURED_SELECTOR_PLATFORMS:
        return all(bool(configured.get(phase)) for phase in phases)
    for phase in ("followed", "liked", "reposted"):
        phase_config = configured.get(phase)
        if not isinstance(phase_config, dict):
            return False
        if not click_selectors(phase_config) or not selector_values(
            phase_config.get("done") or phase_config.get("success")
        ):
            return False
    comment = configured.get("commented")
    if not isinstance(comment, dict):
        return False
    return bool(
        selector_values(comment.get("input") or comment.get("inputs"))
        and selector_values(comment.get("submit") or comment.get("submits"))
        and selector_values(comment.get("done") or comment.get("success"))
    )


def selector_phases_for_platform(platform: str) -> tuple[str, ...]:
    return OBSERVATION_ONLY_PHASES.get(str(platform or "").strip().lower(), PHASES)


def selector_phase_configured(platform: str, configured: dict, phase: str) -> bool:
    """Validate one platform-specific selector or observation phase.

    Manual-assisted platforms use selectors only for read-only observation.
    They therefore do not require mutation click/read-back pairs.  Douyin
    collection and repost are stricter: only explicit, independent ``done``
    state selectors qualify, never the generic share entry point.
    """

    if not isinstance(configured, dict):
        return False
    normalized = str(platform or "").strip().lower()
    if phase not in selector_phases_for_platform(normalized):
        return False
    value = configured.get(phase)
    if normalized in OBSERVATION_ONLY_PHASES:
        if phase == "commented":
            return isinstance(value, dict) and bool(
                selector_values(value.get("input") or value.get("inputs"))
                and selector_values(value.get("submit") or value.get("submits"))
            )
        if normalized == "douyin" and phase in {"favorited", "reposted"}:
            return isinstance(value, dict) and bool(
                selector_values(value.get("done") or value.get("success"))
            )
        if isinstance(value, dict):
            return bool(
                click_selectors(value)
                or selector_values(value.get("done") or value.get("success"))
            )
        return bool(selector_values(value))
    if normalized not in STRUCTURED_SELECTOR_PLATFORMS:
        return bool(value)
    done = selector_values(value.get("done") or value.get("success")) if isinstance(value, dict) else []
    if phase == "commented":
        return isinstance(value, dict) and bool(
            selector_values(value.get("input") or value.get("inputs"))
            and selector_values(value.get("submit") or value.get("submits"))
            and done
        )
    return bool(click_selectors(value) and done)


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
