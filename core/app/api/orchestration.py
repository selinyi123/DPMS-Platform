"""Orchestration Runtime API (V12 / stage S11).

Read-only generation (+ inert draft persistence) of cross-platform campaign
plans: stage candidate lotteries into ordered, capacity-bounded waves that
respect the mandatory dry->shadow->real safety ramp. The pure planning lives in
``app.orchestration.engine``; this layer gathers candidates, their *already
recorded* readiness, and current capacity.

Safety: a target is only marked ``real_run`` when the real-run gate has
*already* recorded an ``allow`` for it (V7 ``policy_decisions``), and only
``shadow_run`` when a shadow run already succeeded — the campaign never
re-evaluates the gate, it only stages what is already cleared. ``POST
/campaign/draft`` regenerates the plan server-side and stores it as an inert
``status='draft'`` row for audit; it never executes a wave or activates
anything. Adopting a plan still goes through the existing gated dispatch flow.
"""

import json

from fastapi import APIRouter

from app.api.capacity import _load_accounts, _load_proxies
from app.capacity.engine import compute_capacity
from app.db import database
from app.orchestration.engine import (
    build_campaign_plan,
    campaign_risk_summary,
    validate_campaign,
)

router = APIRouter()

CANDIDATE_STATUSES = ("pending", "claimed")


@router.get("/campaign")
async def generate_campaign(platform: str | None = None):
    """Generate (without persisting) a staged cross-platform campaign plan."""
    targets, gate_state = await _load_targets(platform)
    capacity = await _current_capacity()
    plan = build_campaign_plan(targets=targets, capacity=capacity["per_platform"])
    valid, errors = validate_campaign(plan, gate_state=gate_state)
    return {
        "plan": plan,
        "risk_summary": campaign_risk_summary(plan),
        "validation": {"valid": valid, "errors": errors},
        "target_count": len(targets),
    }


@router.post("/campaign/draft")
async def persist_campaign_draft(payload: dict | None = None):
    """Persist a server-regenerated plan as an inert audit draft (never runs)."""
    payload = payload or {}
    platform = payload.get("platform")
    campaign_key = str(payload.get("campaign_key") or "adhoc")[:64]

    targets, gate_state = await _load_targets(platform)
    capacity = await _current_capacity()
    plan = build_campaign_plan(targets=targets, capacity=capacity["per_platform"])
    valid, errors = validate_campaign(plan, gate_state=gate_state)
    summary = campaign_risk_summary(plan)

    result = await database.execute(
        """INSERT INTO campaign_plans
             (campaign_key, platform_scope, plan, status, waves, requires_review, created_by)
           VALUES (:key, :scope, :plan, 'draft', :waves, :review, 'admin')""",
        {
            "key": campaign_key,
            "scope": json.dumps(plan["summary"].get("platforms", []), ensure_ascii=False),
            "plan": json.dumps(plan, ensure_ascii=False),
            "waves": plan["summary"].get("wave_count", 0),
            "review": 1 if summary.get("requires_review") else 0,
        },
    )
    return {
        "status": "draft_saved",
        "draft_id": result,
        "campaign_key": campaign_key,
        "waves": plan["summary"].get("wave_count", 0),
        "validation": {"valid": valid, "errors": errors},
        "note": "Draft is inert and never auto-executes; adopt via the gated dispatch flow.",
    }


@router.get("/drafts")
async def list_campaign_drafts(limit: int = 20):
    """List recent persisted campaign drafts (audit view)."""
    limit = max(1, min(int(limit), 100))
    rows = await database.fetch_all(
        """SELECT id, campaign_key, platform_scope, status, waves, requires_review, created_by, created_at
           FROM campaign_plans ORDER BY id DESC LIMIT :limit""",
        {"limit": limit},
    )
    items = []
    for row in rows:
        item = dict(row)
        try:
            item["platform_scope"] = json.loads(item["platform_scope"]) if item.get("platform_scope") else []
        except (TypeError, ValueError):
            item["platform_scope"] = []
        item["requires_review"] = bool(item.get("requires_review"))
        items.append(item)
    return {"items": items, "count": len(items)}


async def _load_targets(platform: str | None):
    """Candidate lotteries with readiness derived purely from recorded data.

    ``gate_ready`` is deliberately strict (P0-4): a single historical ``allow``
    is NOT enough to stage a real_run wave. The *latest* recorded gate decision
    for the lottery must be ``allow``, made under the *currently active* policy
    version, and within the last 24h — so a subsequent ``block``, a policy
    bump, or a stale decision all correctly drop the target out of real_run.
    (Full live re-evaluation of the gate per target is the planned next step;
    this latest-valid-decision check is the conservative interim that never
    over-permits.)
    """
    placeholders = ", ".join(f":s{i}" for i in range(len(CANDIDATE_STATUSES)))
    values: dict = {f"s{i}": status for i, status in enumerate(CANDIDATE_STATUSES)}
    where = (
        f"l.status IN ({placeholders}) "
        "AND (l.expires_at IS NULL OR l.expires_at > UTC_TIMESTAMP())"
    )
    if platform:
        where += " AND l.platform = :platform"
        values["platform"] = platform

    active_row = await database.fetch_one(
        "SELECT version FROM policy_versions WHERE policy_key = 'real_run_gate' AND active = 1 "
        "ORDER BY version DESC LIMIT 1"
    )
    # No active policy => no target can be gate_ready (fail-closed).
    values["active_version"] = active_row["version"] if active_row else None

    rows = await database.fetch_all(
        f"""SELECT l.id, l.platform, l.value_score,
                   EXISTS(SELECT 1 FROM task_runs tr
                          WHERE tr.lottery_id = l.id AND tr.task_mode = 'shadow_run'
                            AND tr.status = 'succeeded') AS shadow_eligible,
                   EXISTS(SELECT 1 FROM policy_decisions pd
                          WHERE pd.subject_type = 'lottery'
                            AND pd.subject_id = CAST(l.id AS CHAR)
                            AND pd.policy_key = 'real_run_gate'
                            AND pd.id = (SELECT MAX(pd2.id) FROM policy_decisions pd2
                                         WHERE pd2.subject_type = 'lottery'
                                           AND pd2.subject_id = CAST(l.id AS CHAR)
                                           AND pd2.policy_key = 'real_run_gate')
                            AND pd.outcome = 'allow'
                            AND pd.policy_version = :active_version
                            AND pd.created_at >= (UTC_TIMESTAMP() - INTERVAL 24 HOUR)) AS gate_ready
            FROM lotteries l
            WHERE {where}
            ORDER BY l.value_score DESC, l.id ASC LIMIT 500""",
        values,
    )
    targets = []
    gate_state = {}
    for row in rows:
        gate_ready = bool(row["gate_ready"])
        shadow_eligible = bool(row["shadow_eligible"])
        targets.append({
            "lottery_id": row["id"],
            "platform": row["platform"],
            "value_score": int(row["value_score"] or 0),
            "gate_ready": gate_ready,
            "shadow_eligible": shadow_eligible,
        })
        gate_state[row["id"]] = {"gate_ready": gate_ready, "shadow_eligible": shadow_eligible}
    return targets, gate_state


async def _current_capacity() -> dict:
    accounts = await _load_accounts()
    proxies = await _load_proxies()
    return compute_capacity(accounts, proxies)
