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
    parsed = urlparse((raw_url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return LotteryTargetValidation(False, reason="invalid_url")
    if platform == "bilibili":
        return validate_bilibili_target(parsed)
    if platform == "weibo":
        return validate_weibo_target(parsed)
    if platform == "xiaohongshu":
        return validate_xiaohongshu_target(parsed)
    return LotteryTargetValidation(True, kind="generic")


def validate_bilibili_target(parsed) -> LotteryTargetValidation:
    host = parsed.netloc.lower().split(":", 1)[0]
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


def validate_weibo_target(parsed) -> LotteryTargetValidation:
    host = parsed.netloc.lower().split(":", 1)[0]
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


def validate_xiaohongshu_target(parsed) -> LotteryTargetValidation:
    host = parsed.netloc.lower().split(":", 1)[0]
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
