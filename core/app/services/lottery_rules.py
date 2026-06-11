import re
from typing import Any


# Bilibili keeps its original patterns (regression-safe: unchanged from the
# pre-refactor `bilibili_discovery.parse_lottery_rule`).
BILIBILI_ACTION_PATTERNS = {
    "followed": (r"关注(?:我|本账号|UP主|up主|主播)?",),
    "liked": (r"点赞", r"点个赞"),
    "commented": (r"评论", r"留言"),
    "reposted": (r"转发", r"分享动态"),
}
BILIBILI_LOTTERY_PATTERNS = (r"抽奖", r"抽送", r"开奖", r"福利", r"奖品")
BILIBILI_AMBIGUOUS_PATTERNS = (
    r"无需(?:关注|点赞|评论|留言|转发)",
    r"不用(?:关注|点赞|评论|留言|转发)",
    r"禁止(?:关注|点赞|评论|留言|转发)",
    r"可选",
    r"任选",
)

# Weibo lotteries are usually phrased as "关注+转发+评论抽奖", with an
# additional "@N位好友" mention requirement that adapters cannot fulfill
# automatically (flagged via unsupported_actions instead of required_actions).
WEIBO_ACTION_PATTERNS = {
    "followed": (r"关注(?:我|本账号|博主)?",),
    "liked": (r"点赞", r"点个赞"),
    "commented": (r"评论", r"留言"),
    "reposted": (r"转发", r"转发(?:本)?微博", r"转起"),
}
WEIBO_LOTTERY_PATTERNS = (
    r"抽奖",
    r"转发抽奖",
    r"抽取",
    r"包邮",
    r"福利",
    r"中奖",
    r"开奖",
    r"抽[\d一二三四五六七八九十]+[位名]",
)
WEIBO_AMBIGUOUS_PATTERNS = (
    r"无需(?:关注|点赞|评论|留言|转发|分享)",
    r"不用(?:关注|点赞|评论|留言|转发|分享)",
    r"禁止(?:关注|点赞|评论|留言|转发|分享)",
    r"可选",
    r"任选",
)

# Xiaohongshu notes commonly ask for "关注+点赞+收藏+评论"; "收藏" (favorite)
# has no adapter phase, so it is reported as an unsupported action instead of
# silently dropped or mismapped onto "liked".
XIAOHONGSHU_ACTION_PATTERNS = {
    "followed": (r"关注(?:我|本账号|博主|up主)?",),
    "liked": (r"点赞", r"双击点赞"),
    "commented": (r"评论", r"留言", r"评论区"),
    "reposted": (r"分享", r"转发"),
}
XIAOHONGSHU_LOTTERY_PATTERNS = (
    r"抽奖",
    r"抽取",
    r"包邮",
    r"福利",
    r"送出",
    r"中奖",
    r"开奖",
    r"评论区抽",
    r"抽[\d一二三四五六七八九十]+[位名]",
)
XIAOHONGSHU_AMBIGUOUS_PATTERNS = (
    r"无需(?:关注|点赞|评论|留言|转发|分享|收藏)",
    r"不用(?:关注|点赞|评论|留言|转发|分享|收藏)",
    r"禁止(?:关注|点赞|评论|留言|转发|分享|收藏)",
    r"可选",
    r"任选",
)

DOUYIN_ACTION_PATTERNS = {
    "followed": (r"关注(?:我|本账号|up主)?",),
    "liked": (r"点赞", r"双击点赞"),
    "commented": (r"评论", r"留言"),
    "reposted": (r"分享", r"转发"),
}
DOUYIN_LOTTERY_PATTERNS = (
    r"抽奖",
    r"抽取",
    r"福利",
    r"中奖",
    r"开奖",
    r"评论抽",
    r"抽[\d一二三四五六七八九十]+[位名]",
)
DOUYIN_AMBIGUOUS_PATTERNS = (
    r"无需(?:关注|点赞|评论|留言|转发|分享)",
    r"不用(?:关注|点赞|评论|留言|转发|分享)",
    r"禁止(?:关注|点赞|评论|留言|转发|分享)",
    r"可选",
    r"任选",
)

PLATFORM_ACTION_PATTERNS: dict[str, dict[str, tuple[str, ...]]] = {
    "bilibili": BILIBILI_ACTION_PATTERNS,
    "weibo": WEIBO_ACTION_PATTERNS,
    "xiaohongshu": XIAOHONGSHU_ACTION_PATTERNS,
    "douyin": DOUYIN_ACTION_PATTERNS,
}
PLATFORM_LOTTERY_PATTERNS: dict[str, tuple[str, ...]] = {
    "bilibili": BILIBILI_LOTTERY_PATTERNS,
    "weibo": WEIBO_LOTTERY_PATTERNS,
    "xiaohongshu": XIAOHONGSHU_LOTTERY_PATTERNS,
    "douyin": DOUYIN_LOTTERY_PATTERNS,
}
PLATFORM_AMBIGUOUS_PATTERNS: dict[str, tuple[str, ...]] = {
    "bilibili": BILIBILI_AMBIGUOUS_PATTERNS,
    "weibo": WEIBO_AMBIGUOUS_PATTERNS,
    "xiaohongshu": XIAOHONGSHU_AMBIGUOUS_PATTERNS,
    "douyin": DOUYIN_AMBIGUOUS_PATTERNS,
}

# Actions that the rule text asks for but no adapter phase can perform. These
# never enter `required_actions`; they force `review_required` so an operator
# decides how to handle the gap (e.g. manually tagging friends).
PLATFORM_UNSUPPORTED_ACTION_PATTERNS: dict[str, dict[str, tuple[str, ...]]] = {
    "weibo": {
        "mention_friends": (
            r"@[\d一二三四五六七八九十]*(?:位|个)?(?:好友|朋友)",
            r"艾特.*?(?:好友|朋友)",
        ),
    },
    "xiaohongshu": {
        "favorited": (r"收藏",),
    },
}


def parse_lottery_rule(text: str, platform: str = "bilibili") -> dict[str, Any]:
    normalized = normalize_text(text)
    action_patterns = PLATFORM_ACTION_PATTERNS.get(platform, BILIBILI_ACTION_PATTERNS)
    lottery_patterns = PLATFORM_LOTTERY_PATTERNS.get(platform, BILIBILI_LOTTERY_PATTERNS)
    ambiguous_patterns = PLATFORM_AMBIGUOUS_PATTERNS.get(platform, BILIBILI_AMBIGUOUS_PATTERNS)
    unsupported_action_patterns = PLATFORM_UNSUPPORTED_ACTION_PATTERNS.get(platform, {})

    matched_rules = []
    required_actions = []
    for action, patterns in action_patterns.items():
        matched = [pattern for pattern in patterns if re.search(pattern, normalized, re.IGNORECASE)]
        if matched:
            required_actions.append(action)
            matched_rules.append({"action": action, "patterns": matched})

    lottery_matches = [pattern for pattern in lottery_patterns if re.search(pattern, normalized, re.IGNORECASE)]
    ambiguity = [pattern for pattern in ambiguous_patterns if re.search(pattern, normalized, re.IGNORECASE)]
    unsupported_actions = [
        action
        for action, patterns in unsupported_action_patterns.items()
        if any(re.search(pattern, normalized, re.IGNORECASE) for pattern in patterns)
    ]

    is_lottery = bool(lottery_matches)
    review_required = not is_lottery or not required_actions or bool(ambiguity) or bool(unsupported_actions)
    confidence = 0.15
    if is_lottery:
        confidence += 0.45
    confidence += min(len(required_actions) * 0.1, 0.3)
    if ambiguity:
        confidence -= 0.25
    if unsupported_actions:
        confidence -= 0.15

    return {
        "version": 1,
        "platform": platform,
        "is_lottery": is_lottery,
        "required_actions": required_actions,
        "review_required": review_required,
        "confidence": round(max(0.0, min(confidence, 1.0)), 2),
        "lottery_patterns": lottery_matches,
        "matched_rules": matched_rules,
        "ambiguity_patterns": ambiguity,
        "unsupported_actions": unsupported_actions,
    }


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()
