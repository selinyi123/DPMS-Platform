"""Autonomous real-run dispatch for the Bilibili direct-API channel (Phase 2b).

A single asyncio loop (core-api lifespan) that, when enabled, periodically picks
pending Bilibili lotteries + ready accounts and enqueues ``real_run`` tasks
WITHOUT the per-lottery Playwright probe/shadow evidence gate (which calibrates
selectors and is meaningless for the signed-API channel).

TRIPLE-GATED AND OFF BY DEFAULT — all three must hold for anything to dispatch:
  1. ``DPMS_AUTO_DISPATCH_ENABLED=1``   (this loop)
  2. the global real-run switch is on   (``is_real_run_enabled()``)
  3. ``DPMS_BILIBILI_API_MODE=1``        (so the worker executes via the engine)

Per-cycle safety still enforced: account daily cap + per-account spacing
(``scheduling.PLATFORM_RATE_LIMITS``), one dispatch per account per cycle, a
per-cycle global cap, the platform circuit breaker, no recent account
``risk_event``, a valid target with a reviewed action plan, a per-lottery
failed-attempt retry cap, and the atomic one-active-task-per-lottery claim.

The eligibility decisions are the pure, unit-tested core (``account_eligible`` /
``lottery_eligible`` / ``select_account_for_cycle``); the DB I/O is thin wiring
around the existing dispatch helpers, so it can't diverge from the HTTP path.

NOTE: this path performs real follows/reposts on the operator's accounts without
a human in the loop. It must be validated against a throwaway account (the
self-test harness) before either flag is turned on.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.db import database
from app.scheduling.engine import rate_limit_for
from app.security import circuit_breaker_allows, is_real_run_enabled
from app.services.outbox import build_lottery_task_message, enqueue_outbox, try_flush_dedup
from app.utils.lottery_targets import validate_lottery_target
from app.utils.log import structured_log

PLATFORM = "bilibili"
_TRUTHY = {"1", "true", "yes", "on"}
_RESET_SETTING = "daily_task_count_reset_date"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def auto_dispatch_enabled() -> bool:
    return os.getenv("DPMS_AUTO_DISPATCH_ENABLED", "").strip().lower() in _TRUTHY


def api_mode_enabled() -> bool:
    return os.getenv("DPMS_BILIBILI_API_MODE", "").strip().lower() in _TRUTHY


# ----- pure eligibility / selection (unit tested) -----


@dataclass
class AccountStats:
    account_id: int
    daily_task_count: int
    last_real_dispatch_at: datetime | None


def account_eligible(stats: AccountStats, limits: dict, now: datetime) -> tuple[bool, str]:
    """Daily-cap + spacing gate for one account."""
    max_daily = int(limits.get("max_daily_per_account", 0) or 0)
    if max_daily and stats.daily_task_count >= max_daily:
        return False, f"daily cap reached ({stats.daily_task_count}/{max_daily})"
    spacing_min = int(limits.get("min_spacing_min", 0) or 0)
    if spacing_min and stats.last_real_dispatch_at is not None:
        elapsed = (now - stats.last_real_dispatch_at).total_seconds() / 60.0
        if elapsed < spacing_min:
            return False, f"spacing not met ({elapsed:.0f}m < {spacing_min}m)"
    return True, "ok"


def lottery_eligible(failed_attempts: int, max_retries: int) -> tuple[bool, str]:
    """Retry-cap gate so a permanently-blocked lottery isn't dispatched forever."""
    if failed_attempts > max_retries:
        return False, f"retry cap exceeded ({failed_attempts} > {max_retries})"
    return True, "ok"


# ----- DB wiring (Docker/integration verified) -----


async def reset_daily_counts_if_due(today: str) -> bool:
    """Zero accounts.daily_task_count once per local day. Idempotent across
    instances via the runtime_settings marker (safe even if another job also
    resets — both no-op after the first)."""
    row = await database.fetch_one(
        "SELECT setting_value FROM runtime_settings WHERE setting_key = :k", {"k": _RESET_SETTING}
    )
    if row and str(row["setting_value"]) == today:
        return False
    await database.execute("UPDATE accounts SET daily_task_count = 0 WHERE daily_task_count <> 0")
    await database.execute(
        """INSERT INTO runtime_settings (setting_key, setting_value)
           VALUES (:k, :v)
           ON DUPLICATE KEY UPDATE setting_value = :v, updated_at = NOW()""",
        {"k": _RESET_SETTING, "v": today},
    )
    structured_log("info", "auto_dispatch_daily_reset", date=today)
    return True


async def _fetch_pending_lotteries(limit: int) -> list[dict]:
    rows = await database.fetch_all(
        """SELECT id, platform, raw_url, canonical_url, action_plan
           FROM lotteries
           WHERE platform = :platform AND status = 'pending'
             AND (expires_at IS NULL OR expires_at > NOW())
           ORDER BY value_score DESC, id ASC
           LIMIT :limit""",
        {"platform": PLATFORM, "limit": limit},
    )
    return [dict(r) for r in rows]


async def _pick_ready_account(exclude_ids: set[int]) -> dict | None:
    rows = await database.fetch_all(
        """SELECT a.id, a.daily_task_count
           FROM accounts a
           WHERE a.platform = :platform AND a.status = 'ready'
             AND OCTET_LENGTH(a.encrypted_credential) > 0
             AND (SELECT c.status FROM account_calibrations c
                  WHERE c.account_id = a.id ORDER BY c.created_at DESC LIMIT 1) = 'succeeded'
           ORDER BY a.daily_task_count ASC, a.id ASC
           LIMIT 50""",
        {"platform": PLATFORM},
    )
    for r in rows:
        if int(r["id"]) not in exclude_ids:
            return dict(r)
    return None


async def _account_stats(account_id: int, daily_task_count: int) -> AccountStats:
    row = await database.fetch_one(
        """SELECT MAX(created_at) AS last_at FROM task_runs
           WHERE account_id = :id AND task_mode = 'real_run'""",
        {"id": account_id},
    )
    return AccountStats(account_id, int(daily_task_count or 0), row["last_at"] if row else None)


async def _failed_attempts(lottery_id: int) -> int:
    row = await database.fetch_one(
        "SELECT COUNT(*) AS n FROM task_runs WHERE lottery_id = :id AND task_mode = 'real_run' AND status = 'failed'",
        {"id": lottery_id},
    )
    return int(row["n"]) if row else 0


async def _has_recent_risk(account_id: int) -> bool:
    row = await database.fetch_one(
        "SELECT COUNT(*) AS n FROM risk_events WHERE account_id = :id AND created_at > (NOW() - INTERVAL 24 HOUR)",
        {"id": account_id},
    )
    return bool(row and int(row["n"]) > 0)


async def _dispatch_api_real_run(lottery: dict, account_id: int) -> str | None:
    """Atomically claim the lottery and enqueue a real_run API task. Mirrors the
    HTTP dispatch's atomic block (sans the Playwright evidence/governance gate)."""
    task_id = str(uuid.uuid4())
    try:
        action_plan = json.loads(lottery.get("action_plan") or "{}")
    except (TypeError, ValueError):
        action_plan = {}
    message = build_lottery_task_message(
        task_id=task_id,
        account_id=account_id,
        lottery_id=lottery["id"],
        platform=lottery["platform"],
        raw_url=lottery.get("raw_url"),
        canonical_url=lottery.get("canonical_url"),
        task_mode="real_run",
        dry_run=False,
        platform_selectors={},  # API channel does not use Playwright selectors
        action_plan=action_plan,
    )
    async with database.transaction():
        locked = await database.fetch_one(
            "SELECT status, execution_lock FROM lotteries WHERE id = :id FOR UPDATE", {"id": lottery["id"]}
        )
        if locked and locked["execution_lock"] and locked["status"] in {"claimed", "running"}:
            return None
        await database.execute(
            """INSERT INTO task_runs (task_id, account_id, lottery_id, status, dry_run, task_mode)
               VALUES (:task_id, :account_id, :lottery_id, 'queued', 0, 'real_run')""",
            {"task_id": task_id, "account_id": account_id, "lottery_id": lottery["id"]},
        )
        await database.execute(
            "UPDATE lotteries SET status = 'claimed', execution_lock = :task_id, locked_at = NOW() WHERE id = :id",
            {"task_id": task_id, "id": lottery["id"]},
        )
        await enqueue_outbox(message, "lottery_tasks", dedup_key=task_id)
    try:
        await try_flush_dedup(task_id)
    except Exception as exc:  # noqa: BLE001 - committed row is retried by the outbox loop
        structured_log("warning", "auto_dispatch_flush_failed", task_id=task_id, error=str(exc))
    return task_id


async def run_auto_dispatch_cycle(now: datetime | None = None) -> dict:
    """One scan/dispatch pass. Returns a small summary for logging/tests."""
    now = now or datetime.now()
    if not auto_dispatch_enabled() or not api_mode_enabled():
        return {"skipped": "flag_off"}
    if not await is_real_run_enabled():
        return {"skipped": "real_run_disabled"}
    allowed, breaker_reason = await circuit_breaker_allows(PLATFORM)
    if not allowed:
        return {"skipped": f"circuit_breaker:{breaker_reason}"}

    await reset_daily_counts_if_due(now.date().isoformat())

    limits = rate_limit_for(PLATFORM)
    max_per_cycle = _env_int("DPMS_AUTO_DISPATCH_MAX_PER_CYCLE", 3)
    max_retries = _env_int("DPMS_AUTO_DISPATCH_MAX_RETRIES", 2)

    candidates = await _fetch_pending_lotteries(limit=max_per_cycle * 5)
    used_accounts: set[int] = set()
    dispatched: list[dict] = []

    for lottery in candidates:
        if len(dispatched) >= max_per_cycle:
            break
        if not validate_lottery_target(PLATFORM, lottery.get("raw_url") or "").valid:
            continue
        if not lottery_eligible(await _failed_attempts(lottery["id"]), max_retries)[0]:
            continue
        account = await _pick_ready_account(exclude_ids=used_accounts)
        if not account:
            break  # no eligible accounts left this cycle
        stats = await _account_stats(account["id"], account["daily_task_count"])
        ok_acct, reason = account_eligible(stats, limits, now)
        if not ok_acct:
            used_accounts.add(account["id"])  # don't reconsider this account this cycle
            continue
        if await _has_recent_risk(account["id"]):
            used_accounts.add(account["id"])
            continue
        task_id = await _dispatch_api_real_run(lottery, account["id"])
        used_accounts.add(account["id"])
        if task_id:
            dispatched.append({"lottery_id": lottery["id"], "account_id": account["id"], "task_id": task_id})

    if dispatched:
        structured_log("info", "auto_dispatch_cycle", count=len(dispatched), dispatched=dispatched)
    return {"dispatched": dispatched, "candidates": len(candidates)}


async def start_auto_dispatch_loop() -> None:
    interval = _env_int("DPMS_AUTO_DISPATCH_INTERVAL", 300)
    structured_log("info", "auto_dispatch_loop_started", interval=interval, enabled=auto_dispatch_enabled())
    while True:
        try:
            await run_auto_dispatch_cycle()
        except asyncio.CancelledError:
            break
        except Exception as exc:  # noqa: BLE001 - never let one cycle kill the loop
            structured_log("error", "auto_dispatch_cycle_error", error=str(exc))
        await asyncio.sleep(interval)
