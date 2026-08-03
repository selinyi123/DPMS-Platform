"""Validated runtime scope shared by Core and Worker deployment units."""

from __future__ import annotations

from collections.abc import Iterable

from shared.platform_ids import PLATFORM_IDS


ALL_PLATFORM_SCOPE = "all"
PLATFORM_ID_SET = frozenset(PLATFORM_IDS)


class PlatformScopeError(ValueError):
    """A deployment requested an unknown or unsafe platform scope."""


def normalize_platform_scope(
    value: str | Iterable[str] | None,
    *,
    allow_all: bool = True,
) -> tuple[str, ...]:
    """Return an ordered, duplicate-free platform tuple.

    Environment deployments normally use one exact platform or ``all``.
    Iterable support keeps internal loop APIs straightforward and testable.
    Unknown/empty values fail closed instead of silently starting every lane.
    """

    if value is None:
        raw_values = (ALL_PLATFORM_SCOPE,)
    elif isinstance(value, str):
        raw_values = tuple(
            item.strip().casefold()
            for item in value.split(",")
            if item.strip()
        )
    else:
        raw_values = tuple(
            str(item or "").strip().casefold()
            for item in value
            if str(item or "").strip()
        )

    if not raw_values:
        raise PlatformScopeError("platform_scope_required")
    if ALL_PLATFORM_SCOPE in raw_values:
        if not allow_all or len(raw_values) != 1:
            raise PlatformScopeError("platform_scope_all_must_be_exclusive")
        return PLATFORM_IDS

    unknown = sorted(set(raw_values) - PLATFORM_ID_SET)
    if unknown:
        raise PlatformScopeError(
            "platform_scope_unsupported:" + ",".join(unknown)
        )
    selected = set(raw_values)
    return tuple(platform for platform in PLATFORM_IDS if platform in selected)


def exact_platform_scope(value: str | Iterable[str] | None) -> str:
    """Require exactly one platform for an isolated business runner."""

    platforms = normalize_platform_scope(value, allow_all=False)
    if len(platforms) != 1:
        raise PlatformScopeError("platform_scope_exactly_one_required")
    return platforms[0]


__all__ = (
    "ALL_PLATFORM_SCOPE",
    "PLATFORM_ID_SET",
    "PlatformScopeError",
    "exact_platform_scope",
    "normalize_platform_scope",
)
