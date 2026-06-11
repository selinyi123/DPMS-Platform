"""Pure strategy scoring engine for the DPMS Strategy Runtime (V5).

This module intentionally has no database, Redis, or FastAPI dependency so the
decision logic can be unit tested and reasoned about in isolation. All inputs
are plain values supplied by the caller (``core/app/api/lotteries.py``), which
keeps strategy decisions separated from data access.

Safety boundary: this engine only *recommends* an execution mode and ranks
targets. It never bypasses the real-run gate, hides automation, or evades
platform protection. ``real_run`` is recommended only when the upstream gate
inputs (calibrated account, adapter, probe evidence, global switch, closed
circuit breaker) are all satisfied; the API and worker still enforce the gate.
"""

from __future__ import annotations

MODE_MULTIPLIER = {
    "real_run": 1.0,
    "shadow_run": 0.78,
    "dry_run": 0.52,
    "blocked": 0.0,
}


def choose_strategy_mode(
    *,
    safe_accounts: int,
    active_runs: int,
    dry_success: int,
    shadow_success: int,
    adapter_ready: bool,
    probe_ready: bool,
    real_run_enabled: bool,
    breaker_allowed: bool,
    breaker_reason: str | None,
) -> tuple[str, list[str], list[str]]:
    """Pick the safest next execution mode for a target.

    Returns ``(mode, reason_codes, blockers)``. The precedence is deliberately
    conservative: any hard blocker short-circuits to ``blocked`` before a
    progressive validation ladder (dry-run -> shadow-run -> real-run).
    """
    reasons: list[str] = []
    blockers: list[str] = []
    if active_runs > 0:
        return "blocked", ["active_run_in_progress"], ["target already has an active run"]
    if safe_accounts <= 0:
        return "blocked", ["no_safe_account"], ["no calibrated ready account"]
    if not breaker_allowed:
        return "blocked", ["circuit_breaker_open"], [breaker_reason or "circuit breaker open"]
    if dry_success <= 0:
        return "dry_run", ["dry_validation_needed"], blockers
    if shadow_success <= 0:
        return "shadow_run", ["shadow_validation_needed"], blockers
    if adapter_ready and probe_ready and real_run_enabled:
        return "real_run", ["real_gate_ready"], blockers
    if not adapter_ready:
        reasons.append("adapter_not_ready")
    if not probe_ready:
        reasons.append("probe_not_ready")
    if not real_run_enabled:
        reasons.append("real_run_disabled")
    return "shadow_run", reasons or ["real_gate_not_ready"], blockers


def strategy_score(
    *,
    value_score: int,
    recommended_mode: str,
    dry_success: int,
    shadow_success: int,
    failed_runs: int,
    recent_risk: int,
    knowledge_confidence: int,
    estimated_win_probability: float,
    account_reputation_score: int,
    expected_value: float,
) -> float:
    """Rank a candidate target. Blocked targets always score 0."""
    if recommended_mode == "blocked":
        return 0.0
    mode_multiplier = MODE_MULTIPLIER.get(recommended_mode, 0.0)
    validation_bonus = min(dry_success, 3) * 3 + min(shadow_success, 3) * 6
    risk_penalty = min(recent_risk * 8 + failed_runs * 7, 45)
    knowledge_bonus = min(knowledge_confidence * 0.08, 8)
    account_bonus = min(max(account_reputation_score - 50, 0) * 0.16, 8)
    expected_value_bonus = min(expected_value * 10, 12)
    probability_bonus = min(estimated_win_probability * 20, 10)
    return round(
        max(
            0,
            value_score * mode_multiplier
            + validation_bonus
            + knowledge_bonus
            + account_bonus
            + expected_value_bonus
            + probability_bonus
            - risk_penalty,
        ),
        2,
    )


def estimate_win_probability(win_rate: float | None, knowledge_confidence: int) -> float:
    """Blend a conservative baseline with observed win rate by confidence."""
    baseline = 0.05
    if win_rate is None:
        return baseline
    confidence_weight = min(max(knowledge_confidence, 0) / 100, 0.75)
    return round(baseline * (1 - confidence_weight) + float(win_rate) * confidence_weight, 4)


def estimate_trust_score(
    *,
    account_reputation_score: int,
    recent_platform_risk: int,
    knowledge_confidence: int,
) -> float:
    """Combine account reputation, recent risk, and data confidence into [0.05, 1.0]."""
    account_trust = max(0.25, min(account_reputation_score, 100) / 100) if account_reputation_score else 0.35
    risk_penalty = min(recent_platform_risk * 0.08, 0.48)
    confidence_bonus = min(max(knowledge_confidence, 0) / 100 * 0.12, 0.12)
    return round(max(0.05, min(account_trust + confidence_bonus - risk_penalty, 1.0)), 4)


def strategy_knowledge_confidence(item: dict) -> int:
    """Confidence in a platform knowledge row, derived from observed sample sizes."""
    result_labels = as_int(item.get("won_lotteries")) + as_int(item.get("lost_lotteries"))
    return clamp(
        min(as_int(item.get("total_lotteries")) * 4, 28)
        + min(result_labels * 8, 24)
        + min(as_int(item.get("total_runs")) * 5, 22)
        + min(as_int(item.get("shadow_success")) * 8, 16)
        + min(as_int(item.get("real_success")) * 8, 10),
        0,
        100,
    )


def priority_tier(strategy_score_value: float) -> str:
    """Map a strategy score to an operator-facing priority tier.

    S = act first, A = strong candidate, B = low-priority/validation,
    hold = blocked or not worth dispatching now.
    """
    if strategy_score_value >= 70:
        return "S"
    if strategy_score_value >= 45:
        return "A"
    if strategy_score_value >= 20:
        return "B"
    return "hold"


def account_tier(reputation_score: int) -> str:
    """Account tier from reputation, aligned with roadmap section 6.3.

    S = high-value targets, A = medium-value, B = low-value/probe,
    watch = keep on dry-run / re-validate before trusting.
    """
    if reputation_score >= 85:
        return "S"
    if reputation_score >= 70:
        return "A"
    if reputation_score >= 50:
        return "B"
    return "watch"


def empty_platform_knowledge(platform: str) -> dict:
    return {
        "platform": platform,
        "total_lotteries": 0,
        "pending_lotteries": 0,
        "won_lotteries": 0,
        "lost_lotteries": 0,
        "high_value_lotteries": 0,
        "avg_value_score": 0,
        "win_rate": None,
        "total_runs": 0,
        "succeeded_runs": 0,
        "failed_runs": 0,
        "task_success_rate": None,
        "shadow_success": 0,
        "real_success": 0,
        "risk_events": 0,
        "knowledge_confidence": 0,
    }


def first_or_none(items):
    return items[0] if items else None


def as_int(value) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def clamp(value: int | float, low: int, high: int) -> int:
    return int(max(low, min(high, round(value))))
