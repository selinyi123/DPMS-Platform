"""Pure Policy Object logic for the DPMS Governance Runtime (V7 / stage S6).

No database or framework dependency. A *Policy Object* is a versioned,
declarative list of gates. Evaluating it against a subject's inputs yields a
deterministic, replayable decision record. This institutionalizes the existing
real-run gate: instead of gate logic scattered across the API, the gates become
data that can be versioned, diffed, and replayed.

Safety boundary (non-negotiable):

- The policy is fail-closed: a required gate whose input is missing or false
  blocks the decision. Evaluation can only ever *deny*; it never invents an
  allow that the inputs do not support.
- Decisions are deterministic and replayable: the same inputs against the same
  policy version always produce the same outcome, so any past decision can be
  audited by re-running it.
"""

from __future__ import annotations

REAL_RUN_GATE_POLICY_KEY = "real_run_gate"

# These gates are the non-negotiable safety floor for every real-run policy.
# A versioned/custom policy may add stricter gates, but it must never be able to
# remove one of these gates or mark it optional.  Keep the canonical order: it
# is also the operator-facing remediation order when several gates fail.
MANDATORY_REAL_RUN_GATES = (
    {"code": "global_real_run_enabled", "required": True, "remediation": "enable_real_run"},
    {"code": "circuit_breaker_closed", "required": True, "remediation": "review_breaker"},
    {"code": "valid_lottery_target", "required": True, "remediation": "add_target"},
    {"code": "action_plan_reviewed", "required": True, "remediation": "review_rule"},
    {"code": "calibrated_account_available", "required": True, "remediation": "add_account"},
    {"code": "real_adapter_enabled", "required": True, "remediation": "configure_adapter"},
    {"code": "recent_complete_probe", "required": True, "remediation": "probe"},
    {"code": "recent_shadow_run", "required": True, "remediation": "shadow_run"},
    {"code": "no_recent_account_risk", "required": True, "remediation": "review_risk"},
)
MANDATORY_REAL_RUN_GATE_CODES = frozenset(gate["code"] for gate in MANDATORY_REAL_RUN_GATES)

# The canonical real-run policy (version 1). Each gate mirrors an existing
# real-run evidence requirement, now expressed as versioned data.
DEFAULT_REAL_RUN_POLICY = {
    "policy_key": REAL_RUN_GATE_POLICY_KEY,
    "version": 1,
    "description": "Real-run readiness gate as a versioned policy object.",
    "gates": [dict(gate) for gate in MANDATORY_REAL_RUN_GATES],
}


def default_policies() -> dict[str, dict]:
    """Return the built-in policies keyed by ``policy_key``."""
    return {REAL_RUN_GATE_POLICY_KEY: DEFAULT_REAL_RUN_POLICY}


def validate_policy(policy: dict) -> tuple[bool, list[str]]:
    """Check a policy definition is well-formed before it is stored."""
    errors: list[str] = []
    if not policy.get("policy_key"):
        errors.append("policy_key_required")
    if not isinstance(policy.get("version"), int) or policy.get("version", 0) < 1:
        errors.append("version_must_be_positive_int")
    gates = policy.get("gates")
    if not isinstance(gates, list) or not gates:
        errors.append("at_least_one_gate_required")
        return (not errors), errors
    codes = []
    for index, gate in enumerate(gates):
        if not isinstance(gate, dict) or not gate.get("code"):
            errors.append(f"gate_{index}_code_required")
            continue
        codes.append(gate["code"])
    if len(codes) != len(set(codes)):
        errors.append("gate_codes_must_be_unique")
    if policy.get("policy_key") == REAL_RUN_GATE_POLICY_KEY:
        gates_by_code = {
            gate.get("code"): gate
            for gate in gates
            if isinstance(gate, dict) and gate.get("code")
        }
        for mandatory_gate in MANDATORY_REAL_RUN_GATES:
            code = mandatory_gate["code"]
            gate = gates_by_code.get(code)
            if gate is None:
                errors.append(f"mandatory_real_run_gate_missing:{code}")
            elif not bool(gate.get("required", True)):
                errors.append(f"mandatory_real_run_gate_optional:{code}")
    return (not errors), errors


def effective_policy_gates(policy: dict) -> list[dict]:
    """Return the gates that are authoritative for this evaluation.

    Stored policy definitions are data and may pre-date the mandatory-gate
    validation above (or have been written outside the API).  The final
    evaluator therefore overlays the canonical real-run safety floor at read
    time as well.  Custom definitions can only append stricter gates; their
    copies of mandatory gates cannot change required/remediation semantics.
    """

    configured = policy.get("gates")
    configured_gates = configured if isinstance(configured, list) else []
    if policy.get("policy_key") != REAL_RUN_GATE_POLICY_KEY:
        return [gate for gate in configured_gates if isinstance(gate, dict)]

    effective = [dict(gate) for gate in MANDATORY_REAL_RUN_GATES]
    effective.extend(
        gate
        for gate in configured_gates
        if isinstance(gate, dict)
        and gate.get("code")
        and gate.get("code") not in MANDATORY_REAL_RUN_GATE_CODES
    )
    return effective


def evaluate_policy(*, policy: dict, inputs: dict) -> dict:
    """Evaluate a policy against subject inputs into a deterministic decision.

    ``inputs`` maps gate codes to truthy/falsy evidence values. Returns the
    outcome (``allow``/``block``), the satisfied and failed gates, and the
    operator-facing remediation for the first failed gate.
    """
    satisfied: list[str] = []
    failed: list[dict] = []
    for gate in effective_policy_gates(policy):
        code = gate.get("code")
        required = bool(gate.get("required", True))
        present = bool(inputs.get(code))
        if present:
            satisfied.append(code)
        elif required:
            failed.append({"code": code, "remediation": gate.get("remediation")})
    outcome = "allow" if not failed else "block"
    return {
        "policy_key": policy.get("policy_key"),
        "policy_version": policy.get("version"),
        "outcome": outcome,
        "satisfied": satisfied,
        "failed": [item["code"] for item in failed],
        "reasons": failed,
        "next_action": failed[0]["remediation"] if failed else "real_run",
    }


def gate_codes_from_reasons(reasons) -> list[str]:
    """The gate codes blocking a decision, from its ``reasons`` list.

    ``reasons`` is the structured ``{"code", "remediation"}`` shape that
    ``evaluate_policy`` produces; bare string codes are also accepted so
    callers reading older/external decision records don't have to special
    case them. Anything else is skipped rather than raising, since this feeds
    operator-facing summaries (real-run gate response, semantic trace
    narrative) where a malformed entry should degrade, not 500.

    The single source for "which gates failed" from a persisted decision:
    ``build_decision_record`` only carries ``reasons`` (not ``evaluate_policy``'s
    flat ``failed`` list), and the ``policy_decisions`` table persists the same
    ``reasons`` column, so every consumer reading a decision back out derives
    the codes here.
    """
    codes = []
    for reason in reasons or []:
        if isinstance(reason, dict) and reason.get("code"):
            codes.append(reason["code"])
        elif isinstance(reason, str):
            codes.append(reason)
    return codes


def build_decision_record(*, policy: dict, inputs: dict, subject_type: str, subject_id: str | None) -> dict:
    """Build a replayable decision record (the API adds an id and timestamp)."""
    decision = evaluate_policy(policy=policy, inputs=inputs)
    return {
        "policy_key": decision["policy_key"],
        "policy_version": decision["policy_version"],
        "subject_type": subject_type,
        "subject_id": subject_id,
        "inputs": dict(inputs),
        "outcome": decision["outcome"],
        "reasons": decision["reasons"],
        "next_action": decision["next_action"],
    }


def replay_decision(*, policy: dict, record: dict) -> dict:
    """Re-evaluate a stored decision's inputs and confirm the outcome matches.

    The heart of decision auditing: feed the recorded inputs back through the
    (same-version) policy and verify we still reach the recorded outcome.
    """
    recomputed = evaluate_policy(policy=policy, inputs=record.get("inputs", {}))
    expected = record.get("outcome")
    return {
        "matches": recomputed["outcome"] == expected,
        "recomputed_outcome": recomputed["outcome"],
        "recorded_outcome": expected,
        "policy_version": policy.get("version"),
        "recorded_policy_version": record.get("policy_version"),
        "version_matches": policy.get("version") == record.get("policy_version"),
    }


def diff_policies(old: dict, new: dict) -> dict:
    """Structurally diff two policy versions for change review.

    ``added_gates_required``/``changed_required_to`` map each added or
    required-changed gate code to its *new* ``required`` flag, so V8's
    ``classify_transition`` can tell a stricter change from a looser one
    without re-reading the full policy objects.
    """
    old_gates = {g["code"]: g for g in old.get("gates", []) if g.get("code")}
    new_gates = {g["code"]: g for g in new.get("gates", []) if g.get("code")}
    added = [code for code in new_gates if code not in old_gates]
    removed = [code for code in old_gates if code not in new_gates]
    changed_required = [
        code
        for code in new_gates
        if code in old_gates and bool(old_gates[code].get("required", True)) != bool(new_gates[code].get("required", True))
    ]
    return {
        "from_version": old.get("version"),
        "to_version": new.get("version"),
        "added_gates": added,
        "removed_gates": removed,
        "changed_required": changed_required,
        "added_gates_required": {code: bool(new_gates[code].get("required", True)) for code in added},
        "changed_required_to": {code: bool(new_gates[code].get("required", True)) for code in changed_required},
    }
