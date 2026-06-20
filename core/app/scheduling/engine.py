"""Pure scheduling logic for the DPMS Scheduling Runtime (V10 / stage S9).

No database or framework dependency. The Scheduling Runtime answers *when*
candidate work should run, subject to compliant, platform-friendly limits:
per-account daily caps, a minimum spacing between an account's tasks, and a
time window. It produces an advisory *schedule plan* — a proposal — and never
dispatches anything.

Time is expressed as integer "minutes from now" so the engine stays a pure
function: the API layer converts an account's ``last_active_at`` into a
``ready_in_min`` before calling in, and converts ``scheduled_at_offset_min``
back into a wall-clock timestamp on the way out.

Safety boundary (non-negotiable):

- The limits here are *compliance* limits (conservative, platform-friendly).
  The scheduler only ever *withholds* work that would exceed a limit (it lands
  in ``unscheduled``); it never schedules beyond a cap, never shrinks the
  spacing, and never relaxes a cooldown. ``respects_rate_limit`` re-checks any
  produced plan and is asserted in the tests as a standing invariant.
- This module decides timing only. The execution *mode* (dry/shadow/real) is
  decided by the real-run gate at dispatch, not here — slots carry
  ``mode="gated"`` to make that explicit. Producing a plan dispatches nothing.
"""

from __future__ import annotations


# Conservative, platform-friendly defaults. These are compliance limits: the
# scheduler treats them as ceilings it must never exceed, not targets to hit.
PLATFORM_RATE_LIMITS: dict[str, dict[str, int]] = {
    "bilibili": {"max_daily_per_account": 8, "min_spacing_min": 45, "max_per_window": 12},
    "weibo": {"max_daily_per_account": 6, "min_spacing_min": 60, "max_per_window": 8},
    "xiaohongshu": {"max_daily_per_account": 5, "min_spacing_min": 75, "max_per_window": 6},
    "douyin": {"max_daily_per_account": 5, "min_spacing_min": 90, "max_per_window": 6},
}

# Fallback for any platform without an explicit entry: the most conservative
# shape, so an unknown platform is never scheduled more aggressively.
DEFAULT_RATE_LIMIT: dict[str, int] = {
    "max_daily_per_account": 4,
    "min_spacing_min": 90,
    "max_per_window": 4,
}


def rate_limit_for(platform) -> dict[str, int]:
    """Return the compliance rate-limit shape for a platform (never raises)."""
    return dict(PLATFORM_RATE_LIMITS.get(platform, DEFAULT_RATE_LIMIT))


def _as_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _group_by_platform(items: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for item in items:
        if isinstance(item, dict):
            grouped.setdefault(item.get("platform"), []).append(item)
    return grouped


def plan_schedule(
    *,
    candidates: list[dict],
    accounts: list[dict],
    window_minutes: int,
    limits: dict[str, dict] | None = None,
) -> dict:
    """Assign candidate work to compliant time slots within ``window_minutes``.

    ``candidates``: ``[{lottery_id, platform, value_score}]`` — higher value is
    scheduled first. ``accounts``: ``[{account_id, platform, remaining_quota,
    ready_in_min, risk_score}]`` — only dispatchable accounts should be passed;
    the engine still filters on ``remaining_quota > 0`` defensively.

    Returns a plan with ``slots`` (scheduled work), ``unscheduled`` (with a
    reason), and a per-platform ``summary``. Every slot respects the account's
    daily quota, the platform's minimum spacing, and the window — anything that
    cannot fit is withheld, never forced.
    """
    window = max(0, _as_int(window_minutes))
    limit_table = limits or PLATFORM_RATE_LIMITS

    def limit_for(platform) -> dict[str, int]:
        return dict(limit_table.get(platform, DEFAULT_RATE_LIMIT))

    cand_by_platform = _group_by_platform(candidates)
    acct_by_platform = _group_by_platform(accounts)

    slots: list[dict] = []
    unscheduled: list[dict] = []
    per_platform: dict[str, dict] = {}

    for platform in sorted(cand_by_platform, key=lambda p: (p is None, str(p))):
        cands = sorted(
            cand_by_platform[platform],
            key=lambda c: (-_as_int(c.get("value_score")), _as_int(c.get("lottery_id"))),
        )
        limit = limit_for(platform)
        spacing = max(1, _as_int(limit.get("min_spacing_min"), 1))
        max_per_window = _as_int(limit.get("max_per_window"), len(cands))

        # Mutable per-account scheduling state, only for accounts with quota.
        pool = [
            {
                "account_id": a.get("account_id"),
                "next_free": max(0, _as_int(a.get("ready_in_min"))),
                "quota_left": _as_int(a.get("remaining_quota")),
                "risk_score": _as_int(a.get("risk_score")),
            }
            for a in acct_by_platform.get(platform, [])
            if _as_int(a.get("remaining_quota")) > 0
        ]

        planned_here = 0
        for cand in cands:
            if planned_here >= max_per_window:
                unscheduled.append({
                    "lottery_id": cand.get("lottery_id"),
                    "platform": platform,
                    "reason": "platform_window_cap_reached",
                })
                continue
            # Pick the account that can take this task earliest, breaking ties
            # toward lower risk and more remaining quota.
            eligible = [
                acct for acct in pool
                if acct["quota_left"] > 0 and acct["next_free"] < window
            ]
            if not eligible:
                unscheduled.append({
                    "lottery_id": cand.get("lottery_id"),
                    "platform": platform,
                    "reason": "no_capacity_within_window",
                })
                continue
            acct = min(
                eligible,
                key=lambda x: (x["next_free"], x["risk_score"], -x["quota_left"], _as_int(x["account_id"])),
            )
            slots.append({
                "lottery_id": cand.get("lottery_id"),
                "account_id": acct["account_id"],
                "platform": platform,
                "scheduled_at_offset_min": acct["next_free"],
                "mode": "gated",
                "value_score": _as_int(cand.get("value_score")),
            })
            acct["next_free"] += spacing
            acct["quota_left"] -= 1
            planned_here += 1

        plat_unscheduled = sum(1 for u in unscheduled if u["platform"] == platform)
        capacity = sum(_as_int(a.get("remaining_quota")) for a in acct_by_platform.get(platform, []))
        per_platform[platform] = {
            "planned": planned_here,
            "unscheduled": plat_unscheduled,
            "accounts": len(pool),
            "demand": len(cands),
            "capacity": capacity,
        }

    slots.sort(key=lambda s: (s["scheduled_at_offset_min"], str(s["platform"]), _as_int(s["account_id"])))
    return {
        "window_minutes": window,
        "slots": slots,
        "unscheduled": unscheduled,
        "summary": {
            "planned": len(slots),
            "unscheduled": len(unscheduled),
            "per_platform": per_platform,
        },
    }


def respects_rate_limit(plan: dict, limits: dict[str, dict] | None = None) -> tuple[bool, list[str]]:
    """Re-verify a produced plan never exceeds its compliance limits.

    Checks, per account, that the assigned count does not exceed any single
    account's quota is *not* knowable from the plan alone; instead this asserts
    the structural invariants the scheduler must always uphold: each account's
    consecutive slots are spaced by at least the platform minimum, and every
    slot falls inside the window. Returns ``(ok, violations)``.
    """
    limit_table = limits or PLATFORM_RATE_LIMITS
    window = _as_int(plan.get("window_minutes"))
    violations: list[str] = []

    by_account: dict[tuple, list[dict]] = {}
    for slot in plan.get("slots", []):
        if _as_int(slot.get("scheduled_at_offset_min")) >= window > 0:
            violations.append(f"slot_outside_window:lottery={slot.get('lottery_id')}")
        by_account.setdefault((slot.get("platform"), slot.get("account_id")), []).append(slot)

    for (platform, account_id), account_slots in by_account.items():
        limit = dict(limit_table.get(platform, DEFAULT_RATE_LIMIT))
        spacing = max(1, _as_int(limit.get("min_spacing_min"), 1))
        offsets = sorted(_as_int(s.get("scheduled_at_offset_min")) for s in account_slots)
        for earlier, later in zip(offsets, offsets[1:]):
            if later - earlier < spacing:
                violations.append(f"spacing_violation:account={account_id}:{earlier}->{later}")

    return (not violations), violations


def detect_overload(
    candidates: list[dict],
    accounts: list[dict],
    *,
    window_minutes: int,
    limits: dict[str, dict] | None = None,
) -> list[str]:
    """List platforms whose demand exceeds sustainable supply within the window.

    Sustainable supply is the smaller of total remaining account quota and the
    number of slots that fit given spacing and the window — whichever binds
    first. A platform appears here when candidates outnumber that supply.
    """
    limit_table = limits or PLATFORM_RATE_LIMITS
    window = max(0, _as_int(window_minutes))
    cand_by_platform = _group_by_platform(candidates)
    acct_by_platform = _group_by_platform(accounts)

    overloaded: list[str] = []
    for platform, cands in cand_by_platform.items():
        limit = dict(limit_table.get(platform, DEFAULT_RATE_LIMIT))
        spacing = max(1, _as_int(limit.get("min_spacing_min"), 1))
        max_per_window = _as_int(limit.get("max_per_window"), len(cands))
        slots_per_account = (window // spacing) if window > 0 else 0
        quota = sum(_as_int(a.get("remaining_quota")) for a in acct_by_platform.get(platform, []))
        time_supply = slots_per_account * len(acct_by_platform.get(platform, []))
        sustainable = min(quota, time_supply, max_per_window)
        if len(cands) > sustainable:
            overloaded.append(platform)
    return sorted(overloaded, key=str)
