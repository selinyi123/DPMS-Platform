from dataclasses import dataclass
import re
from urllib.parse import urlparse


@dataclass(frozen=True)
class LotteryTargetValidation:
    valid: bool
    kind: str | None = None
    reason: str | None = None


def validate_lottery_target(platform: str, raw_url: str) -> LotteryTargetValidation:
    parsed = urlparse((raw_url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return LotteryTargetValidation(False, reason="invalid_url")
    if platform != "bilibili":
        return LotteryTargetValidation(True, kind="generic")
    return validate_bilibili_target(parsed)


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
