"""Immutable registry for independent Worker platform modules."""

from __future__ import annotations

import importlib
import threading
from collections.abc import Iterator, Mapping

from app.platform_modules.base import PlatformModule, PlatformRoutingError


_MODULE_SPECS = {
    "bilibili": ("app.platform_modules.bilibili", "BILIBILI"),
    "weibo": ("app.platform_modules.weibo", "WEIBO"),
    "xiaohongshu": ("app.platform_modules.xiaohongshu", "XIAOHONGSHU"),
    "douyin": ("app.platform_modules.douyin", "DOUYIN"),
}


class PlatformModuleUnavailableError(PlatformRoutingError):
    """A single platform runtime failed to import and is locally unavailable."""

    def __init__(self, platform: str) -> None:
        self.platform = str(platform or "").strip().lower()
        super().__init__(
            f"platform_module_unavailable:{self.platform or 'missing'}"
        )


class LazyPlatformModules(Mapping[str, PlatformModule]):
    """Load one platform runtime only when a task targets it."""

    def __init__(self, specs: Mapping[str, tuple[str, str]]) -> None:
        self._specs = dict(specs)
        self._modules: dict[str, PlatformModule] = {}
        self._failures: dict[str, Exception] = {}
        self._lock = threading.RLock()

    def __getitem__(self, platform: str) -> PlatformModule:
        key = str(platform or "").strip().lower()
        spec = self._specs.get(key)
        if spec is None:
            raise KeyError(key)
        with self._lock:
            cached = self._modules.get(key)
            if cached is not None:
                return cached
            if key in self._failures:
                raise PlatformModuleUnavailableError(key) from self._failures[
                    key
                ]
            try:
                module_name, export_name = spec
                imported = importlib.import_module(module_name)
                module = getattr(imported, export_name)
                if (
                    not isinstance(module, PlatformModule)
                    or module.platform_id != key
                ):
                    raise TypeError(f"platform_module_export_invalid:{key}")
            except Exception as exc:
                self._failures[key] = exc
                raise PlatformModuleUnavailableError(key) from exc
            self._modules[key] = module
            return module

    def __iter__(self) -> Iterator[str]:
        return iter(self._specs)

    def __len__(self) -> int:
        return len(self._specs)

    def failure(self, platform: str) -> Exception | None:
        key = str(platform or "").strip().lower()
        with self._lock:
            return self._failures.get(key)


PLATFORM_MODULES: Mapping[str, PlatformModule] = LazyPlatformModules(
    _MODULE_SPECS
)


def get_platform_module(platform: str) -> PlatformModule:
    key = str(platform or "").strip().lower()
    if key not in PLATFORM_MODULES:
        raise PlatformRoutingError(f"platform_not_supported:{key or 'missing'}")
    return PLATFORM_MODULES[key]


def registered_platforms() -> tuple[str, ...]:
    return tuple(PLATFORM_MODULES)
