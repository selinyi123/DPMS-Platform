"""Bilibili real-action execution via the direct-API engine (Phase 2a, opt-in).

When ``DPMS_BILIBILI_API_MODE`` is enabled, a Bilibili ``real_run`` task is
executed through :mod:`app.bilibili` (signed HTTP API) instead of the Playwright
selector path. **Off by default** — nothing here runs until the flag is set, and
it should only be set after the operator's live self-test
(``worker/tools/bilibili_api_selftest.py``) has validated the engine against a
throwaway account.

The decision logic (which phases to record, when to cool the account, when to
fail the task) is isolated in the pure :func:`plan_from_result` so it is unit
testable without a database; :func:`run_bilibili_api_task` is the thin DB/IO
wiring the worker calls.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from app.bilibili import (
    BiliEngineConfig,
    BilibiliApiClient,
    BilibiliApiExecutor,
    ExecutionResult,
    Outcome,
    parse_dynamic_card,
)
from app.bilibili.targets import extract_dynamic_id
from app.db import database
from app.safety import set_account_status
from app.utils.crypto import CREDENTIAL_AAD, cookie_vault
from app.utils.log import structured_log

# worker phase name (past tense) <-> engine action name
_PHASE_TO_ACTION = {"followed": "follow", "liked": "like", "commented": "comment", "reposted": "repost"}
_ACTION_TO_PHASE = {v: k for k, v in _PHASE_TO_ACTION.items()}

_TRUTHY = {"1", "true", "yes", "on"}


def api_mode_enabled(platform: str) -> bool:
    """True iff the Bilibili API channel is switched on for this platform."""
    return platform == "bilibili" and os.getenv("DPMS_BILIBILI_API_MODE", "").strip().lower() in _TRUTHY


@dataclass
class ChannelOutcome:
    """The side-effects the worker should apply for an :class:`ExecutionResult`."""

    phases_to_save: list[str] = field(default_factory=list)  # worker phase names (+ "completed")
    account_status: tuple[str, str] | None = None            # (status, reason) or None
    failure_reason: str | None = None                        # raise -> task marked failed when set


def plan_from_result(result: ExecutionResult, actions: list[str]) -> ChannelOutcome:
    """Map an engine result to worker side-effects (pure, DB-free)."""
    plan = ChannelOutcome()
    for action in actions:
        r = result.actions.get(action)
        if r and r.ok:
            plan.phases_to_save.append(_ACTION_TO_PHASE[action])

    # RISK / AUTH aborts must cool / re-login the account so it is not re-dispatched.
    if result.aborted and result.abort_outcome is Outcome.RISK:
        plan.account_status = ("cooling", "api_risk_signal")
    elif result.aborted and result.abort_outcome is Outcome.AUTH:
        plan.account_status = ("login_required", "api_auth_failed")

    if result.success:
        plan.phases_to_save.append("completed")
    else:
        plan.failure_reason = result.abort_reason or result.stopped_reason or "API 参与未全部成功"
    return plan


async def _load_cookie_header(account_id: int) -> str:
    row = await database.fetch_one(
        "SELECT encrypted_credential FROM accounts WHERE id = :id", {"id": account_id}
    )
    if not row or not row["encrypted_credential"]:
        raise ValueError(f"Account {account_id} has no imported login Cookie")
    blob = row["encrypted_credential"]
    try:
        credential = cookie_vault.decrypt(blob, aad=CREDENTIAL_AAD)
    except Exception:
        credential = blob.decode("utf-8") if isinstance(blob, bytes) else str(blob)
    cookies = json.loads(credential)
    if not isinstance(cookies, list) or not cookies:
        raise ValueError("Account cookie is empty or invalid")
    return "; ".join(f"{c['name']}={c['value']}" for c in cookies if c.get("name"))


async def run_bilibili_api_task(
    task: dict,
    phases: list[str],
    save_phase: Callable[[str, int, int, str], Awaitable[None]],
    refresh_lease: Callable[[str], Awaitable[None]],
) -> None:
    """Execute one Bilibili real_run task via the API engine.

    Raises on any non-success so the caller marks the task failed; on RISK/AUTH
    it first cools / flags the account (which survives ``mark_task_finished``
    because that only resets accounts still in ``executing``).
    """
    task_id = str(task.get("task_id"))
    account_id = int(task["account_id"])
    lottery_id = int(task["lottery_id"])
    url = task.get("raw_url") or task.get("canonical_url") or ""

    dyid = extract_dynamic_id(url)
    if not dyid:
        raise RuntimeError(f"API 模式无法从目标解析 dynamic_id: {url!r}（b23.tv 短链请先展开）")

    actions = [_PHASE_TO_ACTION[p] for p in phases if p in _PHASE_TO_ACTION]
    cookie = await _load_cookie_header(account_id)
    cfg = BiliEngineConfig()

    async with BilibiliApiClient(cookie, config=cfg) as client:
        if not await client.check_login():
            await set_account_status(account_id, "login_required", "api_cookie_invalid")
            raise RuntimeError("API 模式: 账号 Cookie 无效（未登录）")
        await refresh_lease(task_id)
        detail = await client.get_dynamic_detail(dyid)
        card = parse_dynamic_card((detail.get("data") or {}).get("item"))
        if not card.dynamic_id:
            raise RuntimeError(f"API 模式: 无法加载动态 {dyid}")
        await refresh_lease(task_id)
        result = await BilibiliApiExecutor(client, cfg).participate(card, actions)

    structured_log(
        "info", "bilibili_api_task_result", task_id=task_id, success=result.success,
        aborted=result.aborted, performed=result.performed, follow_capped=result.follow_capped,
    )

    plan = plan_from_result(result, actions)
    for phase in plan.phases_to_save:
        await save_phase(task_id, account_id, lottery_id, phase)
    if plan.account_status:
        status, reason = plan.account_status
        await set_account_status(account_id, status, reason)
    if plan.failure_reason:
        raise RuntimeError(plan.failure_reason)
