import re
from urllib.parse import urlparse

from app.platform_modules import (
    LotteryTargetValidation,
    get_platform_module,
    require_platform_module,
)
from app.platform_modules.catalog import (
    WEIBO_MBLOGID_PATTERN,
    WEIBO_MID_MAX,
    WEIBO_MID_PATTERN,
    XIAOHONGSHU_NOTE_PATTERN,
    is_weibo_status_id,
)

# Compatibility exports remain in this module for existing callers.  The
# authoritative patterns and validators now live with their platform module.


def validate_bilibili_target(parsed, host: str) -> LotteryTargetValidation:
    return require_platform_module("bilibili").validate_parsed_target(
        parsed, host
    )


def validate_weibo_target(parsed, host: str) -> LotteryTargetValidation:
    return require_platform_module("weibo").validate_parsed_target(parsed, host)


def validate_xiaohongshu_target(parsed, host: str) -> LotteryTargetValidation:
    return require_platform_module("xiaohongshu").validate_parsed_target(
        parsed, host
    )


def validate_douyin_target(parsed, host: str) -> LotteryTargetValidation:
    return require_platform_module("douyin").validate_parsed_target(parsed, host)


def validate_lottery_target(platform: str, raw_url: str) -> LotteryTargetValidation:
    platform_key = str(platform or "").strip().lower()
    try:
        parsed = urlparse((raw_url or "").strip())
    except ValueError:
        return LotteryTargetValidation(False, reason="invalid_url")
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return LotteryTargetValidation(False, reason="invalid_url")
    host = validated_url_host(parsed)
    if host is None:
        return LotteryTargetValidation(False, reason="invalid_url")

    platform_module = get_platform_module(platform_key)
    if platform_module is None:
        return LotteryTargetValidation(True, kind="generic")
    result = platform_module.validate_parsed_target(parsed, host)

    # Report HTTPS compatibility only for a URL that is otherwise an
    # actionable target on the selected platform.  Checking this after the
    # authority and target shape prevents unsafe or foreign HTTP URLs from
    # being misdiagnosed as if changing only the scheme would make them safe.
    if not result.valid:
        return result
    if parsed.scheme != "https":
        return LotteryTargetValidation(False, kind=result.kind, reason="https_required")
    return result


def validate_canonical_lottery_target(
    platform: str,
    canonical_url: str | None,
) -> LotteryTargetValidation:
    """Validate an already-normalized internal target identity.

    Runtime/readiness code must not demote a successfully resolved short link
    back to ``short_link`` merely because ``raw_url`` still contains b23.tv.
    Public ingress continues to validate the original HTTPS URL; this helper is
    only for persisted ``canonical://`` identities produced by the trusted
    canonicalizer.
    """

    platform_key = str(platform or "").strip().casefold()
    try:
        parsed = urlparse(str(canonical_url or "").strip())
        port = parsed.port
        hostname = parsed.hostname
    except ValueError:
        return LotteryTargetValidation(False, reason="invalid_canonical_target")
    if (
        parsed.scheme != "canonical"
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or hostname != platform_key
        or parsed.query
        or parsed.fragment
    ):
        return LotteryTargetValidation(False, reason="invalid_canonical_target")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 2:
        return LotteryTargetValidation(False, reason="invalid_canonical_target")
    kind, resource_id = parts
    if platform_key == "bilibili":
        if kind == "dynamic" and re.fullmatch(r"(?:opus_)?[0-9]{1,20}", resource_id):
            return LotteryTargetValidation(True, kind="dynamic")
        if kind == "video" and re.fullmatch(r"BV[0-9A-Za-z]{10}", resource_id):
            return LotteryTargetValidation(True, kind="video")
        if kind == "article" and re.fullmatch(r"cv[0-9]+", resource_id):
            return LotteryTargetValidation(True, kind="article")
    elif platform_key == "xiaohongshu":
        if kind == "note" and XIAOHONGSHU_NOTE_PATTERN.fullmatch(resource_id):
            return LotteryTargetValidation(True, kind="note")
    elif platform_key == "weibo":
        if kind == "status" and is_weibo_status_id(resource_id):
            return LotteryTargetValidation(True, kind="status")
    elif platform_key == "douyin":
        if kind in {"video", "note"} and re.fullmatch(r"[0-9]{10,24}", resource_id):
            return LotteryTargetValidation(True, kind=kind)
    return LotteryTargetValidation(False, reason="invalid_canonical_target")


def validate_lottery_identity(
    platform: str,
    raw_url: str | None,
    canonical_url: str | None,
) -> LotteryTargetValidation:
    """Prefer a valid persisted canonical identity, then validate raw ingress."""

    canonical = validate_canonical_lottery_target(platform, canonical_url)
    if canonical.valid:
        return canonical
    return validate_lottery_target(platform, str(raw_url or ""))


def validated_url_host(parsed) -> str | None:
    if parsed.username is not None or parsed.password is not None:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    default_port = 443 if parsed.scheme == "https" else 80
    if port is not None and port != default_port:
        return None
    host = parsed.hostname
    if not host:
        return None
    return host.rstrip(".").lower()
