"""Pure policy-transition logic for the DPMS Transition Runtime (V8 / stage S7).

No database or framework dependency. A *transition* records how a Policy
Object (see ``app.governance.policy``) moved from one version to the next:
why it changed, what changed, what to do if it needs to be undone, and
whether the change made the gate stricter or looser.

Safety boundary (non-negotiable):

- Loosening a policy (removing a required gate, or turning a required gate
  optional) must always carry an explicit, non-empty justification and is
  flagged for extra review. Tightening or neutral changes are never blocked
  on this.
- Versions must increment by exactly one: no skipping, no backfilling
  history. This keeps the lineage a single, ordered chain.
- This module only validates and classifies; it never mutates a policy or
  activates a version. Publishing and activating remain separate, explicit,
  admin-confirmed actions in the API layer.
"""

from __future__ import annotations

from app.governance.policy import diff_policies


TRANSITION_REASON_CODES = (
    "experiment_result",
    "risk_tightening",
    "manual_review",
    "incident_response",
    "scheduled_review",
)

# policy_key -> subject_type affected by a gate change for that policy.
POLICY_SUBJECTS = {
    "real_run_gate": "lottery",
}


def classify_transition(diff: dict) -> str:
    """Classify a policy diff as "tightening", "loosening" or "neutral".

    Loosening (removing a gate, or turning a required gate optional) takes
    priority over any tightening signal in the same diff: a mixed change
    that loosens *anything* must be reviewed as a loosening change.
    """
    if diff.get("removed_gates"):
        return "loosening"
    changed_to = diff.get("changed_required_to", {})
    if any(required is False for required in changed_to.values()):
        return "loosening"

    added_required = diff.get("added_gates_required", {})
    if any(required is True for required in added_required.values()):
        return "tightening"
    if any(required is True for required in changed_to.values()):
        return "tightening"

    return "neutral"


def validate_transition(
    *,
    from_policy: dict,
    to_policy: dict,
    reason_code: str,
    rollback_condition: str,
    loosening_justification: str | None = None,
) -> tuple[bool, list[str]]:
    """Validate a proposed transition between two policy versions.

    Returns ``(valid, errors)``. The caller separately computes ``diff`` and
    ``classification`` via ``diff_policies``/``classify_transition`` for
    persistence; this function recomputes them internally purely to decide
    validity, keeping it self-contained and easy to test with two policy
    dicts.
    """
    errors: list[str] = []

    from_version = from_policy.get("version")
    to_version = to_policy.get("version")
    if not isinstance(from_version, int) or not isinstance(to_version, int) or to_version != from_version + 1:
        errors.append("version_must_increment_by_one")

    if reason_code not in TRANSITION_REASON_CODES:
        errors.append("invalid_reason_code")

    if not str(rollback_condition or "").strip():
        errors.append("rollback_condition_required")

    diff = diff_policies(from_policy, to_policy)
    classification = classify_transition(diff)
    if classification == "loosening":
        if not str(loosening_justification or "").strip():
            errors.append("loosening_justification_required")
        if reason_code == "scheduled_review":
            errors.append("loosening_cannot_use_scheduled_review")

    return (not errors), errors


def impact_scope(policy_key: str, diff: dict) -> dict:
    """Describe what a policy diff affects, for operator-facing display.

    Unknown ``policy_key`` values return an empty (but non-erroring) impact
    scope, since the lineage/diff are still meaningful even for policies this
    runtime doesn't yet know how to describe.
    """
    affected_gates = sorted(
        set(diff.get("added_gates", []))
        | set(diff.get("removed_gates", []))
        | set(diff.get("changed_required", []))
    )
    subject_type = POLICY_SUBJECTS.get(policy_key)
    if subject_type is None:
        return {"subject_type": None, "affected_gates": affected_gates, "description": ""}
    if not affected_gates:
        description = f"No gate changes affecting {subject_type} real-run evaluation."
    else:
        description = (
            f"Affects every {subject_type}'s real-run gate evaluation "
            f"via {len(affected_gates)} changed gate(s): {', '.join(affected_gates)}."
        )
    return {"subject_type": subject_type, "affected_gates": affected_gates, "description": description}


def rollback_plan(transition_record: dict) -> dict:
    """Draft a reverse transition back to ``from_version``.

    The draft is informational only: it still must pass
    ``validate_transition`` (against the policy definitions for the current
    and target versions) before it can be published, so a rollback never
    takes effect automatically.
    """
    return {
        "policy_key": transition_record.get("policy_key"),
        "target_version": transition_record.get("from_version"),
        "reason_code": "manual_review",
        "rollback_condition": transition_record.get("rollback_condition"),
    }
