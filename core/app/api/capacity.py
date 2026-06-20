"""Capacity Runtime API (V11 / stage S10).

Read-only: model the supply side of the fleet — safe accounts, healthy
proxies, the sustainable daily ceiling each platform implies, and (respecting
one-account-one-proxy isolation) which idle accounts could be bound to free
healthy proxies. The pure aggregation lives in ``app.capacity.engine``.

Safety: read-only and advisory. Binding *recommendations* are proposals only;
the actual binding stays a human action through the existing account-management
flow. ``isolation_violations`` reports shared proxies/fingerprints so an
operator can fix them — it never mutates anything.
"""

from datetime import datetime, timezone

from fastapi import APIRouter

from app.capacity.engine import (
    compute_capacity,
    isolation_violations,
    recommend_bindings,
)
from app.db import database

router = APIRouter()


@router.get("/overview")
async def capacity_overview():
    """Per-platform supply, sustainable ceilings and the global proxy pool."""
    accounts = await _load_accounts()
    proxies = await _load_proxies()
    capacity = compute_capacity(accounts, proxies)

    # Augment with observed current daily usage so headroom is visible.
    usage = await database.fetch_all(
        "SELECT platform, COALESCE(SUM(daily_task_count), 0) AS used FROM accounts GROUP BY platform"
    )
    used_by_platform = {row["platform"]: int(row["used"] or 0) for row in usage}
    for platform, bucket in capacity["per_platform"].items():
        used = used_by_platform.get(platform, 0)
        bucket["current_daily_used"] = used
        bucket["headroom"] = max(0, bucket["sustainable_daily"] - used)
    return capacity


@router.get("/bindings")
async def capacity_bindings():
    """Binding recommendations for idle accounts plus isolation warnings."""
    accounts = await _load_accounts()
    proxies = await _load_proxies()
    return {
        **recommend_bindings(accounts, proxies),
        "isolation_violations": isolation_violations(accounts),
    }


async def _load_accounts() -> list[dict]:
    rows = await database.fetch_all(
        "SELECT id, platform, status, proxy_id, fingerprint_id FROM accounts"
    )
    return [
        {
            "account_id": row["id"],
            "platform": row["platform"],
            "status": row["status"],
            "proxy_id": row["proxy_id"],
            "fingerprint_id": row["fingerprint_id"],
        }
        for row in rows
    ]


async def _load_proxies() -> list[dict]:
    rows = await database.fetch_all(
        "SELECT id, status, health_score, cooldown_until FROM proxies"
    )
    now = datetime.now(timezone.utc)
    proxies = []
    for row in rows:
        proxies.append({
            "proxy_id": row["id"],
            "status": row["status"],
            "health_score": row["health_score"],
            "in_cooldown": _in_cooldown(row["cooldown_until"], now),
        })
    return proxies


def _in_cooldown(cooldown_until, now: datetime) -> bool:
    if not cooldown_until:
        return False
    try:
        until = cooldown_until
        if isinstance(until, str):
            until = datetime.fromisoformat(until)
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        return until > now
    except (ValueError, TypeError):
        return False
