"""Worker platform-module types.

The execution registry is intentionally not imported here: Action Plan code
loads the pure ``contracts`` subpackage and must not pull in browser or HTTP
runtime dependencies. Runtime callers import ``platform_modules.registry``.
"""

from app.platform_modules.base import (
    ExecutionPath,
    PlatformModule,
    PlatformRoutingError,
)

__all__ = [
    "ExecutionPath",
    "PlatformModule",
    "PlatformRoutingError",
]
