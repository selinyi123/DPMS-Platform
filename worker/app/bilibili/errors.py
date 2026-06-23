"""Bilibili API error taxonomy and business-code classification.

Re-implemented in Python from the per-action business-code handling in
LotteryAutoScript (GPL-3.0, Node.js) ``lib/net/bili.js``. Only the *behavior*
(which Bilibili business code means what, and how the engine should react) is
reproduced here — no GPL source is copied — so this module can live in a
non-GPL codebase.

The point of this module is to turn raw Bilibili ``code`` integers into an
``Outcome`` the executor can act on without re-deriving the meaning at every
call site:

* ``OK``      — action succeeded, or a harmless duplicate (already followed /
                already liked / already reserved). Treat as done.
* ``RETRY``   — transient (rate limit, "data error", "too frequent"); safe to
                retry the *same* action after a backoff.
* ``LIMIT``   — a Bilibili-imposed cap was hit (follow cap, comment frequency).
                Stop attempting this action type for now; the account is not
                necessarily compromised. Long-term operation should schedule a
                follow-cleanup pass.
* ``SKIP``    — permanent *for this target* (deleted, comments closed, source
                forbids repost, blacklisted, duplicate/sensitive comment). Move
                on to the next lottery; not an account problem.
* ``CAPTCHA`` — a captcha is required (comment code 12015). v1 has no solver, so
                this is surfaced and the action is abandoned.
* ``RISK``    — account-level risk control / abnormal (账号异常, -352). Stop the
                whole run and cool the account down.
* ``AUTH``    — not logged in / cookie invalid (-101). Stop; credentials need
                refreshing.
* ``FATAL``   — unknown/unexpected code. For multi-route actions this triggers a
                failover to the next route; otherwise it aborts the action.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Outcome(str, Enum):
    OK = "ok"
    RETRY = "retry"
    LIMIT = "limit"
    SKIP = "skip"
    CAPTCHA = "captcha"
    RISK = "risk"
    AUTH = "auth"
    FATAL = "fatal"


#: Outcomes that mean "stop the whole participation run for this account".
ABORT_OUTCOMES = frozenset({Outcome.RISK, Outcome.AUTH})


@dataclass(frozen=True)
class CodeResult:
    """The classified result of one Bilibili business ``code``."""

    code: int
    outcome: Outcome
    message: str

    @property
    def ok(self) -> bool:
        return self.outcome is Outcome.OK


class BiliApiError(Exception):
    """Raised for transport/HTTP-level failures (network, non-JSON, 4xx/5xx)."""


class BiliAuthError(BiliApiError):
    """Cookie is invalid / not logged in."""


class BiliRiskControlError(BiliApiError):
    """Bilibili risk control tripped (-352 / empty content / 账号异常)."""


# Business codes that mean the same thing regardless of which action raised
# them. Checked before the per-action table.
_GENERIC: dict[int, tuple[Outcome, str]] = {
    -101: (Outcome.AUTH, "账号未登录 / Cookie 失效"),
    -111: (Outcome.AUTH, "csrf 校验失败"),
    -352: (Outcome.RISK, "风控校验失败 (-352)"),
    -403: (Outcome.RISK, "访问权限不足 (-403, 可能缺少 wbi 签名)"),
    -412: (Outcome.LIMIT, "请求被拦截 (-412, 频率过高)"),
    -509: (Outcome.RETRY, "请求过于频繁 (-509)"),
    -404: (Outcome.SKIP, "目标不存在或已删除"),
}

# Per-action tables. Keyed by action name used by the client/executor.
_PER_ACTION: dict[str, dict[int, tuple[Outcome, str]]] = {
    "follow": {
        0: (Outcome.OK, "关注成功"),
        22014: (Outcome.OK, "已关注，无需重复"),
        22002: (Outcome.SKIP, "已被对方拉黑"),
        22003: (Outcome.SKIP, "黑名单用户无法关注"),
        22009: (Outcome.LIMIT, "关注已达上限"),
        22015: (Outcome.RISK, "账号异常"),
    },
    "unfollow": {
        0: (Outcome.OK, "取关成功"),
    },
    "like": {
        0: (Outcome.OK, "点赞成功"),
        65006: (Outcome.OK, "已赞过"),
        1000001: (Outcome.RETRY, "点赞频繁"),
        1000113: (Outcome.RETRY, "点赞异常"),
        4128014: (Outcome.RISK, "账号异常，点赞失败"),
    },
    "repost": {
        0: (Outcome.OK, "转发成功"),
        1101004: (Outcome.SKIP, "该动态不能转发分享"),
        4126117: (Outcome.SKIP, "源动态禁止转发"),
        2201116: (Outcome.RETRY, "请求数据错误，请稍后重试"),
        1101008: (Outcome.LIMIT, "操作太频繁"),
    },
    "comment": {
        0: (Outcome.OK, "评论成功"),
        12002: (Outcome.SKIP, "评论区已关闭"),
        12061: (Outcome.SKIP, "UP 主已关闭评论区"),
        12035: (Outcome.SKIP, "已被对方拉黑"),
        12053: (Outcome.SKIP, "黑名单用户无法互动"),
        12016: (Outcome.SKIP, "评论内容包含敏感信息"),
        12051: (Outcome.SKIP, "重复评论"),
        12078: (Outcome.SKIP, "需关注 UP 主 7 天以上"),
        12015: (Outcome.CAPTCHA, "需要输入验证码"),
        12073: (Outcome.RETRY, "验证码错误"),
        -404: (Outcome.SKIP, "原动态已删除"),
    },
    "reserve": {
        0: (Outcome.OK, "预约成功"),
        7604003: (Outcome.OK, "已预约"),
    },
}


def classify(action: str, code: int) -> CodeResult:
    """Map a Bilibili business ``code`` for ``action`` to a :class:`CodeResult`.

    Per-action meanings win over the generic table (e.g. ``-404`` is a skip for
    a comment target). Unknown codes are ``FATAL``.
    """
    table = _PER_ACTION.get(action, {})
    if code in table:
        outcome, message = table[code]
        return CodeResult(code, outcome, message)
    if code in _GENERIC:
        outcome, message = _GENERIC[code]
        return CodeResult(code, outcome, message)
    return CodeResult(code, Outcome.FATAL, f"未知错误 (code={code})")
