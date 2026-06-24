import json
import os
from base64 import b64decode

from app.db import database


PHASES = ("followed", "liked", "commented", "reposted")
STRUCTURED_SELECTOR_PLATFORMS = ("bilibili", "weibo", "xiaohongshu", "douyin")
API_REAL_ADAPTER_PLATFORMS = ("bilibili",)
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
    if platform_has_api_real_adapter(platform):
        return True
    configured = load_selector_config().get(platform, {})
    return selector_config_complete(platform, configured)


async def platform_has_real_adapter_async(platform: str) -> bool:
    if platform_has_api_real_adapter(platform):
        return True
    configured = (await load_runtime_selector_config()).get(platform, {})
    return selector_config_complete(platform, configured)


def platform_has_api_real_adapter(platform: str) -> bool:
    return platform in API_REAL_ADAPTER_PLATFORMS


def platform_has_runtime_real_adapter(selector_config: dict, platform: str) -> bool:
    if platform_has_api_real_adapter(platform):
        return True
    configured = selector_config.get(platform, {}) if isinstance(selector_config, dict) else {}
    return selector_config_complete(platform, configured)


def platform_real_adapter_kind(selector_config: dict, platform: str) -> str:
    if platform_has_api_real_adapter(platform):
        return "api"
    configured = selector_config.get(platform, {}) if isinstance(selector_config, dict) else {}
    if selector_config_complete(platform, configured):
        return "selector"
    return "none"


def selector_config_complete(platform: str, configured: dict) -> bool:
    if not isinstance(configured, dict):
        return False
    if platform not in STRUCTURED_SELECTOR_PLATFORMS:
        return all(bool(configured.get(phase)) for phase in PHASES)
    if not all(click_selectors(configured.get(phase)) for phase in ("followed", "liked", "reposted")):
        return False
    comment = configured.get("commented")
    if not isinstance(comment, dict):
        return False
    return bool(
        selector_values(comment.get("input") or comment.get("inputs"))
        and selector_values(comment.get("submit") or comment.get("submits"))
    )


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
