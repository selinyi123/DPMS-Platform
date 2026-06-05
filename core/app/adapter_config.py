import json
import os
from base64 import b64decode

from app.db import database


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
    configured = load_selector_config().get(platform, {})
    if not isinstance(configured, dict):
        return False
    return all(bool(configured.get(phase)) for phase in PHASES)


async def platform_has_real_adapter_async(platform: str) -> bool:
    configured = (await load_runtime_selector_config()).get(platform, {})
    if not isinstance(configured, dict):
        return False
    return all(bool(configured.get(phase)) for phase in PHASES)


def parse_json(value):
    if isinstance(value, (dict, list)) or value is None:
        return value
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    try:
        return json.loads(value)
    except Exception:
        return None
