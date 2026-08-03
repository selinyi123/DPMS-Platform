"""Public platform-module registry API."""

from app.platform_modules.base import (
    ExecutionPathMetadata,
    LotteryTargetValidation,
    PlatformCapabilityError,
    PlatformDiscoverySession,
    PlatformModule,
    PlatformPolicyConflict,
)
from app.platform_modules.registry import (
    PLATFORM_REGISTRY,
    LazyPlatformRegistry,
    PlatformModuleUnavailableError,
    PlatformRegistry,
    get_platform_module,
    get_platform_modules,
    require_platform_module,
)
from app.platform_modules.catalog import registered_platforms

__all__ = (
    "ExecutionPathMetadata",
    "LotteryTargetValidation",
    "PLATFORM_REGISTRY",
    "LazyPlatformRegistry",
    "PlatformCapabilityError",
    "PlatformDiscoverySession",
    "PlatformModule",
    "PlatformModuleUnavailableError",
    "PlatformPolicyConflict",
    "PlatformRegistry",
    "get_platform_module",
    "get_platform_modules",
    "registered_platforms",
    "require_platform_module",
)
