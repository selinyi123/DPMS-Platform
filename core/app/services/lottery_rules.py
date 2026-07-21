import re
from typing import Any


BILIBILI_COMBINED_ACTION_PATTERN = (
    r"(?:"
    r"转[\s+、，,/]*评[\s+、，,/]*赞|"
    r"转[\s+、，,/]*赞[\s+、，,/]*评|"
    r"评[\s+、，,/]*转[\s+、，,/]*赞|"
    r"评[\s+、，,/]*赞[\s+、，,/]*转|"
    r"赞[\s+、，,/]*转[\s+、，,/]*评|"
    r"赞[\s+、，,/]*评[\s+、，,/]*转"
    r")"
)

BILIBILI_ACTION_PATTERNS = {
    "followed": (r"关注(?:我|本账号|本账户|UP主|up主|主播|@[\w\u4e00-\u9fff_-]+)?",),
    "liked": (r"点赞", r"点个赞", BILIBILI_COMBINED_ACTION_PATTERN, r"\b赞\b"),
    "commented": (r"评论", r"留言", BILIBILI_COMBINED_ACTION_PATTERN),
    "reposted": (r"转发", r"分享动态", BILIBILI_COMBINED_ACTION_PATTERN),
}
BILIBILI_LOTTERY_PATTERNS = (
    r"抽奖",
    r"抽取",
    r"开奖",
    r"福利",
    r"奖品",
    r"送出",
    r"中奖",
    r"赢(?:取|得)?[^，。！？\r\n]{0,30}(?:奖品|好礼|礼物|礼包|键盘|鼠标|周边|实物|资格|名额|券|京东[eE]?卡|购物卡|礼品卡|充值卡|会员卡|电话卡|流量卡|游戏点卡|点卡)",
    r"评论区抽",
    r"抽[\d一二三四五六七八九十]+[位名]",
)
BILIBILI_AMBIGUOUS_PATTERNS = (
    r"无需(?:关注|点赞|评论|留言|转发|分享)",
    r"不用(?:关注|点赞|评论|留言|转发|分享)",
    r"禁止(?:关注|点赞|评论|留言|转发|分享)",
    r"可选",
    r"任选",
)

# Weibo lotteries often add a friend-mention requirement. The adapters cannot
# fulfill that safely, so it is surfaced through unsupported_actions.
WEIBO_ACTION_PATTERNS = {
    "followed": (r"关注(?:我|本账号|本账户|博主|主播)?",),
    "liked": (r"点赞", r"点个赞", r"\b赞\b"),
    "commented": (r"评论", r"留言"),
    "reposted": (r"转发", r"转发(?:本条|这条)?微博", r"转起"),
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

# Xiaohongshu's participation contract uses collection/favorite as the fourth
# action. Sharing/reposting is a different side effect and must never be treated
# as a substitute for collection.
XIAOHONGSHU_ACTION_PATTERNS = {
    "followed": (r"关注(?:我|本账号|本账户|博主|up主)?", r"(?:一键)?四连"),
    "liked": (r"点赞", r"双击点赞", r"(?:一键)?四连"),
    "commented": (r"评论", r"留言", r"评论区", r"(?:一键)?四连"),
    "favorited": (r"收藏", r"(?:一键)?四连"),
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
    "followed": (r"关注(?:我|本账号|本账户|up主)?",),
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

# Selector adapters can only post their configured fixed text and click the
# configured interaction controls. Content-bearing requirements must therefore
# fail closed on every browser-backed platform, not just Bilibili.
COMMON_CONTENT_UNSUPPORTED_ACTION_PATTERNS: dict[str, tuple[str, ...]] = {
    "topic_tag": (
        r"(?:带上?|添加|使用)(?:指定)?话题(?:\s*#[^#\r\n]{1,64}#)?",
        r"(?:评论|留言|转发|分享|投稿|发布|文案|内容)[^。；;！？!?\r\n]{0,20}(?:带上?|添加|使用|包含)[^。；;！？!?\r\n]{0,8}(?:#[^#\r\n]{1,64}#|话题)",
        r"(?:评论|留言)(?:区)?\s*#[^#\r\n]{1,64}#",
    ),
    "mention_account": (
        r"(?:并|且|同时|转发|分享|文案|内容|带上?|添加|标注)\s*@(?:[\w\u4e00-\u9fff-]{1,64}|[\d一二三四五六七八九十两]+(?:位|个)?(?:好友|朋友))",
        r"(?:评论|留言)(?:区)?[^。；;！？!?\r\n]{0,8}@[\w\u4e00-\u9fff-]{1,64}",
        r"(?:邀请|艾特|提及)[^。；;！？!?\r\n]{0,12}(?:好友|朋友|账号|博主|用户)",
    ),
    "media_submission": (
        r"(?:晒出?|上传|提交|附上|带图|图评)[^。；;！？!?\r\n]{0,30}(?:视频|照片|图片|图文|截图)",
        r"(?:请|需|需要|必须|另行|另|自行)(?:发布|发)[^。；;！？!?\r\n]{0,24}(?:视频|照片|图片|图文|截图)",
        r"(?:视频|照片|图片|图文|截图)[^。；;！？!?\r\n]{0,12}(?:投稿|提交|参赛)",
    ),
    "translation_required": (
        r"(?:视频|照片|图片|图文|文案|内容|作品)[^，。！？；;\r\n]{0,16}(?:\+|并|及|和|附|带|配)[^，。！？；;\r\n]{0,6}(?:翻译|译文)",
        r"(?:附上?|添加|提供|配上?|带上?)(?:对应)?的?(?:翻译|译文)",
    ),
    "comment_content": (
        r"(?:评论|留言)(?:区)?[^。；;！？!?\r\n]{0,16}(?:指定文案|指定内容|关键词|口令|回答|回复问题|说出|写下|留下|告诉我们|聊聊|填写|内容为|带上?|包含)",
        r"(?:评论|留言)(?:区)?(?:里|中)?\s*(?:你|您|自己|所在|打出|输入|写(?:下|上)?|说(?:出|说)?|回复|回答|告诉|聊聊|填写|留下|[\"'“‘「『]|\d{2,}|[A-Za-z]{2,})",
        r"(?:评论|留言)(?:区)?[^。；;！？!?\r\n]{0,8}分享(?:你|您|自己|一段|一个|最|对|关于)",
        r"(?:评论|留言)(?:区)?\s*[\"'“‘「『][^\"'”’」』\r\n]{1,80}[\"'”’」』]",
        r"(?:说说|说出|写下|分享|回答|回复|告诉我们|聊聊)[^。；;！？!?\r\n]{1,80}(?:，|,)?\s*(?:在)?(?:评论|留言)(?:区)?",
        r"(?:你|大家|各位)[^。；;！？!?\r\n]{1,80}[？?]\s*(?:在)?(?:评论|留言)(?:区)?",
    ),
    "repost_content": (
        r"(?:转发|分享)(?:时|后|并)?[^。；;！？!?\r\n]{0,16}(?:指定文案|指定内容|文案|关键词|口令|写上?|附上?|带上?|添加|包含)",
    ),
    "separate_post": (
        r"(?:请|需|需要|必须|通过|另|另行|自行)(?:发布|发|投稿)",
        r"(?:投稿|另(?:行)?发(?:布)?(?:动态|视频|图文|帖子|作品)?)[^。；;！？!?\r\n]{0,16}(?:参与|抽奖|活动|赢)",
        r"(?:参与|抽奖|活动)[^。；;！？!?\r\n]{0,16}(?:投稿|另(?:行)?发(?:布)?(?:动态|视频|图文|帖子|作品)?)",
    ),
}

# Actions that the rule text asks for but no adapter phase can perform. These
# never enter `required_actions`; they force `review_required` so an operator
# decides how to handle the gap.
PLATFORM_UNSUPPORTED_ACTION_PATTERNS: dict[str, dict[str, tuple[str, ...]]] = {
    "bilibili": {
        "topic_tag": (
            r"(?:带上?|添加|使用)(?:指定)?话题(?:\s*#[^#\r\n]{1,64}#)?",
            r"(?:评论|留言|转发|分享|投稿|发布|文案|内容)[^。；;！？!?\r\n]{0,20}(?:带上?|添加|使用|包含)[^。；;！？!?\r\n]{0,8}(?:#[^#\r\n]{1,64}#|话题)",
            r"(?:评论|留言)(?:区)?\s*#[^#\r\n]{1,64}#",
        ),
        "mention_account": (
            r"(?:并|且|同时|转发|分享|文案|内容|带上?|添加|标注)\s*@[\w\u4e00-\u9fff-]{1,64}",
            r"(?:评论|留言)(?:区)?[^。；;！？!?\r\n]{0,8}@[\w\u4e00-\u9fff-]{1,64}",
            r"@[\d一二三四五六七八九十两]+(?:位|个)?(?:好友|朋友)",
            r"(?:邀请|艾特|提及)[^。；;！？!?\r\n]{0,12}(?:好友|朋友)",
            r"(?:艾特|提及)(?:指定)?(?:账号|UP主|up主|用户)",
        ),
        "media_submission": (
            r"(?:晒出?|上传|提交|附上|带图|图评)[^。；;！？!?\r\n]{0,30}(?:视频|照片|图片|图文|截图)",
            r"(?:请|需|需要|必须|另行|另|自行)(?:发布|发)[^。；;！？!?\r\n]{0,24}(?:视频|照片|图片|图文|截图)",
            r"(?:视频|照片|图片|图文|截图)[^。；;！？!?\r\n]{0,12}(?:投稿|提交|参赛)",
        ),
        "translation_required": (
            r"(?:视频|照片|图片|图文|文案|内容|作品)[^，。！？；;\r\n]{0,16}(?:\+|并|及|和|附|带|配)[^，。！？；;\r\n]{0,6}(?:翻译|译文)",
            r"(?:附上?|添加|提供|配上?|带上?)(?:对应)?的?(?:翻译|译文)",
        ),
        "comment_content": (
            r"(?:评论|留言)(?:区)?[^。；;！？!?\r\n]{0,16}(?:指定文案|指定内容|关键词|口令|回答|回复问题|说出|写下|留下|告诉我们|聊聊|填写|内容为|带上?|包含)",
            r"(?:评论|留言)(?:区)?(?:里|中)?\s*(?:你|您|自己|所在|打出|输入|写(?:下|上)?|说(?:出|说)?|回复|回答|告诉|聊聊|填写|留下|[\"'“‘「『]|\d{2,}|[A-Za-z]{2,})",
            r"(?:评论|留言)(?:区)?[^。；;！？!?\r\n]{0,8}分享(?:你|您|自己|一段|一个|最|对|关于)",
            r"(?:评论|留言)(?:区)?\s*[\"'“‘「『][^\"'”’」』\r\n]{1,80}[\"'”’」』]",
            r"(?:说说|说出|写下|分享|回答|回复|告诉我们|聊聊)[^。；;！？!?\r\n]{1,80}(?:，|,)?\s*(?:在)?(?:评论|留言)(?:区)?",
            r"(?:你|大家|各位)[^。；;！？!?\r\n]{1,80}[？?]\s*(?:在)?(?:评论|留言)(?:区)?",
        ),
        "repost_content": (
            r"(?:转发|分享)(?:时|后|并)?[^。；;！？!?\r\n]{0,16}(?:指定文案|指定内容|文案|关键词|口令|写上?|附上?|带上?|添加|包含)",
        ),
        "coined": (
            r"投币",
            r"(?:一键)?三连",
        ),
        "favorited": (
            r"(?:点击|点一下|记得|需要|请|并|及|和|、|\+)\s*收藏",
            r"收藏(?:本|该|这)(?:动态|视频|作品)",
            r"(?:一键)?三连",
        ),
        "separate_post": (
            r"(?:请|需|需要|必须|通过|另|另行|自行)(?:发布|发|投稿)",
            r"(?:投稿|另(?:行)?发(?:布)?(?:动态|视频|图文|帖子|作品)?)[^。；;！？!?\r\n]{0,16}(?:参与|抽奖|活动|赢)",
            r"(?:参与|抽奖|活动)[^。；;！？!?\r\n]{0,16}(?:投稿|另(?:行)?发(?:布)?(?:动态|视频|图文|帖子|作品)?)",
        ),
        "multiple_prize_branches": (
            r"(?:两种|三种|多种)(?:参与|抽奖|获奖)?(?:方式|方法|路径)",
            r"(?:参与)?(?:方式|方法)\s*[Aa一1①][：:\s]?[^。\r\n]{0,120}(?:参与)?(?:方式|方法)\s*[Bb二2②]",
            r"(?:第一|第?1|①)(?:种)?(?:参与)?(?:方式|方法)[^。\r\n]{0,120}(?:第二|第?2|②)(?:种)?(?:参与)?(?:方式|方法)",
        ),
    },
    "weibo": {
        **COMMON_CONTENT_UNSUPPORTED_ACTION_PATTERNS,
        "mention_friends": (
            r"@[\d一二三四五六七八九十]*个?(?:好友|朋友)",
            r"艾特.*?(?:好友|朋友)",
        ),
    },
    "xiaohongshu": {
        **COMMON_CONTENT_UNSUPPORTED_ACTION_PATTERNS,
        # Sharing/reposting is outside the strict four-action contract. Keep it
        # unresolved so an operator cannot exchange it for ``favorited``.
        "reposted": (r"分享", r"转发"),
    },
    "douyin": {
        **COMMON_CONTENT_UNSUPPORTED_ACTION_PATTERNS,
    },
}

MOJIBAKE_MARKERS = ("Ã", "Â", "â", "ä", "å", "æ", "ç", "è", "é", "ï")

EXACT_TOPIC_TAG_PATTERN = re.compile(r"#[^#\r\n]{1,64}#")
EXACT_MENTION_PATTERN = re.compile(r"@[\w\u4e00-\u9fff-]{1,64}")
GENERIC_FRIEND_MENTION_PREFIX_PATTERN = re.compile(
    r"@[\d一二三四五六七八九十两]+(?:位|个)?(?:好友|朋友)"
)
FOLLOW_TARGET_PATTERN = re.compile(
    r"关注\s*(?P<handle>@[\w\u4e00-\u9fff-]{1,64})",
    re.IGNORECASE,
)
FOLLOW_TARGET_ACTION_SUFFIX_PATTERN = re.compile(
    r"(?:并|且|同时|\+|＋)(?:转评赞|转赞评|评转赞|评赞转|赞转评|赞评转|转发|分享|评论|留言|点赞|点个赞)"
    r"(?:本条|该条|这条)?(?:动态|内容|视频|微博|笔记)?$",
    re.IGNORECASE,
)
COMMENT_CONTEXT_PATTERN = re.compile(r"(?:评论|留言)(?:区)?", re.IGNORECASE)
REPOST_CONTEXT_PATTERN = re.compile(r"(?:转发|分享)(?:动态)?", re.IGNORECASE)


def _empty_content_requirements() -> dict[str, Any]:
    return {
        "follow_targets": [],
        "commented": {"topic_tags": [], "mentions": []},
        "reposted": {"topic_tags": [], "mentions": []},
    }


def _token_action_scope(normalized_rule: str, start: int, end: int) -> str:
    """Bind a source token to the action whose exact text must contain it.

    Explicit local wording wins.  A bare topic/@ instruction in a lottery that
    contains a comment action is treated as comment content, which matches the
    Bilibili ``转评赞`` convention.  We never copy one token into both actions:
    doing so would silently invent a stricter rule than the source text.
    """

    before = normalized_rule[max(0, start - 32) : start]
    after = normalized_rule[end : min(len(normalized_rule), end + 32)]
    nearby = f"{before}\0{after}"
    comment_hits = list(COMMENT_CONTEXT_PATTERN.finditer(nearby))
    repost_hits = list(REPOST_CONTEXT_PATTERN.finditer(nearby))
    if comment_hits and not repost_hits:
        return "commented"
    if repost_hits and not comment_hits:
        return "reposted"

    # When both action words occur, prefer an explicit instruction immediately
    # before the token.  Otherwise Bilibili topic/@ participation tokens belong
    # to the comment payload; repost-only wording is handled above.
    direct_before = normalized_rule[max(0, start - 18) : start]
    last_comment = max(
        (match.start() for match in COMMENT_CONTEXT_PATTERN.finditer(direct_before)),
        default=-1,
    )
    last_repost = max(
        (match.start() for match in REPOST_CONTEXT_PATTERN.finditer(direct_before)),
        default=-1,
    )
    return "reposted" if last_repost > last_comment else "commented"


def extract_content_requirements(
    normalized_rule: str,
    unsupported_actions: list[str],
) -> dict[str, Any]:
    """Extract exact source tokens that an reviewed payload must preserve.

    Requirement *classes* such as ``topic_tag`` are not sufficient evidence:
    an arbitrary tag or account must never satisfy a rule that names a
    concrete one.  Mentions used solely as the account to follow are excluded
    from the comment/repost content requirements.  Generic friend placeholders
    are deliberately not converted into exact account identities, leaving the
    requirement unresolved and therefore fail-closed.
    """

    requirements = _empty_content_requirements()
    unsupported = set(unsupported_actions)
    follow_targets: list[str] = []
    for match in FOLLOW_TARGET_PATTERN.finditer(normalized_rule):
        handle = match.group("handle")
        suffix = FOLLOW_TARGET_ACTION_SUFFIX_PATTERN.search(handle)
        if suffix and suffix.start() > 1:
            handle = handle[: suffix.start()]
        if EXACT_MENTION_PATTERN.fullmatch(handle) and handle not in follow_targets:
            follow_targets.append(handle)
    requirements["follow_targets"] = follow_targets
    if "topic_tag" in unsupported:
        for match in EXACT_TOPIC_TAG_PATTERN.finditer(normalized_rule):
            action = _token_action_scope(normalized_rule, match.start(), match.end())
            bucket = requirements[action]["topic_tags"]
            token = match.group(0)
            if token not in bucket:
                bucket.append(token)
    if "mention_account" in unsupported:
        for match in EXACT_MENTION_PATTERN.finditer(normalized_rule):
            token = match.group(0)
            suffix = FOLLOW_TARGET_ACTION_SUFFIX_PATTERN.search(token)
            if suffix and suffix.start() > 1:
                token = token[: suffix.start()]
            prefix = normalized_rule[max(0, match.start() - 8) : match.start()]
            if re.search(r"关注\s*$", prefix, re.IGNORECASE):
                continue
            # The username regex is deliberately broad and can greedily absorb
            # trailing Chinese text (for example ``@两位好友并转发``).  Detect
            # the generic placeholder at the original match position instead
            # of relying on a fullmatch against the greedy token.
            generic = GENERIC_FRIEND_MENTION_PREFIX_PATTERN.match(
                normalized_rule, match.start()
            )
            if generic is not None:
                continue
            action = _token_action_scope(normalized_rule, match.start(), match.end())
            bucket = requirements[action]["mentions"]
            if token not in bucket:
                bucket.append(token)
    return requirements


def parse_lottery_rule(text: str, platform: str = "bilibili") -> dict[str, Any]:
    normalized = normalize_text(text)
    platform_key = str(platform or "").strip().lower()
    supported_platform = platform_key in PLATFORM_ACTION_PATTERNS
    action_patterns = PLATFORM_ACTION_PATTERNS.get(platform_key, BILIBILI_ACTION_PATTERNS)
    lottery_patterns = PLATFORM_LOTTERY_PATTERNS.get(platform_key, BILIBILI_LOTTERY_PATTERNS)
    ambiguous_patterns = PLATFORM_AMBIGUOUS_PATTERNS.get(platform_key, BILIBILI_AMBIGUOUS_PATTERNS)
    # Keep the legacy Bilibili fallback for action discovery, but apply the
    # same content-requirement checks.  Previously a case variant such as
    # ``BILIBILI`` (or an unknown value already present in legacy data) could
    # recognize the four click actions while silently dropping topic, mention,
    # media, or exact-content blockers.
    unsupported_action_patterns = PLATFORM_UNSUPPORTED_ACTION_PATTERNS.get(
        platform_key,
        PLATFORM_UNSUPPORTED_ACTION_PATTERNS["bilibili"],
    )

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
    content_requirements = extract_content_requirements(
        normalized,
        unsupported_actions,
    )

    is_lottery = bool(lottery_matches)
    review_required = (
        not supported_platform
        or not is_lottery
        or not required_actions
        or bool(ambiguity)
        or bool(unsupported_actions)
    )
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
        "content_requirements": content_requirements,
    }


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", repair_mojibake(str(value or ""))).strip()


def repair_mojibake(value: str) -> str:
    text = str(value or "")
    if not text:
        return ""

    candidates = [text]
    if any(marker in text for marker in MOJIBAKE_MARKERS) or has_c1_controls(text):
        for encoding in ("latin1", "cp1252"):
            try:
                candidates.append(text.encode(encoding).decode("utf-8"))
            except UnicodeError:
                continue

    return max(candidates, key=text_quality_score)


def has_c1_controls(text: str) -> bool:
    return any(0x80 <= ord(ch) <= 0x9F for ch in text)


def text_quality_score(text: str) -> int:
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    markers = sum(text.count(marker) for marker in MOJIBAKE_MARKERS)
    controls = sum(1 for ch in text if 0x80 <= ord(ch) <= 0x9F)
    replacement = text.count("\ufffd") + text.count("?")
    return cjk * 4 - markers * 2 - controls * 3 - replacement * 4
