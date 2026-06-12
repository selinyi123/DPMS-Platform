"""Pure decision logic for the DPMS Experiment Runtime (V5.5 / stage S3).

This module has no database, Redis, or FastAPI dependency so the A/B
experiment math (branch assignment, sample statistics, leading-branch
comparison, and safety auto-stop) can be unit tested and reasoned about in
isolation. The API layer (``core/app/api/experiments.py``) supplies plain
values and persists results.

Safety boundary (non-negotiable, mirrors the master design plan):

- Experiments only ever compare execution *strategies* (e.g. comment
  templates, time-of-day) inside ``dry_run`` / ``shadow_run``. ``real_run``
  experiments are not auto-approved here; they are flagged as requiring the
  existing admin + real-run gate and are rejected by ``validate_experiment_spec``
  unless ``allow_real_run`` is explicitly set by an admin upstream.
- Comment/content branches must draw from a controlled template pool; this
  module never generates free-form content.
- Any captcha / rate-limit / review signal must stop the affected branch
  (``should_stop_branch``); experiments never hide or bypass these signals.
"""

from __future__ import annotations

import hashlib

# Modes an experiment may run in without escalated authorization. real_run is
# intentionally excluded: a real-run experiment must pass the same admin +
# evidence gate as any other real-run dispatch.
SAFE_EXPERIMENT_MODES = ("dry_run", "shadow_run")

# A branch is auto-stopped once its observed risk rate crosses this fraction of
# its runs, or as soon as a hard risk signal (captcha / ban) is seen.
DEFAULT_RISK_RATE_THRESHOLD = 0.2
MIN_RUNS_FOR_RISK_RATE = 5

# Minimum per-branch sample before any comparison is allowed to name a leader.
DEFAULT_MIN_SAMPLES = 8


def validate_experiment_spec(
    *,
    name: str,
    platform: str,
    mode: str,
    branches: list[dict],
    supported_platforms: set[str],
    allow_real_run: bool = False,
) -> tuple[bool, list[str]]:
    """Validate an experiment definition before it is persisted.

    Returns ``(valid, errors)``. The rules are deliberately strict: an
    experiment that cannot be safely and meaningfully run should never be
    created.
    """
    errors: list[str] = []
    if not (name or "").strip():
        errors.append("experiment_name_required")
    if platform not in supported_platforms:
        errors.append("unsupported_platform")
    if mode not in SAFE_EXPERIMENT_MODES:
        if mode == "real_run" and not allow_real_run:
            errors.append("real_run_experiment_requires_admin_gate")
        elif mode != "real_run":
            errors.append("unsupported_experiment_mode")
    if not isinstance(branches, list) or len(branches) < 2:
        errors.append("at_least_two_branches_required")
        return (not errors), errors

    keys = []
    for index, branch in enumerate(branches):
        if not isinstance(branch, dict):
            errors.append(f"branch_{index}_not_object")
            continue
        key = str(branch.get("key") or "").strip()
        if not key:
            errors.append(f"branch_{index}_key_required")
        else:
            keys.append(key)
        weight = branch.get("weight", 1)
        if not isinstance(weight, (int, float)) or weight <= 0:
            errors.append(f"branch_{key or index}_weight_must_be_positive")
    if len(keys) != len(set(keys)):
        errors.append("branch_keys_must_be_unique")
    return (not errors), errors


def assign_branch(*, branches: list[dict], unit_key: str) -> str | None:
    """Deterministically assign a unit (e.g. a lottery id) to a branch.

    Uses a stable hash of ``unit_key`` mapped onto the cumulative weight
    distribution, so the same unit always lands in the same branch (no
    re-bucketing churn) while honoring branch weights.
    """
    usable = [b for b in branches if isinstance(b, dict) and b.get("key") and _weight(b) > 0]
    if not usable:
        return None
    total = sum(_weight(b) for b in usable)
    digest = hashlib.sha256(unit_key.encode("utf-8")).hexdigest()
    # 8 hex chars -> [0, 1) fraction, stable across processes and Python runs.
    fraction = int(digest[:8], 16) / 0xFFFFFFFF
    target = fraction * total
    cumulative = 0.0
    for branch in usable:
        cumulative += _weight(branch)
        if target <= cumulative:
            return str(branch["key"])
    return str(usable[-1]["key"])


def branch_stats(raw: dict) -> dict:
    """Aggregate one branch's raw counters into rates for comparison."""
    assignments = _as_int(raw.get("assignments"))
    runs = _as_int(raw.get("runs"))
    participated = _as_int(raw.get("participated"))
    won = _as_int(raw.get("won"))
    lost = _as_int(raw.get("lost"))
    failed = _as_int(raw.get("failed"))
    risk_events = _as_int(raw.get("risk_events"))
    decided = won + lost
    return {
        "key": raw.get("key"),
        "assignments": assignments,
        "runs": runs,
        "participated": participated,
        "won": won,
        "lost": lost,
        "failed": failed,
        "risk_events": risk_events,
        "participation_rate": _ratio(participated, runs),
        "win_rate": _ratio(won, decided),
        "failure_rate": _ratio(failed, runs),
        "risk_rate": _ratio(risk_events, runs),
    }


def compare_branches(branch_results: list[dict], *, min_samples: int = DEFAULT_MIN_SAMPLES) -> dict:
    """Pick a leading branch by win rate, gated on a minimum sample size.

    Conservative on purpose: with thin samples it reports ``insufficient_data``
    rather than declaring a winner, and ties keep the existing leader. This is
    a *suggestion* surface only — it never auto-promotes a branch.
    """
    stats = [branch_stats(b) for b in branch_results]
    eligible = [s for s in stats if s["runs"] >= min_samples and s["win_rate"] is not None]
    if not eligible:
        return {
            "branches": stats,
            "leader": None,
            "status": "insufficient_data",
            "min_samples": min_samples,
        }
    leader = max(
        eligible,
        key=lambda s: (s["win_rate"], s["participation_rate"] or 0, -(s["risk_rate"] or 0)),
    )
    return {
        "branches": stats,
        "leader": leader["key"],
        "leader_win_rate": leader["win_rate"],
        "status": "leader_found",
        "min_samples": min_samples,
        "eligible_branches": [s["key"] for s in eligible],
    }


def should_stop_branch(
    *,
    risk_events: int,
    runs: int,
    captcha_or_ban: bool = False,
    risk_rate_threshold: float = DEFAULT_RISK_RATE_THRESHOLD,
) -> tuple[bool, str | None]:
    """Decide whether a branch must be auto-stopped for safety.

    A hard signal (captcha / ban / review) stops the branch immediately. A
    soft signal stops it once the observed risk rate crosses the threshold,
    after a minimum number of runs to avoid stopping on a single early event.
    """
    if captcha_or_ban:
        return True, "hard_risk_signal"
    if runs >= MIN_RUNS_FOR_RISK_RATE and risk_events / runs >= risk_rate_threshold:
        return True, "risk_rate_exceeded"
    return False, None


def experiment_guardrails(*, mode: str, has_comment_branch: bool) -> list[str]:
    """Return the human-readable guardrails that apply to an experiment.

    Surfaced to operators so the safety contract is visible in the UI, not
    just enforced in code.
    """
    notes = ["captcha_or_ratelimit_stops_branch", "results_are_advisory_only"]
    if mode == "real_run":
        notes.append("real_run_requires_admin_and_evidence_gate")
    else:
        notes.append("runs_in_dry_or_shadow_only")
    if has_comment_branch:
        notes.append("comment_content_from_controlled_template_pool")
    return notes


def _weight(branch: dict) -> float:
    weight = branch.get("weight", 1)
    if isinstance(weight, (int, float)) and weight > 0:
        return float(weight)
    return 0.0


def _as_int(value) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _ratio(numerator: int, denominator: int) -> float | None:
    if not denominator:
        return None
    return round(numerator / denominator, 4)
