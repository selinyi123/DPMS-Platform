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

from app.services.real_run_gate import load_active_policy
from app.api.learning import target_predictions
from app.api.lotteries import explain_lottery_strategy, parse_json_field
from app.api.risk_intel import account_risk_intelligence
from app.api.transitions import policy_lineage
from app.db import database
from app.governance.policy import REAL_RUN_GATE_POLICY_KEY
from app.semantic.trace import build_semantic_trace
from app.strategy.engine import account_tier


router = APIRouter()

# Subject types the trace can assemble today. Institution and transition are
# subject-agnostic (the same real-run-gate policy object and its lineage
# govern every subject); intent and execution differ: a lottery gets its own
# strategy/learning intent and task history, an account gets its V6
# risk-intelligence intent and the task runs it performed, and a task is a
# single task_runs row whose intent and gate decision are inherited from the
# lottery it ran against.
SUPPORTED_SUBJECTS = {"lottery", "account", "task"}


@router.get("/trace/{subject_type}/{subject_id}")
async def semantic_trace(subject_type: str, subject_id: str):
    """Return the five-layer semantic trace for a lottery, account or task."""
    if subject_type not in SUPPORTED_SUBJECTS:
        supported = ", ".join(sorted(SUPPORTED_SUBJECTS))
        raise HTTPException(400, detail=f"subject_type must be one of: {supported}")

    # Institution and transition are subject-agnostic: the same policy object
    # and its lineage govern every subject under this real-run gate.
    institution = await _build_institution(REAL_RUN_GATE_POLICY_KEY)
    transition_lineage = await policy_lineage(REAL_RUN_GATE_POLICY_KEY)

    if subject_type == "task":
        # task_id is a UUID string, not an integer id.
        intent, policy_decision, execution = await _build_task_layers(subject_id)
    else:
        try:
            numeric_id = int(subject_id)
        except (TypeError, ValueError):
            raise HTTPException(400, detail="subject_id must be an integer id")

        policy_decision = await _latest_decision(subject_type, subject_id)
        if subject_type == "lottery":
            lottery = await database.fetch_one(
                "SELECT id, status FROM lotteries WHERE id = :id", {"id": numeric_id}
            )
            if not lottery:
                raise HTTPException(404, detail="Lottery not found")
            intent = await _build_intent(numeric_id)
            execution = await _build_execution(numeric_id, lottery["status"])
        else:  # account
            account = await database.fetch_one(
                "SELECT id, status FROM accounts WHERE id = :id", {"id": numeric_id}
            )
            if not account:
                raise HTTPException(404, detail="Account not found")
            intent = await _build_account_intent(numeric_id)
            execution = await _build_account_execution(numeric_id, account["status"])

    return build_semantic_trace(
        subject_type=subject_type,
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


async def _build_account_intent(account_id: int) -> dict | None:
    """Intent layer for an account: reputation/tier and 24h risk forecast (V6)."""
    try:
        intel = await account_risk_intelligence(account_id)
    except HTTPException:
        return None
    reputation = intel.get("reputation_score")
    return {
        "reputation_score": reputation,
        "account_tier": account_tier(reputation) if isinstance(reputation, int) else None,
        "platform": intel.get("platform"),
        "account_status": intel.get("status"),
        "forecast_band": intel.get("forecast_band"),
        "forecast_24h": intel.get("forecast_24h"),
        "recommended_action": intel.get("recommended_action"),
        # An account has no learning drivers; keep the key so callers/UI are uniform.
        "top_drivers": [],
    }


async def _build_institution(policy_key: str) -> dict:
    """Institution layer: which policy object is active, at which version (V7)."""
    policy = await load_active_policy(policy_key)
    return {"policy_key": policy.get("policy_key"), "active_version": policy.get("version")}


async def _latest_decision(subject_type: str, subject_id: str) -> dict | None:
    """Policy layer: the most recent recorded gate decision for this subject (V7).

    Filters on both ``subject_type`` and ``subject_id`` so an account and a
    lottery that happen to share a numeric id never pick up each other's
    decision.
    """
    row = await database.fetch_one(
        """SELECT decision_id, policy_key, policy_version, subject_type, subject_id,
                  outcome, reasons, created_at
           FROM policy_decisions
           WHERE subject_type = :stype AND subject_id = :sid
           ORDER BY id DESC LIMIT 1""",
        {"stype": subject_type, "sid": subject_id},
    )
    if not row:
        return None
    record = dict(row)
    record["reasons"] = parse_json_field(record.get("reasons")) or []
    return record


async def _decision_by_id(decision_id: str) -> dict | None:
    """Fetch one recorded gate decision by its id (the task-bound decision)."""
    row = await database.fetch_one(
        """SELECT decision_id, policy_key, policy_version, subject_type, subject_id,
                  outcome, reasons, created_at
           FROM policy_decisions WHERE decision_id = :did""",
        {"did": decision_id},
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


async def _build_account_execution(account_id: int, account_status) -> dict:
    """Execution layer for an account: the task runs it actually performed."""
    rows = await database.fetch_all(
        """SELECT task_id, status, lottery_id,
                  COALESCE(task_mode, IF(dry_run = 1, 'dry_run', 'real_run')) AS task_mode,
                  created_at, finished_at
           FROM task_runs WHERE account_id = :aid ORDER BY id DESC LIMIT 20""",
        {"aid": account_id},
    )
    runs = [dict(row) for row in rows]
    return {
        "status": account_status,
        "task_runs": runs,
        "count": len(runs),
        "latest_run_status": runs[0]["status"] if runs else None,
    }


async def _build_task_layers(task_id: str):
    """Intent, policy and execution layers for a single task run.

    A task has no strategy/risk record of its own: its intent and gate
    decision are inherited from the lottery it ran against, while its
    execution layer is that one ``task_runs`` row.
    """
    row = await database.fetch_one(
        """SELECT task_id, lottery_id, account_id, status,
                  COALESCE(task_mode, IF(dry_run = 1, 'dry_run', 'real_run')) AS task_mode,
                  decision_id, policy_version,
                  created_at, started_at, finished_at
           FROM task_runs WHERE task_id = :tid""",
        {"tid": task_id},
    )
    if not row:
        raise HTTPException(404, detail="Task run not found")
    run = dict(row)
    intent = await _build_intent(run["lottery_id"])
    # Prefer the exact gate decision bound to this task at dispatch (P1-4); fall
    # back to the lottery's latest decision for legacy tasks with no binding.
    policy_decision = None
    if run.get("decision_id"):
        policy_decision = await _decision_by_id(run["decision_id"])
    if policy_decision is None:
        policy_decision = await _latest_decision("lottery", str(run["lottery_id"]))
    execution = {
        "status": run["status"],
        "task_runs": [run],
        "count": 1,
        "latest_run_status": run["status"],
    }
    return intent, policy_decision, execution
