"""Durable per-platform manual discovery scan request topology."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType

from shared.platform_ids import PLATFORM_IDS


DISCOVERY_SCAN_PROTOCOL_VERSION = 1
DISCOVERY_SCAN_STREAM_PREFIX = (
    f"discovery_scan_requests:v{DISCOVERY_SCAN_PROTOCOL_VERSION}"
)
DISCOVERY_SCAN_GROUP_PREFIX = (
    f"discovery-platform-runners:v{DISCOVERY_SCAN_PROTOCOL_VERSION}"
)
DISCOVERY_SCAN_RESULT_PREFIX = (
    f"discovery_scan_result:v{DISCOVERY_SCAN_PROTOCOL_VERSION}"
)


@dataclass(frozen=True)
class DiscoveryScanStreamBinding:
    platform: str
    stream_key: str
    group_name: str


DISCOVERY_SCAN_STREAM_BINDINGS = tuple(
    DiscoveryScanStreamBinding(
        platform=platform,
        stream_key=f"{DISCOVERY_SCAN_STREAM_PREFIX}:{platform}",
        group_name=f"{DISCOVERY_SCAN_GROUP_PREFIX}:{platform}",
    )
    for platform in PLATFORM_IDS
)
DISCOVERY_SCAN_BINDINGS_BY_PLATFORM = MappingProxyType(
    {
        binding.platform: binding
        for binding in DISCOVERY_SCAN_STREAM_BINDINGS
    }
)


def discovery_scan_binding_for_platform(
    platform: str,
) -> DiscoveryScanStreamBinding:
    normalized = str(platform or "").strip().casefold()
    try:
        return DISCOVERY_SCAN_BINDINGS_BY_PLATFORM[normalized]
    except KeyError as exc:
        raise ValueError(
            f"discovery_scan_platform_unsupported:{normalized or 'missing'}"
        ) from exc


def discovery_scan_result_key(request_id: str, platform: str) -> str:
    normalized_request_id = str(request_id or "").strip().casefold()
    binding = discovery_scan_binding_for_platform(platform)
    return (
        f"{DISCOVERY_SCAN_RESULT_PREFIX}:"
        f"{normalized_request_id}:{binding.platform}"
    )


__all__ = (
    "DISCOVERY_SCAN_PROTOCOL_VERSION",
    "DISCOVERY_SCAN_STREAM_BINDINGS",
    "DiscoveryScanStreamBinding",
    "discovery_scan_binding_for_platform",
    "discovery_scan_result_key",
)
