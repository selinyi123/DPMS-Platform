"""Deterministic, no-retry orchestration for official Weibo OAuth actions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable, Protocol

from app.action_plan import (
    WEIBO_OAUTH_EXECUTION_PATH,
    ValidatedActionPlanV2,
)
from app.weibo.client import WeiboActionReceipt, validate_weibo_text
from app.weibo.credentials import validate_weibo_rip, weibo_rip_required


class WeiboActionClient(Protocol):
    async def follow(self, target_uid: str, *, rip: str, operation_key: str) -> WeiboActionReceipt: ...

    async def like(self, status_id: str, *, operation_key: str) -> WeiboActionReceipt: ...

    async def comment(self, status_id: str, text: str, *, rip: str, operation_key: str) -> WeiboActionReceipt: ...

    async def favorite(self, status_id: str, *, operation_key: str) -> WeiboActionReceipt: ...

    async def repost(self, status_id: str, text: str | None = None, *, rip: str, operation_key: str) -> WeiboActionReceipt: ...


class WeiboExecutionError(RuntimeError):
    pass


class WeiboExecutionOutcomeUnknown(WeiboExecutionError):
    def __init__(self, action: str, reason: str) -> None:
        self.action = action
        self.reason = reason
        super().__init__(f"weibo_execution_outcome_unknown:{action}:{reason}")


@dataclass
class WeiboExecutionResult:
    status_id: str
    receipts: dict[str, WeiboActionReceipt] = field(default_factory=dict)

    @property
    def success(self) -> bool:
        return bool(self.receipts)


class WeiboOAuthExecutor:
    """Execute one already-reviewed OAuth plan exactly once per action.

    ``operation_key_for`` must return a key backed by the durable external
    action intent journal.  The executor never creates a random key, never
    retries, and stops on the first non-confirmed action.
    """

    def __init__(
        self,
        client: WeiboActionClient,
        *,
        operation_key_for: Callable[[str], Awaitable[str]],
        before_action: Callable[[str], Awaitable[None]] | None = None,
        after_receipt: Callable[[str, WeiboActionReceipt], Awaitable[None]] | None = None,
    ) -> None:
        self.client = client
        self._operation_key_for = operation_key_for
        self._before_action = before_action
        self._after_receipt = after_receipt

    async def execute(
        self,
        plan: ValidatedActionPlanV2,
        *,
        status_id: str,
        follow_target_uid: str | None = None,
        rip: str = "",
    ) -> WeiboExecutionResult:
        if plan.execution_path_id != WEIBO_OAUTH_EXECUTION_PATH:
            raise WeiboExecutionError("weibo_oauth_execution_path_required")
        if plan.plan.get("executable") is not True:
            raise WeiboExecutionError("weibo_oauth_plan_not_executable")
        required_rip = weibo_rip_required(plan.required_actions)
        bound_rip = validate_weibo_rip(rip, required=required_rip)

        # Reject every text boundary violation before the first durable intent
        # is marked started. The transport client repeats this at the HTTP edge.
        for action in plan.required_actions:
            payload = plan.payload_for(action)
            if action == "commented":
                validate_weibo_text(action, payload.get("text"))
            elif action == "reposted":
                validate_weibo_text(action, payload.get("text"), allow_none=True)

        result = WeiboExecutionResult(status_id=str(status_id))
        for action in plan.required_actions:
            if self._before_action is not None:
                await self._before_action(action)
            operation_key = await self._operation_key_for(action)
            payload = plan.payload_for(action)
            if action == "followed":
                if not follow_target_uid:
                    raise WeiboExecutionError("weibo_follow_target_uid_not_bound")
                receipt = await self.client.follow(
                    follow_target_uid,
                    rip=bound_rip,
                    operation_key=operation_key,
                )
            elif action == "liked":
                receipt = await self.client.like(
                    status_id,
                    operation_key=operation_key,
                )
            elif action == "commented":
                receipt = await self.client.comment(
                    status_id,
                    payload.get("text", ""),
                    rip=bound_rip,
                    operation_key=operation_key,
                )
            elif action == "favorited":
                receipt = await self.client.favorite(
                    status_id,
                    operation_key=operation_key,
                )
            elif action == "reposted":
                receipt = await self.client.repost(
                    status_id,
                    payload.get("text"),
                    rip=bound_rip,
                    operation_key=operation_key,
                )
            else:  # validator parity should make this unreachable
                raise WeiboExecutionError("weibo_action_not_supported")
            expected_target_id = (
                str(follow_target_uid) if action == "followed" else str(status_id)
            )
            if (
                receipt.action != action
                or receipt.target_id != expected_target_id
            ):
                raise WeiboExecutionOutcomeUnknown(
                    action,
                    "client_receipt_binding_invalid",
                )
            if self._after_receipt is not None:
                try:
                    await self._after_receipt(action, receipt)
                except BaseException as exc:
                    raise WeiboExecutionOutcomeUnknown(
                        action,
                        "receipt_persistence_failed",
                    ) from exc
            result.receipts[action] = receipt
        return result
