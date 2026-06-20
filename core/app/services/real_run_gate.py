"""The single authoritative real-run gate (P1-4).

Before this, two gates coexisted: ``validate_real_run_evidence`` (called inline
by dispatch) and the Governance policy decision (``evaluate_policy`` behind the
Governance API). They shared intent but were not the same authority — dispatch
could proceed on the evidence check while the recorded policy decision was only
an audit artefact.

This module makes the Governance policy the authority that dispatch must clear:

    1. build the account-specific gate inputs
    2. load the active policy version
    3. evaluate_policy (fail-closed)
    4. record a policy_decisions row
    5. only outcome == "allow" may dispatch
    6. the task binds the decision_id / policy_version

Because ``real_run_gate_status`` already wraps ``validate_real_run_evidence`` and
adds the global/breaker/adapter/account inputs, the policy gate is a strict
superset of the old evidence gate — unifying onto it can only ever be as strict
or stricter, never looser.
"""

import json
import uuid

from app.adapter_config import load_runtime_selector_config
from app.db import database
from app.governance.policy import (
    DEFAULT_REAL_RUN_POLICY,
    REAL_RUN_GATE_POLICY_KEY,
    build_decision_record,
    gate_codes_from_reasons,
)
from app.security import circuit_breaker_allows, is_real_run_enabled


def gate_inputs(gate: dict, *, breaker_allowed: bool) -> dict:
    """Map the real-run gate status onto the policy's gate codes.

    Pure. The key set returned here MUST cover every gate code in the active
    policy, or a policy gate would silently never receive evidence (and a
    fail-closed gate with no input always blocks). ``test_real_run_gate``
    asserts this against ``DEFAULT_REAL_RUN_POLICY``.
    """
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


def failed_gate_codes(decision: dict) -> list[str]:
    """The codes of the gates that blocked a recorded decision.

    ``build_decision_record`` carries the full ``reasons`` but, unlike
    ``evaluate_policy``, does not expose a flat ``failed`` list — so derive the
    codes from ``reasons`` via the shared extractor rather than reading a key
    that does not exist.
    """
    return gate_codes_from_reasons(decision.get("reasons"))


def _as_policy_dict(definition) -> dict | None:
    if isinstance(definition, dict):
        return definition
    if isinstance(definition, (bytes, bytearray)):
        definition = definition.decode("utf-8", "ignore")
    if isinstance(definition, str) and definition.strip():
        try:
            parsed = json.loads(definition)
        except Exception:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


async def load_active_policy(policy_key: str = REAL_RUN_GATE_POLICY_KEY) -> dict:
    """Load the active policy version from the DB, falling back to the default."""
    row = await database.fetch_one(
        "SELECT definition FROM policy_versions WHERE policy_key = :pk AND active = 1 ORDER BY version DESC LIMIT 1",
        {"pk": policy_key},
    )
    if row and row["definition"]:
        definition = _as_policy_dict(row["definition"])
        if definition is not None:
            return definition
    if policy_key == REAL_RUN_GATE_POLICY_KEY:
        return DEFAULT_REAL_RUN_POLICY
    from fastapi import HTTPException

    raise HTTPException(404, detail="Policy not found")


async def record_policy_decision(*, decision_id: str, policy: dict, inputs: dict, decision: dict, subject_id: str) -> None:
    await database.execute(
        """INSERT INTO policy_decisions
             (decision_id, policy_key, policy_version, subject_type, subject_id, inputs, outcome, reasons)
           VALUES (:decision_id, :policy_key, :policy_version, 'lottery', :subject_id, :inputs, :outcome, :reasons)""",
        {
            "decision_id": decision_id,
            "policy_key": policy["policy_key"],
            "policy_version": policy["version"],
            "subject_id": subject_id,
            "inputs": json.dumps(inputs, ensure_ascii=False),
            "outcome": decision["outcome"],
            "reasons": json.dumps(decision["reasons"], ensure_ascii=False),
        },
    )


async def evaluate_real_run_decision(lottery, *, account_id: int | None, record: bool = True) -> dict:
    """Authoritatively evaluate (and optionally record) the real-run decision.

    Returns ``allowed``, the recorded ``decision_id`` / ``policy_version``, the
    raw ``outcome`` and the gate ``blockers`` (for operator-facing messaging).
    """
    # Lazy import to avoid an import cycle (lotteries imports this module).
    from app.api.lotteries import real_run_gate_status

    selector_config = await load_runtime_selector_config()
    real_run_enabled = await is_real_run_enabled()
    gate = await real_run_gate_status(
        lottery,
        selector_config=selector_config,
        real_run_enabled=real_run_enabled,
        account_id=account_id,
    )
    breaker_allowed, _ = await circuit_breaker_allows(lottery["platform"])
    policy = await load_active_policy(REAL_RUN_GATE_POLICY_KEY)
    inputs = gate_inputs(gate, breaker_allowed=breaker_allowed)
    decision = build_decision_record(
        policy=policy, inputs=inputs, subject_type="lottery", subject_id=str(lottery["id"])
    )

    decision_id = None
    if record:
        decision_id = str(uuid.uuid4())
        await record_policy_decision(
            decision_id=decision_id,
            policy=policy,
            inputs=inputs,
            decision=decision,
            subject_id=str(lottery["id"]),
        )

    return {
        "allowed": decision["outcome"] == "allow",
        "outcome": decision["outcome"],
        "decision_id": decision_id,
        "policy_version": policy["version"],
        "policy_key": policy["policy_key"],
        "blockers": list(gate.get("blockers") or []),
        "failed_gates": failed_gate_codes(decision),
        "next_action": decision["next_action"],
        "inputs": inputs,
        "gate": gate,
    }
