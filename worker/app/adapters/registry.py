from app.adapters.base import BaseAdapter
from app.platform_modules.base import PlatformRoutingError
from app.platform_modules.registry import get_platform_module


class UnsupportedAdapter(BaseAdapter):
    def __init__(self, platform: str):
        self.PLATFORM = platform


def get_adapter(platform: str, selector_config: dict | None = None):
    """Compatibility facade; adapter ownership lives in platform modules."""

    normalized = str(platform or "").strip().lower()
    try:
        return get_platform_module(normalized).create_adapter(selector_config)
    except PlatformRoutingError:
        return UnsupportedAdapter(normalized)
