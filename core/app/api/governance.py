"""Governance Runtime API (V7 / stage S6).

Surfaces the Policy Object: the active real-run policy, policy-based evaluation
of a real lottery's gate, a log of recorded decisions, and decision replay. All
logic lives in the pure module ``app.governance.policy``; this layer loads the
active policy version, gathers the real gate inputs, and persists decisions.

Safety: governance is fail-closed and read-mostly. Evaluating or replaying a
decision never dispatches anything and never relaxes the real-run gate — it
makes the existing gate auditable and versioned.
"""

import json
import uuid

from fastapi import APIRouter, HTTPException

from app.adapter_config import load_runtime_selector_config
from app.api.lotteries import parse_json_field, real_run_gate_status
from app.db import database
from app.governance.policy import (
    DEFAULT_REAL_RUN_POLICY,
    REAL_RUN_GATE_POLICY_KEY,
    build_decision_record,
    replay_decision,
)
from app.security import circuit_breaker_allows, is_real_run_enabled


router = APIRouter()


@router.get("/policy")
async def active_policy(policy_key: str = REAL_RUN_GATE_POLICY_KEY):
    return await _load_active_policy(policy_key)


@router.get("/policies")
async def list_policy_versions(policy_key: str = REAL_RUN_GATE_POLICY_KEY):
    rows = await database.fetch_all(
        "SELECT policy_key, version, note, active, created_at FROM policy_versions WHERE policy_key = :pk ORDER BY version DESC",
        {"pk": policy_key},
    )
    versions = [dict(row) for row in rows]
    if not versions:
        versions = [
            {
                "policy_key": REAL_RUN_GATE_POLICY_KEY,
                "version": DEFAULT_REAL_RUN_POLICY["version"],
                "note": "built-in default",
                "active": 1,
                "created_at": None,
            }
        ]
    return {"items": versions, "count": len(versions)}


@router.get("/real-run/{lottery_id}")
async def evaluate_real_run_policy(lottery_id: int, record: bool = True):
    lottery = await database.fetch_one("SELECT * FROM lotteries WHERE id = :id", {"id": lottery_id})
    if not lottery:
        raise HTTPException(404, detail="Lottery not found")

    policy = await _load_active_policy(REAL_RUN_GATE_POLICY_KEY)
    selector_config = await load_runtime_selector_config()
    real_run_enabled = await is_real_run_enabled()
    gate = await real_run_gate_status(lottery, selector_config=selector_config, real_run_enabled=real_run_enabled)
    breaker_allowed, _ = await circuit_breaker_allows(lottery["platform"])

    inputs = _gate_inputs(gate, breaker_allowed=breaker_allowed)
    decision = build_decision_record(
        policy=policy, inputs=inputs, subject_type="lottery", subject_id=str(lottery_id)
    )

    decision_id = None
    if record:
        decision_id = str(uuid.uuid4())
        await database.execute(
            """INSERT INTO policy_decisions
                 (decision_id, policy_key, policy_version, subject_type, subject_id, inputs, outcome, reasons)
               VALUES (:decision_id, :policy_key, :policy_version, 'lottery', :subject_id, :inputs, :outcome, :reasons)""",
            {
                "decision_id": decision_id,
                "policy_key": policy["policy_key"],
                "policy_version": policy["version"],
                "subject_id": str(lottery_id),
                "inputs": json.dumps(inputs, ensure_ascii=False),
                "outcome": decision["outcome"],
                "reasons": json.dumps(decision["reasons"], ensure_ascii=False),
            },
        )
    return {"decision_id": decision_id, "policy_version": policy["version"], **decision}


@router.get("/decisions")
async def list_decisions(limit: int = 50, subject_id: str | None = None):
    where = ""
    values = {"limit": min(max(limit, 1), 200)}
    if subject_id is not None:
        where = "WHERE subject_id = :subject_id"
        values["subject_id"] = subject_id
    rows = await database.fetch_all(
        f"""SELECT decision_id, policy_key, policy_version, subject_type, subject_id, outcome, reasons, created_at
            FROM policy_decisions {where}
            ORDER BY id DESC LIMIT :limit""",
        values,
    )
    items = []
    for row in rows:
        item = dict(row)
        item["reasons"] = parse_json_field(item.get("reasons"))
        items.append(item)
    return {"items": items, "count": len(items)}


@router.post("/decisions/{decision_id}/replay")
async def replay_recorded_decision(decision_id: str):
    row = await database.fetch_one(
        "SELECT * FROM policy_decisions WHERE decision_id = :did", {"did": decision_id}
    )
    if not row:
        raise HTTPException(404, detail="Decision not found")
    record = dict(row)
    record["inputs"] = parse_json_field(record.get("inputs")) or {}
    policy = await _load_active_policy(record["policy_key"])
    result = replay_decision(policy=policy, record=record)
    return {
        "decision_id": decision_id,
        "subject_id": record["subject_id"],
        **result,
    }


def _gate_inputs(gate: dict, *, breaker_allowed: bool) -> dict:
    """Map the existing real-run gate status onto policy gate codes."""
    blockers = set(gate.get("blockers") or [])
    return {
        "global_real_run_enabled": bool(gate.get("real_run_enabled")),
        "circuit_breaker_closed": bool(breaker_allowed),
        "valid_lottery_target": bool(gate.get("target_valid")),
        "action_plan_reviewed": bool(gate.get("action_plan_ready")),
        "calibrated_account_available": int(gate.get("safe_accounts") or 0) > 0,
        "real_adapter_enabled": bool(gate.get("adapter_enabled")),
        "recent_complete_probe": bool(gate.get("probe_ready")),
        "recent_shadow_run": bool(gate.get("shadow_ready")),
        "no_recent_account_risk": "recent_account_risk_event" not in blockers,
    }


async def _load_active_policy(policy_key: str) -> dict:
    """Load the active policy version from the DB, falling back to the default."""
    row = await database.fetch_one(
        "SELECT definition FROM policy_versions WHERE policy_key = :pk AND active = 1 ORDER BY version DESC LIMIT 1",
        {"pk": policy_key},
    )
    if row and row["definition"]:
        definition = parse_json_field(row["definition"])
        if isinstance(definition, dict):
            return definition
    if policy_key == REAL_RUN_GATE_POLICY_KEY:
        return DEFAULT_REAL_RUN_POLICY
    raise HTTPException(404, detail="Policy not found")
