import json
import os
from base64 import b64decode


PHASES = ("followed", "liked", "commented", "reposted")
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
    return all(bool(configured.get(phase)) for phase in PHASES)
