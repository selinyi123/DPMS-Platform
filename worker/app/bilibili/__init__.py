"""Bilibili direct-API lottery engine.

A Python re-implementation of LotteryAutoScript's HTTP-API approach to Bilibili
动态抽奖 participation, built to run unattended and long-term inside the DPMS
worker. Uses Bilibili's documented web endpoints directly (with wbi signing the
reference lacks) rather than browser automation.

Public surface:
    BilibiliApiClient   - async HTTP client for the Bilibili web API
    BilibiliApiExecutor - orchestrates follow/like/repost/comment for one lottery
    BiliEngineConfig    - tunable pacing / filtering / safety knobs
    parse_dynamic_card / parse_feed / DynamicCard - feed JSON -> structured card
    classify / Outcome / CodeResult - business-code taxonomy
"""

from .client import BilibiliApiClient, parse_cookie
from .config import BiliEngineConfig
from .errors import CodeResult, Outcome, classify
from .executor import BilibiliApiExecutor, ExecutionResult
from .parser import DynamicCard, looks_like_lottery, parse_dynamic_card, parse_feed

__all__ = [
    "BilibiliApiClient",
    "parse_cookie",
    "BiliEngineConfig",
    "CodeResult",
    "Outcome",
    "classify",
    "BilibiliApiExecutor",
    "ExecutionResult",
    "DynamicCard",
    "looks_like_lottery",
    "parse_dynamic_card",
    "parse_feed",
]
