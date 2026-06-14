"""Pure orchestration logic for the DPMS Orchestration Runtime (V12 / stage S11).

No database or framework dependency. V12 lifts scheduling (V10) and capacity
(V11) from a single subject to a *cross-platform batch*: given a set of targets
spanning multiple platforms, it stages them into ordered **waves** that respect
per-platform capacity and a **mandatory safety ramp** — dry_run before
shadow_run before real_run, never skipping a step.

Safety boundary (non-negotiable):

- The ramp is mandatory and ordered: ``RAMP_STAGES = (dry_run, shadow_run,
  real_run)``. A target may only be placed in a ``real_run`` wave when the
  real-run gate clears it, and in a ``shadow_run`` wave when it is
  shadow-eligible. ``validate_campaign`` re-checks this against authoritative
  gate state and rejects any plan that would let a target jump the ramp — it
  mirrors the gate and can never bypass it.
- A campaign plan is an inert *draft*. This module builds and validates plans;
  it never executes a wave, dispatches a task, or activates anything.
"""

from __future__ import annotations

# Ordered safety ramp. Index = how far along the ramp a mode is; waves run in
# this order so the fleet always proves a safer mode before a riskier one.
RAMP_STAGES = ("dry_run", "shadow_run", "real_run")
RAMP_INDEX = {stage: i for i, stage in enumerate(RAMP_STAGES)}

DEFAULT_WAVE_CAPACITY = 4


def _as_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def target_stage(target: dict) -> str:
    """The highest ramp mode a target is currently cleared for.

    ``real_run`` only when the gate clears it; ``shadow_run`` only when it is
    shadow-eligible (e.g. a probe completed); otherwise ``dry_run``. This is the
    conservative floor the campaign builds on — never above what readiness
    permits.
    """
    if target.get("gate_ready"):
        return "real_run"
    if target.get("shadow_eligible"):
        return "shadow_run"
    return "dry_run"


def _wave_cap_for(platform, capacity: dict | None, wave_capacity: int | None) -> int:
    if wave_capacity is not None:
        return max(1, _as_int(wave_capacity, DEFAULT_WAVE_CAPACITY))
    if capacity:
        bucket = capacity.get(platform) or {}
        sustainable = _as_int(bucket.get("sustainable_daily"))
        if sustainable > 0:
            return sustainable
    return DEFAULT_WAVE_CAPACITY


def _chunk(items: list, size: int) -> list[list]:
    size = max(1, size)
    return [items[i:i + size] for i in range(0, len(items), size)]


def build_campaign_plan(
    *,
    targets: list[dict],
    capacity: dict | None = None,
    wave_capacity: int | None = None,
) -> dict:
    """Stage targets into ordered, capacity-bounded, ramp-respecting waves.

    ``targets``: ``[{lottery_id, platform, value_score, gate_ready,
    shadow_eligible}]``. ``capacity``: optional V11 output
    ``{platform: {sustainable_daily}}`` used to cap each wave; ``wave_capacity``
    overrides it (mainly for tests). Returns ``{waves, summary}``.
    """
    # Assign each target the conservative mode it is cleared for, grouped by
    # mode in ramp order, then by platform.
    by_mode: dict[str, dict[str, list]] = {stage: {} for stage in RAMP_STAGES}
    for target in targets:
        mode = target_stage(target)
        platform = target.get("platform")
        item = {
            "lottery_id": target.get("lottery_id"),
            "platform": platform,
            "mode": mode,
            "value_score": _as_int(target.get("value_score")),
        }
        by_mode[mode].setdefault(platform, []).append(item)

    waves: list[dict] = []
    wave_index = 0
    for mode in RAMP_STAGES:
        platform_items = by_mode[mode]
        if not platform_items:
            continue
        # Sort each platform's items by value (desc) for stable, useful staging.
        chunked_by_platform = {}
        max_chunks = 0
        for platform, items in platform_items.items():
            items.sort(key=lambda it: (-it["value_score"], _as_int(it["lottery_id"])))
            chunks = _chunk(items, _wave_cap_for(platform, capacity, wave_capacity))
            chunked_by_platform[platform] = chunks
            max_chunks = max(max_chunks, len(chunks))

        for k in range(max_chunks):
            wave_items: list[dict] = []
            platform_loads: dict[str, int] = {}
            for platform, chunks in sorted(chunked_by_platform.items(), key=lambda kv: str(kv[0])):
                if k < len(chunks):
                    wave_items.extend(chunks[k])
                    platform_loads[platform] = len(chunks[k])
            waves.append({
                "index": wave_index,
                "mode": mode,
                "ramp_step": RAMP_INDEX[mode],
                "platform_loads": platform_loads,
                "items": wave_items,
                "size": len(wave_items),
            })
            wave_index += 1

    by_mode_counts = {mode: sum(len(v) for v in by_mode[mode].values()) for mode in RAMP_STAGES}
    platforms = sorted({t.get("platform") for t in targets}, key=str)
    return {
        "waves": waves,
        "summary": {
            "total": len(targets),
            "by_mode": by_mode_counts,
            "wave_count": len(waves),
            "platforms": platforms,
            "requires_review": by_mode_counts["real_run"] > 0,
        },
    }


def validate_campaign(plan: dict, *, gate_state: dict) -> tuple[bool, list[str]]:
    """Reject any plan that lets a target jump the safety ramp.

    ``gate_state``: ``{lottery_id: {gate_ready: bool, shadow_eligible: bool}}``
    — the authoritative readiness. A ``real_run`` item requires ``gate_ready``;
    a ``shadow_run`` item requires ``shadow_eligible``. Returns ``(valid,
    errors)``. This mirrors the real-run gate; it can only ever be stricter,
    never permissive.
    """
    errors: list[str] = []
    for wave in plan.get("waves", []):
        for item in wave.get("items", []):
            lottery_id = item.get("lottery_id")
            mode = item.get("mode")
            state = gate_state.get(lottery_id) or gate_state.get(str(lottery_id)) or {}
            if mode == "real_run" and not state.get("gate_ready"):
                errors.append(f"real_run_without_gate:lottery={lottery_id}")
            elif mode == "shadow_run" and not state.get("shadow_eligible"):
                errors.append(f"shadow_run_without_probe:lottery={lottery_id}")
    return (not errors), errors


def campaign_risk_summary(plan: dict) -> dict:
    """Operator-facing summary: load per platform, ramp distribution, review flag."""
    platform_load: dict[str, int] = {}
    mode_counts: dict[str, int] = {stage: 0 for stage in RAMP_STAGES}
    for wave in plan.get("waves", []):
        mode_counts[wave.get("mode")] = mode_counts.get(wave.get("mode"), 0) + wave.get("size", 0)
        for platform, count in wave.get("platform_loads", {}).items():
            platform_load[platform] = platform_load.get(platform, 0) + count
    return {
        "platform_load": platform_load,
        "mode_counts": mode_counts,
        "real_run_items": mode_counts.get("real_run", 0),
        "requires_review": mode_counts.get("real_run", 0) > 0,
        "wave_count": len(plan.get("waves", [])),
    }
