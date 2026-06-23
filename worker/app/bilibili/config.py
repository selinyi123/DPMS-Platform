"""Tunable knobs for the Bilibili API engine.

Defaults are ported from LotteryAutoScript's ``lib/data/config.js`` (the values
its operators run in production) and from the audit's stability findings. The
two changes from the reference defaults that matter for long-term safety:

* a real **hard per-run / per-day action cap** (the reference has none — its
  throughput is bounded only by delays and scan-page limits), and
* jitter applied to *every* delay, not just the main relay interval, so the
  request cadence is not a fingerprintable constant.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# A current desktop Chrome UA. The reference ships a hardcoded Chrome/87 string,
# which is itself a weak risk-control signal; keep this reasonably fresh.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Default comment pool. Bilibili rejects duplicate comments (code 12051), so the
# executor picks one at random per comment to vary the text.
DEFAULT_COMMENT_POOL = (
    "参与一下，祝我好运[doge]",
    "蹲一个，谢谢老板[打call]",
    "来了来了，希望中奖[星星眼]",
    "抽奖冲鸭，沾沾喜气[脱单doge]",
    "参与～感谢分享[tv_鼓掌]",
)


@dataclass
class BiliEngineConfig:
    # --- transport ---
    user_agent: str = DEFAULT_USER_AGENT
    timeout_seconds: float = 20.0
    #: HTTP-level retries for transient transport failures (network/5xx).
    max_http_retries: int = 3
    #: fixed wait (seconds) between HTTP-level retries.
    http_retry_wait: float = 5.0

    # --- inter-action pacing (seconds) ---
    #: base wait between the major actions (follow/like/comment/repost).
    action_wait: float = 30.0
    #: +/- fraction of jitter applied to *every* delay (0.5 == [0.5x, 1.5x]).
    jitter_fraction: float = 0.5
    #: wait between dynamic-feed pages while scanning.
    scan_page_wait: float = 2.0
    #: wait before re-trying a retryable action; grows linearly per attempt.
    action_retry_base_wait: float = 60.0
    #: max attempts for a single retryable action (the reference uses 5-6).
    action_max_attempts: int = 4

    # --- discovery bounds ---
    #: pages of a UP's space feed to scan per run (~12 dynamics/page).
    scan_pages: int = 3

    # --- safety caps (the reference lacks these) ---
    #: hard ceiling on state-changing actions performed in one participate()
    #: call; a backstop against a runaway loop on a single target.
    max_actions_per_target: int = 6

    # --- lottery filtering ---
    min_follower: int = 1000
    #: a lottery dynamic must contain at least one of these to be acted on.
    require_keywords: tuple[str, ...] = ("抽奖", "福利", "转发", "关注", "评论", "参与")
    #: dynamics containing any of these are treated as scam/honeypot and skipped.
    block_keywords: tuple[str, ...] = ("脚本", "抽奖号", "钓鱼", "测试")
    #: skip lotteries whose draw time is more than this many days out / already past.
    max_days_ahead: int = 60

    # --- repost / comment content ---
    repost_text: str = "转发动态"
    comment_pool: tuple[str, ...] = DEFAULT_COMMENT_POOL

    # --- blacklist ---
    blocked_uids: frozenset[int] = field(default_factory=frozenset)
