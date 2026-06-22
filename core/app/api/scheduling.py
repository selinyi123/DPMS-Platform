"""Scheduling Runtime API (V10 / stage S9).

Read-only: compute an advisory *schedule plan* for candidate lotteries across
the dispatchable account fleet, honouring compliant per-platform rate limits
(daily caps, minimum spacing, window caps). The pure planning lives in
``app.scheduling.engine``; this layer only gathers current accounts/candidates
and converts the engine's minute-offsets into wall-clock timestamps.

Safety: every endpoint is read-only and advisory. It performs no writes,
dispatches nothing, and the plan only ever *withholds* work that would exceed a
compliance limit. The real-run gate still decides whether and how any task
actually runs — slots carry ``mode="gated"`` to make that explicit.
"""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter

from app.db import database
from app.scheduling.engine import (
    PLATFORM_RATE_LIMITS,
    detect_overload,
    plan_schedule,
    rate_limit_for,
)

router = APIRouter()

# Statuses an account must be in to be considered dispatchable at all. The
# scheduler never plans work onto cooling/login-required/frozen accounts.
DISPATCHABLE_STATUS = "ready"
# Candidate lotteries are those still worth running.
CANDIDATE_STATUSES = ("pending", "claimed")


@router.get("/limits")
async def scheduling_limits():
    """Expose the compliance rate-limit table for transparency."""
    return {"limits": PLATFORM_RATE_LIMITS}


@router.get("/plan")
async def scheduling_plan(window_minutes: int = 240, platform: str | None = None):
    """Return an advisory schedule plan for the next ``window_minutes``."""
    window_minutes = max(1, min(int(window_minutes), 24 * 60))
    now = datetime.now(timezone.utc)

    accounts = await _load_dispatchable_accounts(platform)
    candidates = await _load_candidates(platform)

    plan = plan_schedule(
        candidates=candidates,
        accounts=accounts,
        window_minutes=window_minutes,
    )
    overloaded = detect_overload(
        candidates, accounts, window_minutes=window_minutes
    )

    # Convert minute-offsets into wall-clock timestamps for display.
    for slot in plan["slots"]:
        offset = int(slot.get("scheduled_at_offset_min", 0))
        slot["scheduled_at"] = (now + timedelta(minutes=offset)).isoformat()

    plan["generated_at"] = now.isoformat()
    plan["overloaded_platforms"] = overloaded
    plan["dispatchable_accounts"] = len(accounts)
    plan["candidate_count"] = len(candidates)
    return plan


async def _load_dispatchable_accounts(platform: str | None) -> list[dict]:
    """Ready accounts with their remaining daily quota and readiness offset."""
    where = "status = :status AND deleted_at IS NULL"
    values: dict = {"status": DISPATCHABLE_STATUS}
    if platform:
        where += " AND platform = :platform"
        values["platform"] = platform
    rows = await database.fetch_all(
        f"""SELECT id, platform, status, risk_score, daily_task_count, last_active_at
            FROM accounts WHERE {where}""",
        values,
    )
    now = datetime.now(timezone.utc)
    accounts = []
    for row in rows:
        record = dict(row)
        limit = rate_limit_for(record.get("platform"))
        max_daily = int(limit["max_daily_per_account"])
        spacing = int(limit["min_spacing_min"])
        used = int(record.get("daily_task_count") or 0)
        ready_in_min = _ready_in_min(record.get("last_active_at"), spacing, now)
        accounts.append({
            "account_id": record["id"],
            "platform": record.get("platform"),
            "remaining_quota": max(0, max_daily - used),
            "ready_in_min": ready_in_min,
            "risk_score": int(record.get("risk_score") or 0),
        })
    return accounts


async def _load_candidates(platform: str | None) -> list[dict]:
    """Pending/claimed, not-yet-expired lotteries, highest value first."""
    placeholders = ", ".join(f":s{i}" for i in range(len(CANDIDATE_STATUSES)))
    values: dict = {f"s{i}": status for i, status in enumerate(CANDIDATE_STATUSES)}
    where = f"status IN ({placeholders}) AND (expires_at IS NULL OR expires_at > UTC_TIMESTAMP())"
    if platform:
        where += " AND platform = :platform"
        values["platform"] = platform
    rows = await database.fetch_all(
        f"""SELECT id, platform, value_score
            FROM lotteries WHERE {where}
            ORDER BY value_score DESC, id ASC LIMIT 500""",
        values,
    )
    return [
        {"lottery_id": row["id"], "platform": row["platform"], "value_score": int(row["value_score"] or 0)}
        for row in rows
    ]


def _ready_in_min(last_active_at, spacing_min: int, now: datetime) -> int:
    """Minutes until an account clears its minimum spacing since last activity.

    Returns 0 when the account is already free (or when the timestamp is
    missing/unparseable — fail-soft toward "available now", since the engine
    and the gate still bound everything else).
    """
    if not last_active_at:
        return 0
    try:
        last = last_active_at
        if isinstance(last, str):
            last = datetime.fromisoformat(last)
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        elapsed_min = (now - last).total_seconds() / 60.0
        return max(0, int(spacing_min - elapsed_min))
    except (ValueError, TypeError):
        return 0
