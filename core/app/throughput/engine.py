"""Pure throughput logic for the DPMS Throughput Runtime (V13 / stage S12).

No database or framework dependency. V13 is the capstone of the operational-
scaling arc: a sustainable-throughput *governor*. It measures observed load
against the sustainable ceiling (derived from V11 capacity, discounted by recent
risk), and emits **backpressure** — advice that only ever points toward *slowing
down* as risk rises.

Safety boundary (non-negotiable):

- Backpressure is single-directional toward safety. When risk is rising, the
  only actions are ``throttle`` or ``pause``; ``scale_up`` is impossible in that
  state. ``scale_up`` is offered solely when utilisation is low *and* risk is
  stable, and its ``factor`` is capped so projected load never exceeds the
  ceiling. This governor is a brake that can suggest easing off the brake — it
  is never an accelerator that can override a limit.
- All output is advisory. It reinforces (never bypasses) the per-platform rate
  limits and the circuit breaker; it changes no execution by itself.
"""

from __future__ import annotations

# Utilisation thresholds (observed / ceiling).
UTIL_THROTTLE = 1.0   # at or above ceiling -> throttle
UTIL_HOLD = 0.8       # approaching ceiling -> hold
UTIL_SCALE = 0.5      # comfortably below ceiling -> may scale up (if risk stable)
RISK_RATE_HIGH = 0.25  # risk events per run that counts as "high"
SCALE_UP_FACTOR_CAP = 2.0


def _safe_rate(numerator, denominator) -> float:
    try:
        denom = float(denominator)
        return float(numerator) / denom if denom > 0 else 0.0
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


def measure_throughput(runs: list[dict], *, window_min: int) -> dict:
    """Aggregate observed task runs per platform within a window.

    ``runs``: ``[{platform, status}]`` already filtered to the window by the
    caller. Returns per-platform observed counts, success rate and an hourly
    rate for display.
    """
    window_hours = max(window_min, 1) / 60.0
    per_platform: dict[str, dict] = {}
    for run in runs:
        platform = run.get("platform")
        bucket = per_platform.setdefault(platform, {"observed": 0, "succeeded": 0, "failed": 0})
        bucket["observed"] += 1
        if run.get("status") == "succeeded":
            bucket["succeeded"] += 1
        elif run.get("status") == "failed":
            bucket["failed"] += 1
    for bucket in per_platform.values():
        bucket["success_rate"] = round(_safe_rate(bucket["succeeded"], bucket["observed"]), 4)
        bucket["failure_rate"] = round(_safe_rate(bucket["failed"], bucket["observed"]), 4)
        bucket["observed_per_hour"] = round(bucket["observed"] / window_hours, 2)
    return per_platform


def sustainable_ceiling(capacity: dict, risk_signals: dict) -> dict:
    """Per-platform sustainable load ceiling, discounted by recent risk.

    ``capacity``: V11 ``{platform: {sustainable_daily}}``. ``risk_signals``:
    ``{platform: {risk_rate, failure_rate}}``. The ceiling is the sustainable
    volume scaled down (never up) by recent risk and failure — risk only ever
    tightens the ceiling.
    """
    ceilings: dict[str, dict] = {}
    for platform, bucket in capacity.items():
        try:
            base = float(bucket.get("sustainable_daily", 0) or 0)
        except (TypeError, ValueError):
            base = 0.0
        signal = risk_signals.get(platform, {})
        risk_rate = float(signal.get("risk_rate", 0) or 0)
        failure_rate = float(signal.get("failure_rate", 0) or 0)
        # Discount in [0, 0.75]: heavy risk/failure can cut the ceiling by up to
        # three quarters, but never below a quarter of nominal capacity.
        discount = min(0.75, max(0.0, risk_rate + max(0.0, failure_rate - 0.2)))
        ceilings[platform] = {
            "sustainable_daily": base,
            "ceiling": round(base * (1.0 - discount), 2),
            "risk_discount": round(discount, 4),
        }
    return ceilings


def backpressure_recommendation(observed: dict, ceiling: dict, risk_trend: dict) -> dict:
    """Per-platform backpressure: only ever slows down as risk rises.

    ``observed``: ``{platform: {observed, ...}}``. ``ceiling``: ``{platform:
    {ceiling}}`` (same window units as ``observed``). ``risk_trend``:
    ``{platform: "rising"|"flat"|"falling"}``. Returns ``{platform: {action,
    factor, utilization, reasons}}`` with ``action`` in
    ``{scale_up, hold, throttle, pause}``.
    """
    platforms = set(observed) | set(ceiling)
    recommendations: dict[str, dict] = {}
    for platform in platforms:
        obs = float((observed.get(platform) or {}).get("observed", 0) or 0)
        ceil_bucket = ceiling.get(platform) or {}
        ceil = float(ceil_bucket.get("ceiling", 0) or 0)
        risk_rate = float((observed.get(platform) or {}).get("risk_rate", 0) or 0)
        trend = risk_trend.get(platform, "flat")
        rising = trend == "rising"

        utilization = round(_safe_rate(obs, ceil), 4) if ceil > 0 else (None if obs == 0 else 99.0)
        reasons: list[str] = []

        if ceil <= 0:
            action, factor = "pause", 0.0
            reasons.append("no_sustainable_capacity")
        elif rising and risk_rate >= RISK_RATE_HIGH:
            action, factor = "pause", 0.0
            reasons.append("risk_rising_and_high")
        elif rising:
            action, factor = "throttle", 0.5
            reasons.append("risk_trend_rising")
        elif utilization is not None and utilization >= UTIL_THROTTLE:
            action, factor = "throttle", 0.6
            reasons.append("utilization_at_or_over_ceiling")
        elif utilization is not None and utilization >= UTIL_HOLD:
            action, factor = "hold", 1.0
            reasons.append("approaching_ceiling")
        elif obs == 0:
            action, factor = "hold", 1.0
            reasons.append("no_recent_load")
        elif utilization is not None and utilization < UTIL_SCALE and risk_rate < RISK_RATE_HIGH:
            # Headroom exists and risk is stable: allow gentle scale-up, capped
            # so projected load never exceeds the ceiling.
            factor = min(SCALE_UP_FACTOR_CAP, ceil / obs if obs > 0 else 1.0)
            factor = max(1.0, round(factor, 3))
            action = "scale_up"
            reasons.append("low_utilization_risk_stable")
        else:
            action, factor = "hold", 1.0
            reasons.append("steady_state")

        recommendations[platform] = {
            "action": action,
            "factor": factor,
            "utilization": utilization,
            "risk_trend": trend,
            "reasons": reasons,
        }
    return recommendations


def saturation_alerts(observed: dict, ceiling: dict, *, threshold: float = 0.9) -> list[str]:
    """Platforms whose utilisation is at/above ``threshold`` of the ceiling."""
    alerts: list[str] = []
    for platform, bucket in observed.items():
        obs = float(bucket.get("observed", 0) or 0)
        ceil = float((ceiling.get(platform) or {}).get("ceiling", 0) or 0)
        if ceil <= 0 and obs > 0:
            alerts.append(platform)
        elif ceil > 0 and (obs / ceil) >= threshold:
            alerts.append(platform)
    return sorted(alerts, key=str)
