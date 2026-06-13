"""Pure semantic-trace assembly for the DPMS Semantic Runtime (V9 / stage S8).

No database or framework dependency. A *semantic trace* stitches the five
explanation layers that already exist in earlier runtimes into one structure
for a single subject (currently a lottery):

    Intent -> Institution -> Policy -> Transition -> Execution

- Intent (V5 / V6.5): why was this subject considered at all? — its strategy
  score/tier and the advisory win-probability drivers.
- Institution (V7): which policy object, at which active version, governs it?
- Policy (V7): what did the real-run gate evaluation decide, and why?
- Transition (V8): how did that policy version come to be? — its lineage.
- Execution: what actually happened? — the recorded task runs.

Safety boundary (non-negotiable):

- This module is a *read-only, advisory* aggregator. It never decides
  anything: ``narrative_lines`` and ``consistency_checks`` only describe data
  that other runtimes already recorded, and must never feed back into
  ``evaluate_policy``, the real-run gate, or the circuit breaker. The semantic
  layer explains decisions; it never makes them.
- Every field is fail-soft: a missing input becomes an empty section with a
  "no record" narrative line, never an exception. Fail-soft here is for
  *display only* and never loosens the fail-closed gate it describes.
"""

from __future__ import annotations


# Outcomes that mean the gate refused to allow a real run.
BLOCKING_OUTCOMES = {"block", "deny", "blocked"}
# Execution statuses that mean a run actually completed successfully.
SUCCEEDED_STATUSES = {"succeeded", "won", "completed", "success"}


def _as_dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _as_list(value) -> list:
    return value if isinstance(value, list) else []


def _normalize_transition(transition_lineage) -> dict:
    """Reduce a lineage payload to ``{policy_key, active_version, lineage}``.

    ``transition_lineage`` is the response shape of
    ``GET /api/transitions/{policy_key}/lineage`` (a dict with a ``chain``
    list), but a bare list of hops or ``None`` are also accepted. Each hop is
    summarised to the few fields the narrative and UI need, so the trace never
    carries the full diff payload around.
    """
    source = _as_dict(transition_lineage)
    chain = _as_list(source.get("chain")) or _as_list(transition_lineage)
    lineage = [
        {
            "from_version": hop.get("from_version"),
            "to_version": hop.get("to_version"),
            "classification": hop.get("classification"),
            "reason_code": hop.get("reason_code"),
            "requires_extra_review": bool(hop.get("requires_extra_review")),
            "created_at": hop.get("created_at"),
        }
        for hop in chain
        if isinstance(hop, dict)
    ]
    return {
        "policy_key": source.get("policy_key"),
        "active_version": source.get("active_version"),
        "lineage": lineage,
    }


def build_semantic_trace(
    *,
    subject_type: str,
    subject_id,
    intent=None,
    institution=None,
    policy_decision=None,
    transition_lineage=None,
    execution=None,
) -> dict:
    """Assemble the five layers into one read-only trace for a subject.

    Inputs are plain dicts/lists already gathered from the relevant runtime
    APIs (or ``None`` when that source has no record). The function never
    raises on missing data: an absent layer becomes an empty section, and the
    derived ``narrative`` / ``consistency_checks`` degrade gracefully.
    """
    trace = {
        "subject_type": subject_type,
        "subject_id": str(subject_id) if subject_id is not None else None,
        "intent": _as_dict(intent),
        "institution": _as_dict(institution),
        "policy": _as_dict(policy_decision),
        "transition": _normalize_transition(transition_lineage),
        "execution": _as_dict(execution),
    }
    trace["consistency_checks"] = consistency_checks(trace)
    trace["narrative"] = {
        "en": narrative_lines(trace, lang="en"),
        "zh": narrative_lines(trace, lang="zh"),
    }
    return trace


def _policy_next_action(policy: dict):
    """Best-effort next action: stored field, else the first failing gate's remediation."""
    next_action = policy.get("next_action")
    if next_action:
        return next_action
    reasons = _as_list(policy.get("reasons"))
    if reasons and isinstance(reasons[0], dict):
        return reasons[0].get("remediation")
    return None


def _failed_gate_codes(policy: dict) -> list[str]:
    codes = []
    for reason in _as_list(policy.get("reasons")):
        if isinstance(reason, dict) and reason.get("code"):
            codes.append(reason["code"])
        elif isinstance(reason, str):
            codes.append(reason)
    return codes


# Narrative templates, kept side by side so English and Chinese never drift.
_TEMPLATES = {
    "en": {
        "intent": "Subject {sid} scored {score} (tier {tier}); top driver: {driver}.",
        "intent_none": "Intent: no strategy/learning record for this subject yet.",
        "institution": "Governing policy '{key}' is currently active at version v{ver}.",
        "institution_none": "Institution: no active policy object recorded.",
        "policy_block": "Gate evaluation returned '{outcome}'; failing gate(s): {gates}; next action: {action}.",
        "policy_allow": "Gate evaluation returned '{outcome}'; next action: {action}.",
        "policy_none": "Policy: no gate-evaluation decision recorded for this subject.",
        "transition": "Active version v{ver} evolved through {n} recorded transition(s); the latest was a {cls} change for reason '{reason}'.",
        "transition_none": "Transition: no recorded policy evolution; the active version is the original.",
        "execution": "Execution status: {status}, across {n} task run(s).",
        "execution_none": "Execution: no task runs recorded for this subject.",
    },
    "zh": {
        "intent": "目标 {sid} 评分 {score}（{tier} 档）；首要驱动：{driver}。",
        "intent_none": "意图：该目标暂无策略/学习记录。",
        "institution": "适用制度 '{key}' 当前生效版本为 v{ver}。",
        "institution_none": "制度：暂无生效的策略对象记录。",
        "policy_block": "门禁评估结果为 '{outcome}'；未通过门禁：{gates}；下一步：{action}。",
        "policy_allow": "门禁评估结果为 '{outcome}'；下一步：{action}。",
        "policy_none": "策略：该目标暂无门禁评估决策记录。",
        "transition": "生效版本 v{ver} 历经 {n} 次记录在案的迁移；最近一次为因 '{reason}' 的 {cls} 变更。",
        "transition_none": "迁移：暂无策略演化记录，生效版本即初始版本。",
        "execution": "执行状态：{status}，共 {n} 条任务运行记录。",
        "execution_none": "执行：该目标暂无任务运行记录。",
    },
}


def narrative_lines(trace: dict, lang: str = "en") -> list[str]:
    """Render a trace into short, human-readable statements in ``lang``.

    Every line is derived purely from already-recorded data. Missing layers
    yield an explicit "no record" line rather than being dropped, so the
    narrative always covers all five layers and never raises.
    """
    tpl = _TEMPLATES.get(lang, _TEMPLATES["en"])
    lines: list[str] = []

    intent = _as_dict(trace.get("intent"))
    if intent:
        drivers = _as_list(intent.get("top_drivers"))
        top = drivers[0] if drivers else None
        driver_name = top.get("feature") if isinstance(top, dict) else (top if top else "n/a")
        lines.append(tpl["intent"].format(
            sid=trace.get("subject_id"),
            score=intent.get("strategy_score", "n/a"),
            tier=intent.get("priority_tier", "n/a"),
            driver=driver_name,
        ))
    else:
        lines.append(tpl["intent_none"])

    institution = _as_dict(trace.get("institution"))
    if institution.get("policy_key"):
        lines.append(tpl["institution"].format(
            key=institution.get("policy_key"),
            ver=institution.get("active_version", "?"),
        ))
    else:
        lines.append(tpl["institution_none"])

    policy = _as_dict(trace.get("policy"))
    if policy.get("outcome"):
        outcome = policy.get("outcome")
        action = _policy_next_action(policy) or "n/a"
        if outcome in BLOCKING_OUTCOMES:
            gates = _failed_gate_codes(policy)
            lines.append(tpl["policy_block"].format(
                outcome=outcome,
                gates=", ".join(gates) if gates else "n/a",
                action=action,
            ))
        else:
            lines.append(tpl["policy_allow"].format(outcome=outcome, action=action))
    else:
        lines.append(tpl["policy_none"])

    transition = _as_dict(trace.get("transition"))
    lineage = _as_list(transition.get("lineage"))
    if lineage:
        latest = lineage[-1]
        lines.append(tpl["transition"].format(
            ver=transition.get("active_version", "?"),
            n=len(lineage),
            cls=latest.get("classification", "n/a"),
            reason=latest.get("reason_code", "n/a"),
        ))
    else:
        lines.append(tpl["transition_none"])

    execution = _as_dict(trace.get("execution"))
    runs = _as_list(execution.get("task_runs"))
    if execution.get("status") or runs:
        lines.append(tpl["execution"].format(
            status=execution.get("status", "unknown"),
            n=len(runs),
        ))
    else:
        lines.append(tpl["execution_none"])

    return lines


def consistency_checks(trace: dict) -> list[str]:
    """Lightweight, advisory-only cross-layer consistency hints.

    These never block anything: they surface data that looks internally
    inconsistent (e.g. the gate blocked but execution still succeeded, a
    decision was made under an older policy version than the one now active,
    or the lottery record hasn't caught up with its own latest task run) so
    an operator can investigate. A fully consistent trace returns ``[]``.
    """
    hints: list[str] = []

    policy = _as_dict(trace.get("policy"))
    execution = _as_dict(trace.get("execution"))
    outcome = policy.get("outcome")
    status = execution.get("status")
    if outcome in BLOCKING_OUTCOMES and status in SUCCEEDED_STATUSES:
        hints.append(
            f"Policy outcome was '{outcome}' but execution status is '{status}': "
            "a run succeeded despite the gate blocking — verify the recorded sequence."
        )

    institution = _as_dict(trace.get("institution"))
    decision_version = policy.get("policy_version")
    active_version = institution.get("active_version")
    if (
        isinstance(decision_version, int)
        and isinstance(active_version, int)
        and decision_version < active_version
    ):
        hints.append(
            f"Decision was made under policy version v{decision_version}, but the active "
            f"policy is now v{active_version}: replay this decision with its historical "
            "version rather than the current one."
        )

    latest_run_status = execution.get("latest_run_status")
    if (
        latest_run_status in SUCCEEDED_STATUSES
        and status is not None
        and status not in SUCCEEDED_STATUSES
    ):
        hints.append(
            f"The latest task run succeeded (status='{latest_run_status}') but the lottery "
            f"record still shows status='{status}': verify the lottery record was updated."
        )

    return hints
