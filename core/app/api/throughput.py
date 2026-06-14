"""Throughput Runtime API (V13 / stage S12).

Read-only: measure observed task throughput against the sustainable ceiling
(V11 capacity, discounted by recent risk) and surface backpressure advice. The
pure logic lives in ``app.throughput.engine``; this layer gathers recent runs,
risk events and capacity.

Safety: read-only and advisory. Backpressure only ever points toward slowing
down as risk rises (``throttle``/``pause``), reinforcing — never bypassing — the
per-platform limits and circuit breaker. Nothing here changes execution.
"""

from fastapi import APIRouter

from app.api.capacity import _load_accounts, _load_proxies
from app.capacity.engine import compute_capacity
from app.db import database
from app.throughput.engine import (
    backpressure_recommendation,
    measure_throughput,
    saturation_alerts,
    sustainable_ceiling,
)

router = APIRouter()


@router.get("/overview")
async def throughput_overview(window_minutes: int = 1440):
    window_minutes = max(15, min(int(window_minutes), 7 * 24 * 60))
    observed, ceiling, _signals, _trend = await _measure(window_minutes)
    # Attach utilisation per platform for display.
    for platform, bucket in observed.items():
        ceil = float((ceiling.get(platform) or {}).get("ceiling", 0) or 0)
        bucket["ceiling"] = ceil
        bucket["utilization"] = round(bucket["observed"] / ceil, 4) if ceil > 0 else None
    return {
        "window_minutes": window_minutes,
        "observed": observed,
        "ceiling": ceiling,
        "saturation_alerts": saturation_alerts(observed, ceiling),
    }


@router.get("/backpressure")
async def throughput_backpressure(window_minutes: int = 1440):
    window_minutes = max(15, min(int(window_minutes), 7 * 24 * 60))
    observed, ceiling, _signals, trend = await _measure(window_minutes)
    recommendations = backpressure_recommendation(observed, ceiling, trend)
    return {
        "window_minutes": window_minutes,
        "recommendations": recommendations,
        "saturation_alerts": saturation_alerts(observed, ceiling),
    }


async def _measure(window_minutes: int):
    """Gather observed throughput, risk-discounted ceiling, signals and trend."""
    runs = await _load_runs(window_minutes)
    observed = measure_throughput(runs, window_min=window_minutes)

    risk_counts = await _risk_counts(window_minutes)
    risk_trend = await _risk_trend(window_minutes)

    # risk_rate per platform = risk events / observed runs (capped at 1.0).
    signals: dict[str, dict] = {}
    for platform, bucket in observed.items():
        risk = risk_counts.get(platform, 0)
        bucket["risk_rate"] = round(min(1.0, risk / bucket["observed"]) if bucket["observed"] else 0.0, 4)
        signals[platform] = {"risk_rate": bucket["risk_rate"], "failure_rate": bucket.get("failure_rate", 0.0)}

    # Scale the daily sustainable capacity down to the window for comparison.
    accounts = await _load_accounts()
    proxies = await _load_proxies()
    capacity = compute_capacity(accounts, proxies)["per_platform"]
    window_fraction = window_minutes / 1440.0
    scaled_capacity = {
        platform: {"sustainable_daily": float(bucket.get("sustainable_daily", 0) or 0) * window_fraction}
        for platform, bucket in capacity.items()
    }
    ceiling = sustainable_ceiling(scaled_capacity, signals)
    return observed, ceiling, signals, risk_trend


async def _load_runs(window_minutes: int) -> list[dict]:
    rows = await database.fetch_all(
        """SELECT a.platform AS platform, tr.status AS status
           FROM task_runs tr JOIN accounts a ON tr.account_id = a.id
           WHERE tr.created_at >= (UTC_TIMESTAMP() - INTERVAL :win MINUTE)""",
        {"win": window_minutes},
    )
    return [{"platform": row["platform"], "status": row["status"]} for row in rows]


async def _risk_counts(window_minutes: int) -> dict:
    rows = await database.fetch_all(
        """SELECT a.platform AS platform, COUNT(*) AS c
           FROM risk_events r JOIN accounts a ON r.account_id = a.id
           WHERE r.created_at >= (UTC_TIMESTAMP() - INTERVAL :win MINUTE)
           GROUP BY a.platform""",
        {"win": window_minutes},
    )
    return {row["platform"]: int(row["c"]) for row in rows}


async def _risk_trend(window_minutes: int) -> dict:
    """Compare risk in the recent half-window vs the prior half-window."""
    half = max(1, window_minutes // 2)
    recent = await _risk_counts(half)
    rows = await database.fetch_all(
        """SELECT a.platform AS platform, COUNT(*) AS c
           FROM risk_events r JOIN accounts a ON r.account_id = a.id
           WHERE r.created_at >= (UTC_TIMESTAMP() - INTERVAL :win MINUTE)
             AND r.created_at < (UTC_TIMESTAMP() - INTERVAL :half MINUTE)
           GROUP BY a.platform""",
        {"win": window_minutes, "half": half},
    )
    prior = {row["platform"]: int(row["c"]) for row in rows}
    trend: dict[str, str] = {}
    for platform in set(recent) | set(prior):
        recent_c = recent.get(platform, 0)
        prior_c = prior.get(platform, 0)
        if recent_c > prior_c:
            trend[platform] = "rising"
        elif recent_c < prior_c:
            trend[platform] = "falling"
        else:
            trend[platform] = "flat"
    return trend
