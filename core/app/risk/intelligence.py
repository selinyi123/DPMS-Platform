"""Pure risk-forecasting logic for the DPMS Risk Intelligence layer (V6 / S4).

No database, Redis, or FastAPI dependency: given plain account counters the
engine produces a current risk score, a conservative 24h forecast, and a
recommended action drawn from a fixed safe set. The API layer
(``core/app/api/risk_intel.py``) supplies the counters.

Safety boundary (non-negotiable):

- The recommended action is restricted to ``RECOMMENDED_ACTIONS``; the engine
  can never invent a permissive action.
- The forecast is only ever used to *tighten* dispatch. ``tighten_action``
  guarantees the engine can make a decision more conservative but never more
  permissive, and the forecast must never be used to relax the real-run gate.
"""

from __future__ import annotations

# Ordered least-restrictive -> most-restrictive. The index doubles as a
# severity rank so two actions can be compared and the stricter one chosen.
RECOMMENDED_ACTIONS = (
    "allow",
    "dry_run_only",
    "cooldown",
    "relogin",
    "manual_review",
    "block",
)
_ACTION_RANK = {action: rank for rank, action in enumerate(RECOMMENDED_ACTIONS)}


def tighten_action(baseline: str, recommended: str) -> str:
    """Return the more conservative (higher-severity) of two safe actions.

    This is the core safety primitive: risk intelligence may only ever move a
    decision toward caution, so callers combine their baseline action with the
    forecast's recommendation through this function.
    """
    base_rank = _ACTION_RANK.get(baseline, 0)
    rec_rank = _ACTION_RANK.get(recommended, 0)
    return RECOMMENDED_ACTIONS[max(base_rank, rec_rank)]


def forecast_account_risk(
    *,
    reputation_score: int,
    risk_events_24h: int,
    risk_events_7d: int,
    status: str,
    latest_calibration_status: str | None,
    consecutive_failures: int,
    daily_task_count: int,
    hard_signal_24h: bool = False,
) -> dict:
    """Forecast an account's near-term risk and recommend a safe action.

    Returns ``risk_score`` (0-100, higher = riskier), ``forecast_24h`` (a
    probability in [0, 1] that the account trips a risk signal in the next 24
    hours), ``recommended_action`` from ``RECOMMENDED_ACTIONS``, and the
    ``reasons`` that drove both.
    """
    reasons: list[str] = []

    # --- current risk score (higher = riskier) ---
    score = max(0, 100 - _clamp(reputation_score, 0, 100))  # low reputation -> high base risk
    score += min(risk_events_24h * 18, 54)
    score += min(max(risk_events_7d - risk_events_24h, 0) * 6, 24)
    score += min(max(consecutive_failures, 0) * 7, 21)
    if hard_signal_24h:
        score += 30
        reasons.append("hard_risk_signal_24h")
    if status in {"frozen", "banned"}:
        score += 40
        reasons.append("account_locked_state")
    elif status in {"login_required"}:
        score += 18
        reasons.append("login_required_state")
    elif status in {"cooling"}:
        score += 12
        reasons.append("cooling_state")
    if latest_calibration_status and latest_calibration_status != "succeeded":
        score += 8
        reasons.append("calibration_not_succeeded")
    if daily_task_count >= 8:
        reasons.append("daily_volume_high")
        score += 6
    risk_score = _clamp(score, 0, 100)

    if risk_events_24h:
        reasons.append("recent_risk_events")
    if consecutive_failures >= 3:
        reasons.append("consecutive_failures")
    if reputation_score < 50:
        reasons.append("low_reputation")

    # --- conservative 24h forecast (probability of a risk signal) ---
    # Anchored low and nudged up by recent density; capped below certainty so a
    # forecast never reads as a guaranteed outcome.
    forecast = 0.04
    forecast += min(risk_events_24h * 0.22, 0.6)
    forecast += min(max(risk_events_7d - risk_events_24h, 0) * 0.04, 0.18)
    forecast += min(max(consecutive_failures, 0) * 0.05, 0.15)
    if hard_signal_24h:
        forecast += 0.25
    if status in {"frozen", "banned"}:
        forecast += 0.3
    forecast_24h = round(min(forecast, 0.95), 4)

    recommended_action = _recommend_action(
        status=status,
        risk_events_24h=risk_events_24h,
        hard_signal_24h=hard_signal_24h,
        consecutive_failures=consecutive_failures,
        reputation_score=reputation_score,
        latest_calibration_status=latest_calibration_status,
    )

    return {
        "risk_score": risk_score,
        "forecast_24h": forecast_24h,
        "recommended_action": recommended_action,
        "reasons": _dedupe(reasons),
        "forecast_band": _forecast_band(forecast_24h),
    }


def _recommend_action(
    *,
    status: str,
    risk_events_24h: int,
    hard_signal_24h: bool,
    consecutive_failures: int,
    reputation_score: int,
    latest_calibration_status: str | None,
) -> str:
    """Map account state to the safest applicable action.

    Evaluated most-severe-first so the strictest applicable rule wins, which
    keeps the recommendation conservative by construction.
    """
    if status == "banned":
        return "block"
    if status == "frozen":
        return "manual_review"
    if hard_signal_24h or risk_events_24h >= 3:
        return "manual_review"
    if status == "login_required":
        return "relogin"
    if risk_events_24h >= 1:
        return "cooldown"
    if latest_calibration_status and latest_calibration_status != "succeeded":
        return "relogin"
    if consecutive_failures >= 3 or reputation_score < 50:
        return "dry_run_only"
    return "allow"


def _forecast_band(forecast_24h: float) -> str:
    if forecast_24h >= 0.5:
        return "high"
    if forecast_24h >= 0.2:
        return "elevated"
    if forecast_24h >= 0.08:
        return "moderate"
    return "low"


def _clamp(value: int | float, low: int, high: int) -> int:
    return int(max(low, min(high, round(value))))


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))
