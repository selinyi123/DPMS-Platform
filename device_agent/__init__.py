"""Fail-closed Android device primitives for the Douyin mobile app.

This package is intentionally independent from the DPMS Core/Redis runtime.
"""

from .adb import AdbResult, AdbTransport, SubprocessAdb
from .calibration import (
    CalibrationManifest,
    ManifestError,
    NodeSelector,
    TargetCalibration,
)
from .engine import (
    ActionRequest,
    ActionResult,
    ActionStatus,
    DeviceActionEngine,
    SnapshotReport,
)
from .guard import AccountFileLock, RateLimitPolicy
from .http_service import (
    DeviceAgentHttpService,
    DeviceAgentHttpServer,
    create_http_server,
)
from .loop import HealthHeartbeat, ResidentDeviceLoop

__all__ = [
    "AccountFileLock",
    "ActionRequest",
    "ActionResult",
    "ActionStatus",
    "AdbResult",
    "AdbTransport",
    "CalibrationManifest",
    "DeviceActionEngine",
    "DeviceAgentHttpServer",
    "DeviceAgentHttpService",
    "HealthHeartbeat",
    "ManifestError",
    "NodeSelector",
    "RateLimitPolicy",
    "ResidentDeviceLoop",
    "SnapshotReport",
    "SubprocessAdb",
    "TargetCalibration",
    "create_http_server",
]
