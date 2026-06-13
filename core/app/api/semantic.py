"""Semantic Runtime API (V9 / stage S8).

Read-only aggregation: stitch the ``Intent -> Institution -> Policy ->
Transition -> Execution`` layers — already produced by V5/V6.5 (strategy &
learning), V7 (governance) and V8 (transition) — into one self-explaining
trace for a single subject. The pure assembly and narrative live in
``app.semantic.trace``; this layer only gathers the inputs from existing
sources.

Safety: every endpoint here is read-only and advisory. It performs no writes,
adds no new decision authority, and the generated narrative/consistency hints
never feed back into ``evaluate_policy`` or the real-run gate. Missing sources
degrade to empty (fail-soft for display); this does not relax the fail-closed
gate the trace describes.
"""

from fastapi import APIRouter, HTTPException

from app.api.governance import _load_active_policy
from app.api.learning import target_predictions
from app.api.lotteries import explain_lottery_strategy, parse_json_field
from app.api.transitions import policy_lineage
from app.db import database
from app.governance.policy import REAL_RUN_GATE_POLICY_KEY
from app.semantic.trace import build_semantic_trace


router = APIRouter()


@router.get("/trace/{subject_type}/{subject_id}")
async def semantic_trace(subject_type: str, subject_id: str):
    """Return the five-layer semantic trace for a subject (currently a lottery)."""
    if subject_type != "lottery":
        raise HTTPException(400, detail="Only subject_type='lottery' is supported")
    try:
        lottery_id = int(subject_id)
    except (TypeError, ValueError):
        raise HTTPException(400, detail="subject_id must be an integer lottery id")

    lottery = await database.fetch_one(
        "SELECT id, status FROM lotteries WHERE id = :id", {"id": lottery_id}
    )
    if not lottery:
        raise HTTPException(404, detail="Lottery not found")

    intent = await _build_intent(lottery_id)
    institution = await _build_institution(REAL_RUN_GATE_POLICY_KEY)
    policy_decision = await _latest_decision(subject_id)
    transition_lineage = await policy_lineage(REAL_RUN_GATE_POLICY_KEY)
    execution = await _build_execution(lottery_id, lottery["status"])

    return build_semantic_trace(
        subject_type="lottery",
        subject_id=subject_id,
        intent=intent,
        institution=institution,
        policy_decision=policy_decision,
        transition_lineage=transition_lineage,
        execution=execution,
    )


async def _build_intent(lottery_id: int) -> dict | None:
    """Intent layer: strategy score/tier (V5) plus optional learning drivers (V6.5)."""
    try:
        strat = await explain_lottery_strategy(lottery_id)
    except HTTPException:
        return None
    intent = {
        "strategy_score": strat.get("strategy_score"),
        "priority_tier": strat.get("priority_tier"),
        "recommended_mode": strat.get("recommended_mode"),
        "value_score": strat.get("value_score"),
        "top_drivers": [],
        "probability": None,
    }
    # Advisory learning prediction is optional: a lottery only appears in
    # predictions while pending/claimed. Absence is fine — fail soft.
    try:
        preds = await target_predictions(limit=100)
        for entry in preds.get("items", []):
            if entry.get("lottery_id") == lottery_id:
                intent["top_drivers"] = entry.get("top_drivers", [])
                intent["probability"] = entry.get("probability")
                intent["model_version"] = entry.get("model_version")
                break
    except Exception:
        pass
    return intent


async def _build_institution(policy_key: str) -> dict:
    """Institution layer: which policy object is active, at which version (V7)."""
    policy = await _load_active_policy(policy_key)
    return {"policy_key": policy.get("policy_key"), "active_version": policy.get("version")}


async def _latest_decision(subject_id: str) -> dict | None:
    """Policy layer: the most recent recorded gate decision for this subject (V7)."""
    row = await database.fetch_one(
        """SELECT decision_id, policy_key, policy_version, subject_type, subject_id,
                  outcome, reasons, created_at
           FROM policy_decisions WHERE subject_id = :sid ORDER BY id DESC LIMIT 1""",
        {"sid": subject_id},
    )
    if not row:
        return None
    record = dict(row)
    record["reasons"] = parse_json_field(record.get("reasons")) or []
    return record


async def _build_execution(lottery_id: int, lottery_status) -> dict:
    """Execution layer: what actually ran for this lottery (task_runs)."""
    rows = await database.fetch_all(
        """SELECT task_id, status,
                  COALESCE(task_mode, IF(dry_run = 1, 'dry_run', 'real_run')) AS task_mode,
                  created_at, finished_at
           FROM task_runs WHERE lottery_id = :lid ORDER BY id DESC LIMIT 20""",
        {"lid": lottery_id},
    )
    runs = [dict(row) for row in rows]
    return {
        "status": lottery_status,
        "task_runs": runs,
        "count": len(runs),
        "latest_run_status": runs[0]["status"] if runs else None,
    }
