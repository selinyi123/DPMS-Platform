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
from typing import Any, Awaitable, Callable, Mapping

from .config import BiliEngineConfig
from .errors import CodeResult, Outcome
from .parser import DynamicCard

# Keep the API engine aligned with task_runner.PHASE_ORDER so its single latest
# phase marker remains monotonic and safe to resume.
PHASE_ORDER = ("follow", "like", "comment", "repost")


class ActionAttemptLimitExceeded(RuntimeError):
    """The per-target remote mutation-attempt budget is exhausted."""


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
        after_attempt: Callable[[str, CodeResult], Awaitable[None]] | None = None,
        on_attempt_error: Callable[[str, BaseException], Awaitable[None]] | None = None,
        after_action: Callable[[str, CodeResult], Awaitable[None]] | None = None,
    ) -> None:
        self.client = client
        self.config = config or BiliEngineConfig()
        self._sleep = sleep
        self._rand = rand
        self._before_action = before_action
        self._after_attempt = after_attempt
        self._on_attempt_error = on_attempt_error
        self._after_action = after_action
        self._action_attempts = 0

    def _jittered(self, base: float) -> float:
        # [1-f, 1+f] * base, matching the reference's ±50% relay jitter but
        # applied to every delay.
        f = self.config.jitter_fraction
        return base * (1 - f + 2 * f * self._rand())

    async def _pace(self) -> None:
        await self._sleep(self._jittered(self.config.action_wait))

    async def _do_action(self, name: str, call: Callable[[], Awaitable[CodeResult]]) -> CodeResult:
        """Run one action, re-checking the gate before every external attempt."""
        async def attempt() -> CodeResult:
            if self._action_attempts >= self.config.max_actions_per_target:
                raise ActionAttemptLimitExceeded("达到单目标动作上限")
            if self._before_action is not None:
                await self._before_action(name)
            self._action_attempts += 1
            try:
                result = await call()
            except BaseException as exc:
                if self._on_attempt_error is not None:
                    await self._on_attempt_error(name, exc)
                raise
            if self._after_attempt is not None:
                await self._after_attempt(name, result)
            return result

        result = await attempt()
        attempts = 1
        while result.outcome is Outcome.RETRY and attempts < self.config.action_max_attempts:
            await self._sleep(self._jittered(self.config.action_retry_base_wait * attempts))
            result = await attempt()
            attempts += 1
        return result

    async def participate(
        self,
        card: DynamicCard,
        required_actions: list[str],
        action_payloads: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> ExecutionResult:
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
        payloads = dict(action_payloads or {})
        for payload in payloads.values():
            if not isinstance(payload, Mapping):
                raise RuntimeError("bilibili_action_payload_invalid")
            if payload.get("media_refs"):
                raise RuntimeError("bilibili_action_payload_media_unsupported")

        exact_text: dict[str, str] = {}
        for api_action, dpms_action in (("comment", "commented"), ("repost", "reposted")):
            if api_action not in phases:
                continue
            payload = payloads.get(dpms_action)
            text = payload.get("text") if isinstance(payload, Mapping) else None
            if not isinstance(text, str) or not text.strip():
                raise RuntimeError(f"bilibili_{api_action}_exact_text_required")
            exact_text[api_action] = text
        self._action_attempts = 0
        builders: dict[str, Callable[[], Awaitable[CodeResult]]] = {
            "follow": lambda: self.client.follow(target.uid),
            "like": lambda: self.client.like(target.dynamic_id),
            "repost": lambda: self.client.repost(target.dynamic_id, exact_text["repost"]),
            "comment": lambda: self.client.comment(
                target.rid_str, target.chat_type, exact_text["comment"]
            ),
        }

        for i, phase in enumerate(phases):
            if phase == "comment" and not target.rid_str:
                res = CodeResult(0, Outcome.SKIP, "无评论 rid，跳过评论")
            else:
                if i > 0:
                    await self._pace()
                try:
                    res = await self._do_action(phase, builders[phase])
                except ActionAttemptLimitExceeded as exc:
                    res = CodeResult(-1, Outcome.LIMIT, str(exc))
                    result.actions[phase] = res
                    if self._after_action is not None:
                        await self._after_action(phase, res)
                    result.aborted = True
                    result.abort_reason = str(exc)
                    return result
            result.actions[phase] = res
            if self._after_action is not None:
                await self._after_action(phase, res)

            # Every entry in ``required_actions`` is eligibility-critical.
            # Once one of them is not OK, later mutations cannot make the
            # participation complete and only add external side effects. The
            # result is persisted above before the run fails closed.
            if not res.ok:
                result.aborted = True
                result.abort_reason = f"{phase}: {res.message}"
                return result
        # Success = every required phase ended OK (a SKIP on an impossible phase
        # does not count as success — the lottery's requirement was not met).
        result.success = bool(phases) and all(result.actions.get(p, CodeResult(0, Outcome.FATAL, "")).ok for p in phases)
        return result
