from dataclasses import dataclass
import re
from urllib.parse import urlparse


@dataclass(frozen=True)
class LotteryTargetValidation:
    valid: bool
    kind: str | None = None
    reason: str | None = None


WEIBO_MBLOGID_PATTERN = re.compile(r"(?=.*[A-Za-z])[A-Za-z0-9]{6,16}")
WEIBO_MID_PATTERN = re.compile(r"\d{13,19}")
XIAOHONGSHU_NOTE_PATTERN = re.compile(r"[0-9a-fA-F]{24}")


def validate_lottery_target(platform: str, raw_url: str) -> LotteryTargetValidation:
    platform_key = str(platform or "").strip().lower()
    parsed = urlparse((raw_url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return LotteryTargetValidation(False, reason="invalid_url")
    host = validated_url_host(parsed)
    if host is None:
        return LotteryTargetValidation(False, reason="invalid_url")

    result: LotteryTargetValidation
    if platform_key == "bilibili":
        result = validate_bilibili_target(parsed, host)
    elif platform_key == "weibo":
        result = validate_weibo_target(parsed, host)
    elif platform_key == "xiaohongshu":
        result = validate_xiaohongshu_target(parsed, host)
    elif platform_key == "douyin":
        result = validate_douyin_target(parsed, host)
    else:
        return LotteryTargetValidation(True, kind="generic")

    # Report HTTPS compatibility only for a URL that is otherwise an
    # actionable target on the selected platform.  Checking this after the
    # authority and target shape prevents unsafe or foreign HTTP URLs from
    # being misdiagnosed as if changing only the scheme would make them safe.
    if not result.valid:
        return result
    if parsed.scheme != "https":
        return LotteryTargetValidation(False, kind=result.kind, reason="https_required")
    return result


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


def validate_bilibili_target(parsed, host: str) -> LotteryTargetValidation:
    path_parts = [part for part in parsed.path.split("/") if part]

    if host == "b23.tv" and path_parts:
        return LotteryTargetValidation(True, kind="short_link")

    if host == "t.bilibili.com":
        if len(path_parts) == 1 and path_parts[0].isdigit():
            return LotteryTargetValidation(True, kind="dynamic")
        if len(path_parts) == 2 and path_parts[0] == "opus" and path_parts[1].isdigit():
            return LotteryTargetValidation(True, kind="dynamic")

    if host in {"bilibili.com", "www.bilibili.com"}:
        if (
            len(path_parts) >= 2
            and path_parts[0] == "video"
            and re.fullmatch(r"(?:BV[0-9A-Za-z]+|av\d+)", path_parts[1], re.IGNORECASE)
        ):
            return LotteryTargetValidation(True, kind="video")
        if len(path_parts) == 2 and path_parts[0] == "opus" and path_parts[1].isdigit():
            return LotteryTargetValidation(True, kind="dynamic")

    return LotteryTargetValidation(False, reason="bilibili_actionable_url_required")


def validate_weibo_target(parsed, host: str) -> LotteryTargetValidation:
    path_parts = [part for part in parsed.path.split("/") if part]

    if host == "t.cn" and path_parts:
        return LotteryTargetValidation(True, kind="short_link")

    if host == "m.weibo.cn":
        if len(path_parts) == 2 and path_parts[0] in {"status", "detail"} and is_weibo_status_id(path_parts[1]):
            return LotteryTargetValidation(True, kind="status")

    if host in {"weibo.com", "www.weibo.com"}:
        if len(path_parts) == 2 and path_parts[0] == "detail" and is_weibo_status_id(path_parts[1]):
            return LotteryTargetValidation(True, kind="status")
        if len(path_parts) == 2 and path_parts[0].isdigit() and is_weibo_status_id(path_parts[1]):
            return LotteryTargetValidation(True, kind="status")

    return LotteryTargetValidation(False, reason="weibo_actionable_url_required")


def is_weibo_status_id(value: str) -> bool:
    return bool(WEIBO_MID_PATTERN.fullmatch(value) or WEIBO_MBLOGID_PATTERN.fullmatch(value))


def validate_xiaohongshu_target(parsed, host: str) -> LotteryTargetValidation:
    path_parts = [part for part in parsed.path.split("/") if part]

    if host == "xhslink.com" and path_parts:
        return LotteryTargetValidation(True, kind="short_link")

    if host in {"xiaohongshu.com", "www.xiaohongshu.com"}:
        if len(path_parts) == 2 and path_parts[0] == "explore" and XIAOHONGSHU_NOTE_PATTERN.fullmatch(path_parts[1]):
            return LotteryTargetValidation(True, kind="note")
        if (
            len(path_parts) == 3
            and path_parts[0] == "discovery"
            and path_parts[1] == "item"
            and XIAOHONGSHU_NOTE_PATTERN.fullmatch(path_parts[2])
        ):
            return LotteryTargetValidation(True, kind="note")

    return LotteryTargetValidation(False, reason="xiaohongshu_actionable_url_required")


def validate_douyin_target(parsed, host: str) -> LotteryTargetValidation:
    path_parts = [part for part in parsed.path.split("/") if part]

    if host == "v.douyin.com" and path_parts:
        return LotteryTargetValidation(True, kind="short_link")

    if host in {"douyin.com", "www.douyin.com"}:
        if len(path_parts) == 2 and path_parts[0] == "video" and path_parts[1].isdigit():
            return LotteryTargetValidation(True, kind="video")

    if host == "www.iesdouyin.com":
        if len(path_parts) == 3 and path_parts[0] == "share" and path_parts[1] == "video" and path_parts[2].isdigit():
            return LotteryTargetValidation(True, kind="video")

    return LotteryTargetValidation(False, reason="douyin_actionable_url_required")
