import json
import os
from base64 import b64decode


PHASES = ("followed", "liked", "commented", "reposted")
STRUCTURED_SELECTOR_PLATFORMS = ("bilibili", "weibo", "xiaohongshu", "douyin")
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


def decode_b64(value: str) -> str:
    if not value:
        return ""
    try:
        return b64decode(value).decode("utf-8")
    except Exception:
        return ""


def selectors_for(platform: str) -> dict:
    configured = load_selector_config().get(platform, {})
    return configured if isinstance(configured, dict) else {}


def has_complete_selectors(platform: str) -> bool:
    configured = selectors_for(platform)
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
