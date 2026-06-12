"""Experiment Runtime API (V5.5 / stage S3).

Thin data-access layer over ``app.experiment.engine``. The engine owns every
decision (validation, branch assignment, stats, auto-stop); this module only
persists experiments and aggregates raw counters from existing tables.

Safety: experiments run in dry_run / shadow_run only unless an admin explicitly
opts into a real-run experiment, which then still passes the standard real-run
gate at dispatch time. Results are advisory; nothing here auto-dispatches.
"""

import json
import uuid

from fastapi import APIRouter, HTTPException, Request

from app.db import database
from app.event_store.service import record_event
from app.experiment.engine import (
    assign_branch,
    compare_branches,
    experiment_guardrails,
    should_stop_branch,
    validate_experiment_spec,
)
from app.models.schemas import (
    ExperimentAssignRequest,
    ExperimentBranchStopRequest,
    ExperimentCreate,
    ExperimentStopRequest,
)
from app.platforms import get_platforms
from app.security import audit_event, require_min_role


router = APIRouter()


@router.get("/")
async def list_experiments(limit: int = 50):
    rows = await database.fetch_all(
        """SELECT * FROM experiments ORDER BY id DESC LIMIT :limit""",
        {"limit": min(max(limit, 1), 100)},
    )
    items = []
    for row in rows:
        experiment = dict(row)
        branches = await database.fetch_all(
            "SELECT branch_key, label, weight, status FROM experiment_branches WHERE experiment_id = :eid ORDER BY id ASC",
            {"eid": experiment["experiment_id"]},
        )
        experiment["branches"] = [dict(b) for b in branches]
        items.append(experiment)
    return {"items": items, "count": len(items)}


@router.post("/")
async def create_experiment(data: ExperimentCreate, request: Request):
    actor = require_min_role(request, "operator")
    mode = data.mode.value if hasattr(data.mode, "value") else str(data.mode)
    if data.allow_real_run:
        # Opting a real-run experiment in is an admin-only action; the dispatch
        # path still enforces the full real-run evidence gate.
        require_min_role(request, "admin")

    branches = [
        {"key": b.key, "label": b.label, "weight": b.weight, "config": b.config or {}}
        for b in data.branches
    ]
    valid, errors = validate_experiment_spec(
        name=data.name,
        platform=data.platform,
        mode=mode,
        branches=branches,
        supported_platforms=set(get_platforms().keys()),
        allow_real_run=data.allow_real_run,
    )
    if not valid:
        raise HTTPException(400, detail={"message": "invalid experiment spec", "errors": errors})

    experiment_id = str(uuid.uuid4())
    await database.execute(
        """INSERT INTO experiments (experiment_id, name, platform, mode, status, hypothesis, allow_real_run, created_by)
           VALUES (:experiment_id, :name, :platform, :mode, 'running', :hypothesis, :allow_real_run, :created_by)""",
        {
            "experiment_id": experiment_id,
            "name": data.name.strip(),
            "platform": data.platform,
            "mode": mode,
            "hypothesis": data.hypothesis,
            "allow_real_run": int(bool(data.allow_real_run)),
            "created_by": actor["actor_id"],
        },
    )
    for branch in branches:
        await database.execute(
            """INSERT INTO experiment_branches (experiment_id, branch_key, label, weight, config_json, status)
               VALUES (:experiment_id, :branch_key, :label, :weight, :config_json, 'active')""",
            {
                "experiment_id": experiment_id,
                "branch_key": branch["key"],
                "label": branch["label"] or branch["key"],
                "weight": float(branch["weight"]),
                "config_json": json.dumps(branch["config"], ensure_ascii=False),
            },
        )
    await audit_event(
        request,
        action="experiment.create",
        resource_type="experiment",
        resource_id=experiment_id,
        result="created",
        risk_level="medium" if mode != "real_run" else "high",
        detail={"platform": data.platform, "mode": mode, "branches": [b["key"] for b in branches]},
    )
    await record_event(
        aggregate="experiment",
        aggregate_id=experiment_id,
        event_type="ExperimentCreated",
        payload={"name": data.name.strip(), "platform": data.platform, "mode": mode, "branches": [b["key"] for b in branches]},
        actor_type="operator",
        actor_id=actor["actor_id"],
    )
    return {"status": "created", "experiment_id": experiment_id}


@router.get("/{experiment_id}")
async def get_experiment(experiment_id: str):
    experiment = await database.fetch_one(
        "SELECT * FROM experiments WHERE experiment_id = :eid", {"eid": experiment_id}
    )
    if not experiment:
        raise HTTPException(404, detail="Experiment not found")
    experiment = dict(experiment)
    branch_rows = await database.fetch_all(
        "SELECT * FROM experiment_branches WHERE experiment_id = :eid ORDER BY id ASC",
        {"eid": experiment_id},
    )
    raw_stats = await _branch_raw_stats(experiment_id)
    branch_results = []
    for row in branch_rows:
        branch = dict(row)
        counters = raw_stats.get(branch["branch_key"], {})
        branch_results.append({"key": branch["branch_key"], **counters})

    comparison = compare_branches(branch_results)
    has_comment_branch = any(
        "comment" in json.dumps(dict(r).get("config_json") or "", ensure_ascii=False).lower() for r in branch_rows
    )
    # Surface the per-branch auto-stop recommendation without mutating state on a read.
    stop_signals = {}
    for branch in comparison["branches"]:
        stop, reason = should_stop_branch(
            risk_events=branch.get("risk_events", 0),
            runs=branch.get("runs", 0),
        )
        stop_signals[branch["key"]] = {"should_stop": stop, "reason": reason}

    return {
        "experiment": experiment,
        "branches": [dict(r) for r in branch_rows],
        "comparison": comparison,
        "stop_signals": stop_signals,
        "guardrails": experiment_guardrails(mode=experiment["mode"], has_comment_branch=has_comment_branch),
    }


@router.post("/{experiment_id}/assign")
async def assign_experiment_unit(experiment_id: str, data: ExperimentAssignRequest, request: Request):
    actor = require_min_role(request, "operator")
    experiment = await database.fetch_one(
        "SELECT * FROM experiments WHERE experiment_id = :eid", {"eid": experiment_id}
    )
    if not experiment:
        raise HTTPException(404, detail="Experiment not found")
    if experiment["status"] != "running":
        raise HTTPException(409, detail=f"Experiment is {experiment['status']}, not running")

    branch_rows = await database.fetch_all(
        "SELECT branch_key, weight FROM experiment_branches WHERE experiment_id = :eid AND status = 'active'",
        {"eid": experiment_id},
    )
    branches = [{"key": r["branch_key"], "weight": r["weight"]} for r in branch_rows]
    if not branches:
        raise HTTPException(409, detail="No active branches to assign")

    unit_key = f"lottery:{data.lottery_id}"
    existing = await database.fetch_one(
        "SELECT branch_key FROM experiment_assignments WHERE experiment_id = :eid AND unit_key = :unit_key",
        {"eid": experiment_id, "unit_key": unit_key},
    )
    if existing:
        # Stable assignment: a unit is never re-bucketed once recorded.
        return {"status": "exists", "branch_key": existing["branch_key"], "unit_key": unit_key}

    branch_key = assign_branch(branches=branches, unit_key=unit_key)
    if not branch_key:
        raise HTTPException(409, detail="Could not assign a branch")
    await database.execute(
        """INSERT INTO experiment_assignments (experiment_id, branch_key, unit_key, lottery_id, task_id, account_id)
           VALUES (:experiment_id, :branch_key, :unit_key, :lottery_id, :task_id, :account_id)""",
        {
            "experiment_id": experiment_id,
            "branch_key": branch_key,
            "unit_key": unit_key,
            "lottery_id": data.lottery_id,
            "task_id": data.task_id,
            "account_id": data.account_id,
        },
    )
    await record_event(
        aggregate="experiment",
        aggregate_id=experiment_id,
        event_type="ExperimentUnitAssigned",
        payload={"branch_key": branch_key, "unit_key": unit_key, "lottery_id": data.lottery_id},
        actor_type="operator",
        actor_id=actor["actor_id"],
    )
    return {"status": "assigned", "branch_key": branch_key, "unit_key": unit_key}


@router.post("/{experiment_id}/stop")
async def stop_experiment(experiment_id: str, data: ExperimentStopRequest, request: Request):
    actor = require_min_role(request, "admin")
    experiment = await database.fetch_one(
        "SELECT * FROM experiments WHERE experiment_id = :eid", {"eid": experiment_id}
    )
    if not experiment:
        raise HTTPException(404, detail="Experiment not found")
    await database.execute(
        "UPDATE experiments SET status = 'stopped', stopped_reason = :reason, updated_at = NOW() WHERE experiment_id = :eid",
        {"eid": experiment_id, "reason": data.reason},
    )
    await audit_event(
        request,
        action="experiment.stop",
        resource_type="experiment",
        resource_id=experiment_id,
        result="stopped",
        risk_level="medium",
        detail={"reason": data.reason},
    )
    await record_event(
        aggregate="experiment",
        aggregate_id=experiment_id,
        event_type="ExperimentStopped",
        payload={"reason": data.reason},
        actor_type="operator",
        actor_id=actor["actor_id"],
    )
    return {"status": "stopped", "experiment_id": experiment_id}


@router.post("/{experiment_id}/branches/{branch_key}/stop")
async def stop_experiment_branch(experiment_id: str, branch_key: str, data: ExperimentBranchStopRequest, request: Request):
    actor = require_min_role(request, "operator")
    branch = await database.fetch_one(
        "SELECT * FROM experiment_branches WHERE experiment_id = :eid AND branch_key = :bk",
        {"eid": experiment_id, "bk": branch_key},
    )
    if not branch:
        raise HTTPException(404, detail="Branch not found")
    await database.execute(
        "UPDATE experiment_branches SET status = 'stopped', stopped_reason = :reason WHERE experiment_id = :eid AND branch_key = :bk",
        {"eid": experiment_id, "bk": branch_key, "reason": data.reason},
    )
    await record_event(
        aggregate="experiment",
        aggregate_id=experiment_id,
        event_type="ExperimentBranchStopped",
        payload={"branch_key": branch_key, "reason": data.reason},
        actor_type="operator",
        actor_id=actor["actor_id"],
    )
    return {"status": "stopped", "experiment_id": experiment_id, "branch_key": branch_key}


async def _branch_raw_stats(experiment_id: str) -> dict[str, dict]:
    """Aggregate raw per-branch counters from assignments joined to outcomes.

    Pure counting only; the engine turns these into rates and comparisons.
    """
    rows = await database.fetch_all(
        """SELECT a.branch_key,
                  COUNT(*) AS assignments,
                  SUM(tr.status IN ('succeeded','failed')) AS runs,
                  SUM(tr.status = 'failed') AS failed,
                  SUM(l.status IN ('participated','won','lost')) AS participated,
                  SUM(l.status = 'won') AS won,
                  SUM(l.status = 'lost') AS lost
           FROM experiment_assignments a
           LEFT JOIN task_runs tr ON tr.task_id = a.task_id
           LEFT JOIN lotteries l ON l.id = a.lottery_id
           WHERE a.experiment_id = :eid
           GROUP BY a.branch_key""",
        {"eid": experiment_id},
    )
    risk_rows = await database.fetch_all(
        """SELECT a.branch_key, COUNT(*) AS risk_events
           FROM experiment_assignments a
           JOIN risk_events r ON r.account_id = a.account_id AND r.created_at >= a.created_at
           WHERE a.experiment_id = :eid
           GROUP BY a.branch_key""",
        {"eid": experiment_id},
    )
    risk_by_branch = {r["branch_key"]: int(r["risk_events"] or 0) for r in risk_rows}
    result = {}
    for row in rows:
        item = dict(row)
        key = item["branch_key"]
        result[key] = {
            "assignments": int(item["assignments"] or 0),
            "runs": int(item["runs"] or 0),
            "failed": int(item["failed"] or 0),
            "participated": int(item["participated"] or 0),
            "won": int(item["won"] or 0),
            "lost": int(item["lost"] or 0),
            "risk_events": risk_by_branch.get(key, 0),
        }
    return result
