"""Pure scoring helpers for the Knowledge Runtime (V4.5).

Extracted from ``knowledge/service.py`` so the scoring math (reputation,
confidence, data maturity, learning gaps) can be unit tested without a
database connection. ``service.py`` re-exports these for backward
compatibility, and ``api/lotteries.py`` imports ``account_reputation`` from
``service`` as before.
"""

from __future__ import annotations

from typing import Any


def build_data_maturity(
    *,
    platform_profiles: list[dict[str, Any]],
    account_profiles: list[dict[str, Any]],
    risk_profile: dict[str, Any],
    lottery_profile: dict[str, Any],
    event_profile: dict[str, Any],
) -> dict[str, Any]:
    total_events = as_int(event_profile.get("total_events"))
    total_lotteries = sum(item["total_lotteries"] for item in platform_profiles)
    total_runs = sum(item["total_runs"] for item in platform_profiles)
    result_labels = sum(item["won_lotteries"] + item["lost_lotteries"] for item in platform_profiles)
    shadow_success = sum(item["shadow_success"] for item in platform_profiles)
    real_success = sum(item["real_success"] for item in platform_profiles)
    risk_events = sum(item["count"] for item in risk_profile.get("by_type", []))
    platforms_with_data = sum(
        1 for item in platform_profiles if item["total_lotteries"] or item["total_runs"] or item["event_count"]
    )
    score = clamp(
        min(total_events, 30)
        + min(result_labels * 6, 20)
        + min(total_runs * 3, 20)
        + min(shadow_success * 6, 15)
        + min(platforms_with_data * 4, 10)
        + min(risk_events * 2, 5),
        0,
        100,
    )
    if score >= 70:
        level = "learning_ready"
    elif score >= 40:
        level = "usable"
    elif score >= 15:
        level = "warming"
    else:
        level = "cold_start"
    return {
        "data_maturity_score": score,
        "data_maturity_level": level,
        "total_events": total_events,
        "total_lotteries": total_lotteries,
        "total_runs": total_runs,
        "result_labels": result_labels,
        "shadow_success": shadow_success,
        "real_success": real_success,
        "risk_events": risk_events,
        "platforms_with_data": platforms_with_data,
        "accounts_profiled": len(account_profiles),
        "value_bands_profiled": len(lottery_profile.get("by_value_band", [])),
    }


def build_learning_gaps(
    *,
    summary: dict[str, Any],
    platform_profiles: list[dict[str, Any]],
    account_profiles: list[dict[str, Any]],
    risk_profile: dict[str, Any],
    lottery_profile: dict[str, Any],
) -> list[dict[str, Any]]:
    gaps = []
    if summary["total_events"] == 0:
        gaps.append(gap("event_memory_empty", "P0", "Event Store has no recent events", "Run discovery, account calibration, dry-run, or target import so Knowledge Runtime can build history."))
    if summary["result_labels"] < 5:
        gaps.append(gap("result_labels_low", "P1", "Lottery result labels are too sparse", "Mark won/lost results after each known outcome to train future win-rate estimates."))
    if summary["shadow_success"] < 3:
        gaps.append(gap("shadow_evidence_low", "P1", "Shadow-run evidence is still thin", "Collect several successful shadow-runs before relying on real-run recommendations."))
    if not account_profiles:
        gaps.append(gap("account_profiles_empty", "P0", "No account profiles can be built", "Add accounts through QR login or Cookie import, then calibrate them."))
    cold_platforms = [
        item["platform"]
        for item in platform_profiles
        if item["total_lotteries"] < 3 and item["total_runs"] < 3
    ]
    if cold_platforms:
        gaps.append(gap("platform_samples_low", "P2", "Some platforms have low sample counts", "Import or discover more targets before comparing platform-level win or risk rates.", {"platforms": cold_platforms[:8]}))
    if summary["risk_events"] == 0:
        gaps.append(gap("risk_observations_empty", "P2", "Risk model has no recent observations", "This can mean the system is quiet or safe; keep risk event recording enabled for captcha, rate-limit, review, and ban signals."))
    if not lottery_profile.get("by_value_band"):
        gaps.append(gap("lottery_value_profile_empty", "P2", "No value-band lottery profile exists", "Import targets with value_score so the strategy layer can compare expected value bands."))
    return gaps[:8]


def gap(code: str, priority: str, title: str, detail: str, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "code": code,
        "priority": priority,
        "title": title,
        "detail": detail,
        "evidence": evidence or {},
    }


def account_reputation(
    *,
    status: str,
    risk_score: int,
    latest_calibration_status: str | None,
    total_runs: int,
    succeeded_runs: int,
    failed_runs: int,
    shadow_runs: int,
    real_runs: int,
    risk_events: int,
) -> int:
    score = 70
    score += min(succeeded_runs * 2, 12)
    score += min(shadow_runs * 2, 8)
    score += min(real_runs * 3, 9)
    score += 8 if latest_calibration_status == "succeeded" else 0
    score -= min(failed_runs * 6, 24)
    score -= min(risk_events * 12, 36)
    score -= min(risk_score, 40)
    if status in {"frozen", "banned"}:
        score -= 35
    elif status in {"login_required", "cooling"}:
        score -= 18
    elif status == "ready" and total_runs == 0:
        score += 4
    return clamp(score, 0, 100)


def confidence_score(
    *,
    total_lotteries: int,
    result_labels: int,
    task_runs: int,
    shadow_success: int,
    real_success: int,
    event_count: int,
) -> int:
    return clamp(
        min(total_lotteries * 3, 24)
        + min(result_labels * 8, 24)
        + min(task_runs * 4, 20)
        + min(shadow_success * 6, 18)
        + min(real_success * 7, 14)
        + min(event_count * 2, 10),
        0,
        100,
    )


def ratio(numerator: int, denominator: int) -> float | None:
    if not denominator:
        return None
    return round(numerator / denominator, 4)


def as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def as_float(value: Any) -> float:
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def clamp(value: int | float, low: int, high: int) -> int:
    return int(max(low, min(high, round(value))))
