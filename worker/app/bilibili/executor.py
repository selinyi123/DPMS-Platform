"""Participate in one Bilibili lottery via the API client.

Python re-implementation of the *action* half of LotteryAutoScript's
``monitor.go()`` loop: given a parsed lottery card and the actions the lottery
requires, perform follow/like/repost/comment in a fixed order with jittered
delays, per-action retry/backoff, and conservative stops on failure.

Decoupled from the network and from randomness: it takes any object exposing
the ``follow/like/repost/comment`` coroutines (the real
:class:`~worker.app.bilibili.client.BilibiliApiClient` or a fake) and its
``sleep``/``rand`` are injectable, so sequencing, retries, stops, and even
comment selection are deterministic in tests.

Stop semantics (mirroring the reference's "return on non-OK follow"):

* RISK / AUTH on any action  -> abort the whole run; ``abort_outcome`` is set so
  the caller can cool the account down (the Playwright path's risk handling).
* a non-OK ``follow``        -> stop: without the follow the lottery can't
  qualify, and re-poking a capped/blacklisted follow endpoint is what risk
  control escalates on. A follow cap (LIMIT) additionally sets ``follow_capped``.
* FATAL on any mutation      -> stop conservatively (unknown failures may be an
  early soft-risk signal); no account cooldown.
* SKIP / LIMIT / CAPTCHA on a non-follow phase -> record and continue.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from .config import BiliEngineConfig
from .errors import ABORT_OUTCOMES, CodeResult, Outcome
from .parser import DynamicCard

# Canonical execution order; required_actions is intersected against this.
PHASE_ORDER = ("follow", "like", "repost", "comment")
# Outcomes on a *required* phase that mean retrying the lottery later is futile.
_PERMANENT = frozenset({Outcome.SKIP, Outcome.CAPTCHA})


@dataclass
class ExecutionResult:
    dynamic_id: str
    success: bool = False
    aborted: bool = False
    abort_reason: str = ""
    #: outcome that triggered an abort; set only for RISK/AUTH (account-level),
    #: so the caller can route those to account cooldown. None for pre-flight
    #: aborts (charge lottery / blacklist / unresolved target).
    abort_outcome: Outcome | None = None
    #: a follow-cap (22009) was hit — the run loop should switch this account to
    #: a cleanup / only-already-followed posture before following more UPs.
    follow_capped: bool = False
    #: set when the run stopped early without an account-level abort.
    stopped_reason: str = ""
    #: phase name -> the CodeResult that settled it
    actions: dict[str, CodeResult] = field(default_factory=dict)
    #: the phases the lottery actually required (post-intersection)
    required: list[str] = field(default_factory=list)

    @property
    def performed(self) -> list[str]:
        return [name for name, r in self.actions.items() if r.ok]

    @property
    def terminal(self) -> bool:
        """True if retrying this lottery later is pointless.

        Success, an account-level abort, a follow cap, or any required phase
        that is permanently impossible for this target (comments closed, etc.)
        all mean "do not re-lease this lottery forever".
        """
        if self.success or self.aborted or self.follow_capped:
            return True
        return any(self.actions.get(p, _MISSING).outcome in _PERMANENT for p in self.required)


_MISSING = CodeResult(0, Outcome.FATAL, "")


class BilibiliApiExecutor:
    def __init__(
        self,
        client,
        config: BiliEngineConfig | None = None,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rand: Callable[[], float] = random.random,
    ) -> None:
        self.client = client
        self.config = config or BiliEngineConfig()
        self._sleep = sleep
        self._rand = rand

    def _jittered(self, base: float) -> float:
        # [1-f, 1+f] * base, applied to every delay so cadence isn't constant.
        f = self.config.jitter_fraction
        return base * (1 - f + 2 * f * self._rand())

    async def _pace(self) -> None:
        await self._sleep(self._jittered(self.config.action_wait))

    def _pick_comment(self) -> str:
        pool = list(self.config.comment_pool)
        if not pool:
            return "参与抽奖"
        return pool[min(int(self._rand() * len(pool)), len(pool) - 1)]

    async def _do_action(self, call: Callable[[], Awaitable[CodeResult]]) -> CodeResult:
        """Run one action, retrying only while its outcome is RETRY."""
        result = await call()
        attempts = 1
        while result.outcome is Outcome.RETRY and attempts < self.config.action_max_attempts:
            await self._sleep(self._jittered(self.config.action_retry_base_wait * attempts))
            result = await call()
            attempts += 1
        return result

    async def participate(self, card: DynamicCard, required_actions: list[str]) -> ExecutionResult:
        # When handed a forward (转发) of a lottery, the real lottery — the UP to
        # follow, the dynamic to repost, the thread to comment on — is the
        # original dynamic, so act on it rather than the wrapper.
        target = card
        if card.type == 1:
            if card.origin and card.origin.dynamic_id:
                target = card.origin
            else:
                # A forward whose origin we couldn't resolve: acting on the
                # wrapper would follow the reposter, not the lottery host.
                return ExecutionResult(
                    dynamic_id=card.dynamic_id, aborted=True,
                    abort_reason="转发动态但无法解析源抽奖，跳过",
                )

        phases = [p for p in PHASE_ORDER if p in set(required_actions)]
        result = ExecutionResult(dynamic_id=target.dynamic_id, required=phases)

        if target.uid and target.uid in self.config.blocked_uids:
            result.aborted = True
            result.abort_reason = f"uid {target.uid} 在黑名单"
            return result
        if target.is_charge_lottery:
            result.aborted = True
            result.abort_reason = "充电专属抽奖，跳过"
            return result
        if "follow" in phases and not target.uid:
            result.aborted = True
            result.abort_reason = "无法解析关注对象 (uid=0)"
            return result

        builders: dict[str, Callable[[], Awaitable[CodeResult]]] = {
            "follow": lambda: self.client.follow(target.uid),
            "like": lambda: self.client.like(target.dynamic_id),
            "repost": lambda: self.client.repost(target.dynamic_id, self.config.repost_text),
            "comment": lambda: self.client.comment(target.rid_str, target.chat_type, self._pick_comment()),
        }

        for i, phase in enumerate(phases):
            if phase == "comment" and not target.rid_str:
                result.actions[phase] = CodeResult(0, Outcome.SKIP, "无评论 rid，跳过评论")
                continue

            if i > 0:
                await self._pace()
            res = await self._do_action(builders[phase])
            result.actions[phase] = res

            if res.outcome in ABORT_OUTCOMES:
                result.aborted = True
                result.abort_outcome = res.outcome
                result.abort_reason = f"{phase}: {res.message}"
                return result
            if res.ok:
                continue
            # non-OK, non-abort outcomes:
            if phase == "follow":
                if res.outcome is Outcome.LIMIT:
                    result.follow_capped = True
                result.stopped_reason = f"follow 未成功 ({res.message})，跳过后续动作"
                break
            if res.outcome is Outcome.FATAL:
                result.stopped_reason = f"{phase} 未知错误，保守中止"
                break
            # SKIP / LIMIT / CAPTCHA on a non-follow phase: record and continue.

        result.success = bool(phases) and all(result.actions.get(p, _MISSING).ok for p in phases)
        return result
