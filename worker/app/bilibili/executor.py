"""Participate in one Bilibili lottery via the API client.

This is the Python re-implementation of the *action* half of LotteryAutoScript's
``monitor.go()`` loop: given a parsed lottery card and the actions the lottery
requires, perform follow/like/repost/comment in a fixed order with jittered
delays, per-action retry/backoff, and risk-control aborts.

It is deliberately decoupled from the network: it takes any object exposing the
``follow/like/repost/comment`` coroutines (the real
:class:`~worker.app.bilibili.client.BilibiliApiClient` or a fake), and its
``sleep``/``rand`` callables are injectable, so the whole orchestration —
sequencing, retries, abort-on-risk — is unit-testable offline.
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


@dataclass
class ExecutionResult:
    dynamic_id: str
    success: bool = False
    aborted: bool = False
    abort_reason: str = ""
    #: phase name -> the CodeResult that settled it
    actions: dict[str, CodeResult] = field(default_factory=dict)

    @property
    def performed(self) -> list[str]:
        return [name for name, r in self.actions.items() if r.ok]


class BilibiliApiExecutor:
    def __init__(
        self,
        client,
        config: BiliEngineConfig | None = None,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rand: Callable[[], float] = random.random,
        before_action: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self.client = client
        self.config = config or BiliEngineConfig()
        self._sleep = sleep
        self._rand = rand
        self._before_action = before_action

    def _jittered(self, base: float) -> float:
        # [1-f, 1+f] * base, matching the reference's ±50% relay jitter but
        # applied to every delay.
        f = self.config.jitter_fraction
        return base * (1 - f + 2 * f * self._rand())

    async def _pace(self) -> None:
        await self._sleep(self._jittered(self.config.action_wait))

    async def _do_action(self, name: str, call: Callable[[], Awaitable[CodeResult]]) -> CodeResult:
        """Run one action, re-checking the gate before every external attempt."""
        if self._before_action is not None:
            await self._before_action(name)
        result = await call()
        attempts = 1
        while result.outcome is Outcome.RETRY and attempts < self.config.action_max_attempts:
            await self._sleep(self._jittered(self.config.action_retry_base_wait * attempts))
            if self._before_action is not None:
                await self._before_action(name)
            result = await call()
            attempts += 1
        return result

    async def participate(self, card: DynamicCard, required_actions: list[str]) -> ExecutionResult:
        # When handed a forward (转发) of a lottery, the real lottery — the UP to
        # follow, the dynamic to repost, the thread to comment on — is the
        # original dynamic, so act on it rather than the wrapper.
        target = card
        if card.type == 1 and card.origin and card.origin.dynamic_id:
            target = card.origin

        result = ExecutionResult(dynamic_id=target.dynamic_id)

        if target.uid and target.uid in self.config.blocked_uids:
            result.aborted = True
            result.abort_reason = f"uid {target.uid} 在黑名单"
            return result
        if target.is_charge_lottery:
            result.aborted = True
            result.abort_reason = "充电专属抽奖，跳过"
            return result

        phases = [p for p in PHASE_ORDER if p in set(required_actions)]
        builders: dict[str, Callable[[], Awaitable[CodeResult]]] = {
            "follow": lambda: self.client.follow(target.uid),
            "like": lambda: self.client.like(target.dynamic_id),
            "repost": lambda: self.client.repost(target.dynamic_id, self.config.repost_text),
            "comment": lambda: self.client.comment(
                target.rid_str, target.chat_type, random.choice(list(self.config.comment_pool))
            ),
        }

        performed = 0
        for i, phase in enumerate(phases):
            if performed >= self.config.max_actions_per_target:
                result.abort_reason = "达到单目标动作上限"
                break
            if phase == "comment" and not target.rid_str:
                result.actions[phase] = CodeResult(0, Outcome.SKIP, "无评论 rid，跳过评论")
                continue

            if i > 0:
                await self._pace()
            res = await self._do_action(phase, builders[phase])
            result.actions[phase] = res

            if res.outcome in ABORT_OUTCOMES:
                result.aborted = True
                result.abort_reason = f"{phase}: {res.message}"
                return result
            if res.ok:
                performed += 1
            # LIMIT/SKIP/CAPTCHA/FATAL: record and move on to the next phase.

        # Success = every required phase ended OK (a SKIP on an impossible phase
        # does not count as success — the lottery's requirement was not met).
        result.success = bool(phases) and all(result.actions.get(p, CodeResult(0, Outcome.FATAL, "")).ok for p in phases)
        return result
