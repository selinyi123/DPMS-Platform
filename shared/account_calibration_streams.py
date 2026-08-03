"""Redis transport topology for account calibration requests.

Calibration behavior and credential handling stay inside each platform
module.  This module owns only the shared delivery contract: one independent
stream/consumer-group pair per platform plus the historical shared stream
used during a bounded compatibility drain.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
import uuid

from shared.platform_ids import PLATFORM_IDS


LEGACY_ACCOUNT_CALIBRATION_STREAM_KEY = "account_calibration_requests"
LEGACY_ACCOUNT_CALIBRATION_GROUP_NAME = "account-calibrators"
LEGACY_ACCOUNT_CALIBRATION_FANOUT_GROUP_NAME = (
    "account-calibrators:legacy-fanout"
)
LEGACY_ACCOUNT_CALIBRATION_FANOUT_CONSUMER_NAME = (
    "account-calibrator-legacy-fanout"
)
ACCOUNT_CALIBRATION_OUTBOX_DEDUP_PREFIX = "account-calibration:"

ACCOUNT_CALIBRATION_STREAM_KEYS = MappingProxyType(
    {
        platform: f"{LEGACY_ACCOUNT_CALIBRATION_STREAM_KEY}:{platform}"
        for platform in PLATFORM_IDS
    }
)
ACCOUNT_CALIBRATION_GROUP_NAMES = MappingProxyType(
    {
        platform: f"{LEGACY_ACCOUNT_CALIBRATION_GROUP_NAME}:{platform}"
        for platform in PLATFORM_IDS
    }
)

SAFE_ACCOUNT_FALLBACK_STATUSES = frozenset(
    {"cold", "login_required", "ready", "cooling", "frozen", "banned"}
)
_SENSITIVE_FIELD_FRAGMENTS = (
    "authorization",
    "cookie",
    "credential",
    "password",
    "private_key",
    "refresh_token",
    "access_token",
    "weibo_rip",
)


@dataclass(frozen=True)
class AccountCalibrationStreamBinding:
    stream_key: str
    group_name: str
    platform: str | None
    legacy: bool = False


PLATFORM_ACCOUNT_CALIBRATION_STREAM_BINDINGS = tuple(
    AccountCalibrationStreamBinding(
        stream_key=ACCOUNT_CALIBRATION_STREAM_KEYS[platform],
        group_name=ACCOUNT_CALIBRATION_GROUP_NAMES[platform],
        platform=platform,
    )
    for platform in PLATFORM_IDS
)
LEGACY_ACCOUNT_CALIBRATION_STREAM_BINDING = (
    AccountCalibrationStreamBinding(
        stream_key=LEGACY_ACCOUNT_CALIBRATION_STREAM_KEY,
        group_name=LEGACY_ACCOUNT_CALIBRATION_GROUP_NAME,
        platform=None,
        legacy=True,
    )
)
_BINDINGS_BY_STREAM = MappingProxyType(
    {
        binding.stream_key: binding
        for binding in (
            *PLATFORM_ACCOUNT_CALIBRATION_STREAM_BINDINGS,
            LEGACY_ACCOUNT_CALIBRATION_STREAM_BINDING,
        )
    }
)


def account_calibration_stream_binding_for_platform(
    platform: str,
) -> AccountCalibrationStreamBinding:
    normalized = str(platform or "").strip().casefold()
    for binding in PLATFORM_ACCOUNT_CALIBRATION_STREAM_BINDINGS:
        if binding.platform == normalized:
            return binding
    raise ValueError(
        "account_calibration_stream_platform_unsupported:"
        f"{normalized or 'missing'}"
    )


def account_calibration_stream_for_platform(platform: str) -> str:
    return account_calibration_stream_binding_for_platform(
        platform
    ).stream_key


def account_calibration_stream_binding_for_key(
    stream_key: str,
) -> AccountCalibrationStreamBinding | None:
    return _BINDINGS_BY_STREAM.get(str(stream_key or "").strip())


def is_account_calibration_stream(stream_key: str) -> bool:
    return account_calibration_stream_binding_for_key(stream_key) is not None


def account_calibration_stream_bindings(
    *,
    include_legacy: bool,
) -> tuple[AccountCalibrationStreamBinding, ...]:
    if include_legacy:
        return (
            *PLATFORM_ACCOUNT_CALIBRATION_STREAM_BINDINGS,
            LEGACY_ACCOUNT_CALIBRATION_STREAM_BINDING,
        )
    return PLATFORM_ACCOUNT_CALIBRATION_STREAM_BINDINGS


def validate_account_calibration_stream_message(
    binding: AccountCalibrationStreamBinding,
    message: dict,
) -> None:
    """Fail closed on a malformed, cross-lane, or secret-bearing envelope."""

    if not isinstance(message, dict):
        raise ValueError("account_calibration_stream_message_invalid")
    normalized_keys = tuple(str(key or "").strip().casefold() for key in message)
    if any(
        fragment in key
        for key in normalized_keys
        for fragment in _SENSITIVE_FIELD_FRAGMENTS
    ):
        raise ValueError("account_calibration_stream_secret_forbidden")

    platform = str(message.get("platform") or "").strip().casefold()
    calibration_id = str(message.get("calibration_id") or "").strip()
    account_id = str(message.get("account_id") or "").strip()
    check_url = str(message.get("check_url") or "").strip()
    try:
        parsed_id = uuid.UUID(calibration_id)
        parsed_account_id = int(account_id)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("account_calibration_stream_message_invalid") from exc
    if (
        str(parsed_id) != calibration_id.casefold()
        or parsed_account_id <= 0
        or not platform
        or not check_url
        or len(check_url) > 1024
    ):
        raise ValueError("account_calibration_stream_message_invalid")

    expected = account_calibration_stream_binding_for_platform(platform)
    if not binding.legacy and expected.stream_key != binding.stream_key:
        raise ValueError("account_calibration_stream_platform_mismatch")

    fallback = str(
        message.get("fallback_account_status") or "login_required"
    ).strip().casefold()
    if fallback not in SAFE_ACCOUNT_FALLBACK_STATUSES:
        raise ValueError("account_calibration_fallback_status_invalid")


__all__ = (
    "ACCOUNT_CALIBRATION_GROUP_NAMES",
    "ACCOUNT_CALIBRATION_OUTBOX_DEDUP_PREFIX",
    "ACCOUNT_CALIBRATION_STREAM_KEYS",
    "AccountCalibrationStreamBinding",
    "LEGACY_ACCOUNT_CALIBRATION_FANOUT_CONSUMER_NAME",
    "LEGACY_ACCOUNT_CALIBRATION_FANOUT_GROUP_NAME",
    "LEGACY_ACCOUNT_CALIBRATION_GROUP_NAME",
    "LEGACY_ACCOUNT_CALIBRATION_STREAM_BINDING",
    "LEGACY_ACCOUNT_CALIBRATION_STREAM_KEY",
    "PLATFORM_ACCOUNT_CALIBRATION_STREAM_BINDINGS",
    "SAFE_ACCOUNT_FALLBACK_STATUSES",
    "account_calibration_stream_binding_for_key",
    "account_calibration_stream_binding_for_platform",
    "account_calibration_stream_bindings",
    "account_calibration_stream_for_platform",
    "is_account_calibration_stream",
    "validate_account_calibration_stream_message",
)
