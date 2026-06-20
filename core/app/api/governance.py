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

from fastapi import APIRouter, HTTPException, Request

from app.adapter_config import load_runtime_selector_config
from app.api.lotteries import parse_json_field, real_run_gate_status
from app.db import database
from app.event_store.service import record_event
from app.governance.policy import (
    DEFAULT_REAL_RUN_POLICY,
    REAL_RUN_GATE_POLICY_KEY,
    build_decision_record,
    diff_policies,
    replay_decision,
    validate_policy,
)
from app.models.schemas import PolicyActivateRequest, PolicyPublishRequest
from app.security import (
    audit_event,
    circuit_breaker_allows,
    is_real_run_enabled,
    require_confirmation,
    require_min_role,
)
from app.services.real_run_gate import gate_inputs, load_active_policy, record_policy_decision
from app.transition.engine import classify_transition, impact_scope, validate_transition


router = APIRouter()


@router.get("/policy")
async def active_policy(policy_key: str = REAL_RUN_GATE_POLICY_KEY):
    return await load_active_policy(policy_key)


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


@router.post("/policies")
async def publish_policy_version(data: PolicyPublishRequest, request: Request):
    """Publish a new policy version as a recorded transition (V8 / S7).

    Publishing never changes which version is active: ``evaluate_policy``
    keeps using the current active version until a separate, explicit
    ``activate`` call. Every publish is recorded as a ``policy_transitions``
    row; loosening changes (removing or weakening a required gate) require a
    written ``loosening_justification`` and are flagged for extra review.
    """
    actor = require_min_role(request, "admin")
    require_confirmation(request)

    to_policy = data.definition
    valid, errors = validate_policy(to_policy)
    if not valid:
        raise HTTPException(400, detail={"message": "invalid policy definition", "errors": errors})
    if to_policy.get("policy_key") != data.policy_key:
        raise HTTPException(400, detail={"message": "policy_key mismatch between request and definition"})

    from_policy = await load_active_policy(data.policy_key)
    valid, errors = validate_transition(
        from_policy=from_policy,
        to_policy=to_policy,
        reason_code=data.reason_code,
        rollback_condition=data.rollback_condition,
        loosening_justification=data.loosening_justification,
    )
    if not valid:
        raise HTTPException(400, detail={"message": "invalid transition", "errors": errors})

    existing = await database.fetch_one(
        "SELECT 1 FROM policy_versions WHERE policy_key = :pk AND version = :v",
        {"pk": data.policy_key, "v": to_policy["version"]},
    )
    if existing:
        raise HTTPException(409, detail=f"policy_key={data.policy_key} version={to_policy['version']} already published")

    diff = diff_policies(from_policy, to_policy)
    classification = classify_transition(diff)
    scope = impact_scope(data.policy_key, diff)
    requires_extra_review = classification == "loosening"

    await database.execute(
        """INSERT INTO policy_versions (policy_key, version, definition, note, active, created_by)
           VALUES (:policy_key, :version, :definition, :note, 0, :created_by)""",
        {
            "policy_key": data.policy_key,
            "version": to_policy["version"],
            "definition": json.dumps(to_policy, ensure_ascii=False),
            "note": data.note,
            "created_by": actor["actor_id"],
        },
    )
    await database.execute(
        """INSERT INTO policy_transitions
             (policy_key, from_version, to_version, reason_code, classification, diff, impact_scope,
              rollback_condition, loosening_justification, requires_extra_review, created_by)
           VALUES (:policy_key, :from_version, :to_version, :reason_code, :classification, :diff, :impact_scope,
                   :rollback_condition, :loosening_justification, :requires_extra_review, :created_by)""",
        {
            "policy_key": data.policy_key,
            "from_version": from_policy["version"],
            "to_version": to_policy["version"],
            "reason_code": data.reason_code,
            "classification": classification,
            "diff": json.dumps(diff, ensure_ascii=False),
            "impact_scope": json.dumps(scope, ensure_ascii=False),
            "rollback_condition": data.rollback_condition,
            "loosening_justification": data.loosening_justification,
            "requires_extra_review": int(requires_extra_review),
            "created_by": actor["actor_id"],
        },
    )
    await audit_event(
        request,
        action="governance.policy_publish",
        resource_type="policy_version",
        resource_id=f"{data.policy_key}:{to_policy['version']}",
        result="published",
        risk_level="high" if requires_extra_review else "medium",
        detail={"reason_code": data.reason_code, "classification": classification, "diff": diff},
    )
    await record_event(
        aggregate="policy",
        aggregate_id=data.policy_key,
        event_type="PolicyVersionPublished",
        payload={
            "from_version": from_policy["version"],
            "to_version": to_policy["version"],
            "reason_code": data.reason_code,
            "classification": classification,
            "requires_extra_review": requires_extra_review,
        },
        actor_type="operator",
        actor_id=actor["actor_id"],
    )
    return {
        "status": "published",
        "policy_key": data.policy_key,
        "from_version": from_policy["version"],
        "to_version": to_policy["version"],
        "active": False,
        "classification": classification,
        "diff": diff,
        "impact_scope": scope,
        "requires_extra_review": requires_extra_review,
    }


@router.post("/policies/{version}/activate")
async def activate_policy_version(version: int, data: PolicyActivateRequest, request: Request, policy_key: str = REAL_RUN_GATE_POLICY_KEY):
    """Activate a previously published policy version (V8 / S7).

    Activation is a separate, explicit, admin-confirmed step from publishing:
    only this call changes which version ``evaluate_policy`` uses going
    forward. Requires the version to already have a ``policy_transitions``
    record (i.e. it was published via ``POST /policies`` and passed
    validation), so activation can never bypass the transition audit trail.
    """
    actor = require_min_role(request, "admin")
    require_confirmation(request)

    target = await database.fetch_one(
        "SELECT * FROM policy_versions WHERE policy_key = :pk AND version = :v",
        {"pk": policy_key, "v": version},
    )
    if not target:
        raise HTTPException(404, detail="Policy version not found")

    transition = await database.fetch_one(
        "SELECT id FROM policy_transitions WHERE policy_key = :pk AND to_version = :v",
        {"pk": policy_key, "v": version},
    )
    if not transition:
        raise HTTPException(409, detail="Version has no transition record; publish via POST /policies first")

    if target["active"]:
        return {"status": "already_active", "policy_key": policy_key, "version": version}

    current_active = await database.fetch_one(
        "SELECT version FROM policy_versions WHERE policy_key = :pk AND active = 1 ORDER BY version DESC LIMIT 1",
        {"pk": policy_key},
    )
    previous_version = current_active["version"] if current_active else None

    await database.execute(
        "UPDATE policy_versions SET active = 0 WHERE policy_key = :pk AND active = 1",
        {"pk": policy_key},
    )
    await database.execute(
        "UPDATE policy_versions SET active = 1 WHERE policy_key = :pk AND version = :v",
        {"pk": policy_key, "v": version},
    )
    await database.execute(
        "UPDATE policy_transitions SET activated_at = NOW() WHERE id = :id",
        {"id": transition["id"]},
    )
    await audit_event(
        request,
        action="governance.policy_activate",
        resource_type="policy_version",
        resource_id=f"{policy_key}:{version}",
        result="activated",
        risk_level="high",
        detail={"previous_version": previous_version, "note": data.note},
    )
    await record_event(
        aggregate="policy",
        aggregate_id=policy_key,
        event_type="PolicyVersionActivated",
        payload={"version": version, "previous_version": previous_version, "note": data.note},
        actor_type="operator",
        actor_id=actor["actor_id"],
        critical=True,
    )
    return {"status": "activated", "policy_key": policy_key, "version": version, "previous_version": previous_version}


@router.get("/real-run/{lottery_id}")
async def evaluate_real_run_policy(lottery_id: int, record: bool = True):
    lottery = await database.fetch_one("SELECT * FROM lotteries WHERE id = :id", {"id": lottery_id})
    if not lottery:
        raise HTTPException(404, detail="Lottery not found")

    policy = await load_active_policy(REAL_RUN_GATE_POLICY_KEY)
    selector_config = await load_runtime_selector_config()
    real_run_enabled = await is_real_run_enabled()
    gate = await real_run_gate_status(lottery, selector_config=selector_config, real_run_enabled=real_run_enabled)
    breaker_allowed, _ = await circuit_breaker_allows(lottery["platform"])

    inputs = gate_inputs(gate, breaker_allowed=breaker_allowed)
    decision = build_decision_record(
        policy=policy, inputs=inputs, subject_type="lottery", subject_id=str(lottery_id)
    )

    decision_id = None
    if record:
        decision_id = str(uuid.uuid4())
        await record_policy_decision(
            decision_id=decision_id,
            policy=policy,
            inputs=inputs,
            decision=decision,
            subject_id=str(lottery_id),
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
    policy = await load_active_policy(record["policy_key"])
    result = replay_decision(policy=policy, record=record)
    return {
        "decision_id": decision_id,
        "subject_id": record["subject_id"],
        **result,
    }


