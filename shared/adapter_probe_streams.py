"""Redis transport topology for read-only adapter probes.

The platform modules own probe behavior.  This module owns only the durable
transport contract: one stream/consumer-group pair per platform and the
historical shared stream used during a bounded compatibility drain.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType

from shared.platform_ids import PLATFORM_IDS


LEGACY_ADAPTER_PROBE_STREAM_KEY = "adapter_probe_requests"
LEGACY_ADAPTER_PROBE_GROUP_NAME = "adapter-probers"
LEGACY_ADAPTER_PROBE_FANOUT_GROUP_NAME = "adapter-probers:legacy-fanout"
LEGACY_ADAPTER_PROBE_FANOUT_CONSUMER_NAME = "adapter-prober-legacy-fanout"

ADAPTER_PROBE_STREAM_KEYS = MappingProxyType(
    {
        platform: f"{LEGACY_ADAPTER_PROBE_STREAM_KEY}:{platform}"
        for platform in PLATFORM_IDS
    }
)
ADAPTER_PROBE_GROUP_NAMES = MappingProxyType(
    {
        platform: f"{LEGACY_ADAPTER_PROBE_GROUP_NAME}:{platform}"
        for platform in PLATFORM_IDS
    }
)

# The platform streams are a versionless but exact durable protocol.  Keeping
# the field set in shared transport code prevents Core from committing an
# envelope that Worker can only retain forever as unverifiable authority.
ADAPTER_PROBE_STREAM_FIELDS = frozenset(
    {
        "probe_id",
        "platform",
        "account_id",
        "lottery_id",
        "target_url",
        "canonical_url",
        "execution_path_id",
        "target_hash",
        "rule_snapshot_id",
        "rule_hash",
        "action_plan_hash",
        "config_hash",
        "execution_revision",
        "account_lease_id",
        "account_lease_generation",
    }
)


@dataclass(frozen=True)
class AdapterProbeStreamBinding:
    stream_key: str
    group_name: str
    platform: str | None
    legacy: bool = False


PLATFORM_ADAPTER_PROBE_STREAM_BINDINGS = tuple(
    AdapterProbeStreamBinding(
        stream_key=ADAPTER_PROBE_STREAM_KEYS[platform],
        group_name=ADAPTER_PROBE_GROUP_NAMES[platform],
        platform=platform,
    )
    for platform in PLATFORM_IDS
)
LEGACY_ADAPTER_PROBE_STREAM_BINDING = AdapterProbeStreamBinding(
    stream_key=LEGACY_ADAPTER_PROBE_STREAM_KEY,
    group_name=LEGACY_ADAPTER_PROBE_GROUP_NAME,
    platform=None,
    legacy=True,
)
_BINDINGS_BY_STREAM = MappingProxyType(
    {
        binding.stream_key: binding
        for binding in (
            *PLATFORM_ADAPTER_PROBE_STREAM_BINDINGS,
            LEGACY_ADAPTER_PROBE_STREAM_BINDING,
        )
    }
)


def adapter_probe_stream_binding_for_platform(
    platform: str,
) -> AdapterProbeStreamBinding:
    normalized = str(platform or "").strip().casefold()
    for binding in PLATFORM_ADAPTER_PROBE_STREAM_BINDINGS:
        if binding.platform == normalized:
            return binding
    raise ValueError(
        f"adapter_probe_stream_platform_unsupported:{normalized or 'missing'}"
    )


def adapter_probe_stream_for_platform(platform: str) -> str:
    return adapter_probe_stream_binding_for_platform(platform).stream_key


def adapter_probe_stream_binding_for_key(
    stream_key: str,
) -> AdapterProbeStreamBinding | None:
    return _BINDINGS_BY_STREAM.get(str(stream_key or "").strip())


def is_adapter_probe_stream(stream_key: str) -> bool:
    return adapter_probe_stream_binding_for_key(stream_key) is not None


def adapter_probe_stream_bindings(
    *,
    include_legacy: bool,
) -> tuple[AdapterProbeStreamBinding, ...]:
    if include_legacy:
        return (
            *PLATFORM_ADAPTER_PROBE_STREAM_BINDINGS,
            LEGACY_ADAPTER_PROBE_STREAM_BINDING,
        )
    return PLATFORM_ADAPTER_PROBE_STREAM_BINDINGS


def validate_adapter_probe_stream_message(
    binding: AdapterProbeStreamBinding,
    message: dict,
) -> None:
    """Fail closed when an envelope does not belong to its transport lane."""

    if not isinstance(message, dict):
        raise ValueError("adapter_probe_stream_message_invalid")
    platform = str(message.get("platform") or "").strip().casefold()
    probe_id = str(message.get("probe_id") or "").strip()
    if not platform or not probe_id:
        raise ValueError("adapter_probe_stream_message_invalid")
    expected = adapter_probe_stream_binding_for_platform(platform)
    if not binding.legacy and expected.stream_key != binding.stream_key:
        raise ValueError("adapter_probe_stream_platform_mismatch")
    if not binding.legacy and set(message) != ADAPTER_PROBE_STREAM_FIELDS:
        raise ValueError("adapter_probe_stream_message_contract_invalid")
    if any(
        not isinstance(key, str)
        or isinstance(value, bool)
        or not isinstance(value, (str, int))
        for key, value in message.items()
    ):
        raise ValueError("adapter_probe_stream_message_invalid")


__all__ = (
    "ADAPTER_PROBE_GROUP_NAMES",
    "ADAPTER_PROBE_STREAM_FIELDS",
    "ADAPTER_PROBE_STREAM_KEYS",
    "AdapterProbeStreamBinding",
    "LEGACY_ADAPTER_PROBE_FANOUT_CONSUMER_NAME",
    "LEGACY_ADAPTER_PROBE_FANOUT_GROUP_NAME",
    "LEGACY_ADAPTER_PROBE_GROUP_NAME",
    "LEGACY_ADAPTER_PROBE_STREAM_BINDING",
    "LEGACY_ADAPTER_PROBE_STREAM_KEY",
    "PLATFORM_ADAPTER_PROBE_STREAM_BINDINGS",
    "adapter_probe_stream_binding_for_key",
    "adapter_probe_stream_binding_for_platform",
    "adapter_probe_stream_bindings",
    "adapter_probe_stream_for_platform",
    "is_adapter_probe_stream",
    "validate_adapter_probe_stream_message",
)
