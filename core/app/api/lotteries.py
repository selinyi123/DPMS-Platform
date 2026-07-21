import json
import os
import uuid
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from app.action_plan import (
    ACTION_ORDER,
    ACTION_SET,
    BILIBILI_API_EXECUTION_PATH,
    ActionPlanV2Error,
    compute_action_plan_hash,
    compute_bilibili_api_config_hash,
    compute_config_hash,
    compute_target_hash,
    semantic_requirement_status,
    validate_action_payload,
    validate_action_plan_v2,
)

from app.adapter_config import (
    PHASES as ADAPTER_PHASES,
    load_runtime_selector_config,
    platform_has_runtime_real_adapter,
    platform_probe_ready_for_real_actions,
    platform_real_adapter_kind,
    recommended_config_from_probe,
    selector_config_complete,
)
from app.db import database
from app.event_store.service import record_event
from app.knowledge.service import account_reputation
from app.models.schemas import (
    AdapterSelectorConfigUpdate,
    AdapterProbeRequest,
    DispatchTaskRequest,
    LotteryActionPlanUpdate,
    LotteryCreate,
    LotteryResultUpdate,
    LotteryTargetImport,
    TrackedSourceCreate,
)
from app.platforms import get_platform, get_platforms
from app.strategy.engine import (
    account_tier,
    choose_strategy_mode,
    empty_platform_knowledge,
    estimate_trust_score_breakdown,
    estimate_win_probability_breakdown,
    first_or_none,
    priority_tier,
    strategy_knowledge_confidence,
    strategy_knowledge_confidence_breakdown,
    strategy_score_breakdown,
)
from app.services.discovery import run_discovery
from app.services.lottery_rules import parse_lottery_rule
from app.services.account_leases import (
    AccountOperationLeaseConflict,
    acquire_account_operation_lease,
    bind_lease_to_task,
)
from app.services.outbox import build_lottery_task_message, enqueue_outbox, try_flush_dedup
from app.services.real_run_gate import evaluate_real_run_decision
from app.services.real_run_readiness import (
    emit_real_run_gate_notification,
    parse_json_field,
    phase_configured,
    platform_selectors_complete,
    recent_account_risk,
    load_real_run_evidence_batch,
    load_exact_bilibili_execution_evidence,
    real_run_account_risk_summaries,
    real_run_gate_status,
    validate_real_run_evidence,
)
from app.services.bilibili_preflight_evidence import (
    BilibiliPreflightEvidenceError,
    extract_bilibili_dynamic_id,
)
from app.services.rule_provenance import ensure_rule_snapshot
from app.security import (
    audit_event,
    circuit_breaker_allows,
    is_real_run_enabled,
    require_confirmation,
    require_min_role,
)
from app.utils.canonicalizer import canonicalize_platform_url
from app.utils.lottery_targets import validate_lottery_target
from app.utils.log import structured_log


router = APIRouter()
PHASES = ["followed", "liked", "commented", "reposted"]
EVIDENCE_ROOT = Path(os.getenv("EVIDENCE_ROOT", "/profiles"))
TASK_FAILURE_DIR = EVIDENCE_ROOT / "task-failures"
TASK_SHADOW_DIR = EVIDENCE_ROOT / "shadow-runs"
ADAPTER_PROBE_DIR = EVIDENCE_ROOT / "adapter-probes"


def validated_probe_navigation_url(raw_url: str) -> str:
    target_url = str(raw_url or "").strip()
    parsed = urlparse(target_url)
    if parsed.scheme.lower() != "https" or not parsed.netloc:
        raise HTTPException(400, detail="Adapter probe target must use HTTPS")
    return target_url


def protected_source_rule_text(existing_rule_text, proposed_rule_text) -> str:
    """Keep the captured source rule immutable once it has been stored.

    The action-plan editor may classify the source rule, but it must not turn
    into a second ingestion path that replaces a complex source rule with a
    simpler executable summary.  An empty legacy record may be populated once;
    subsequent source corrections need a dedicated, separately audited flow.
    """

    existing_raw = str(existing_rule_text or "")
    existing = existing_raw.strip()
    if proposed_rule_text is None:
        candidate = existing
    else:
        candidate = str(proposed_rule_text).strip()
    if not candidate:
        raise HTTPException(400, detail="Source lottery rule text is required")
    if existing and candidate != existing:
        raise HTTPException(
            409,
            detail="Source lottery rule text cannot be replaced through action-plan review",
        )
    return existing_raw if existing else candidate


STRATEGY_TARGET_METRICS_SQL = """(
                    SELECT COUNT(*)
                    FROM accounts a
                    WHERE a.platform = l.platform
                      AND a.status = 'ready'
                      AND OCTET_LENGTH(a.encrypted_credential) > 0
                      AND (
                        SELECT c.status FROM account_calibrations c
                        WHERE c.account_id = a.id
                        ORDER BY c.created_at DESC
                        LIMIT 1
                      ) = 'succeeded'
                  ) AS safe_accounts,
                  (
                    SELECT COUNT(*)
                    FROM risk_events r
                    JOIN accounts a ON a.id = r.account_id
                    WHERE a.platform = l.platform
                      AND r.created_at >= DATE_SUB(NOW(), INTERVAL 24 HOUR)
                  ) AS recent_platform_risk,
                  (
                    SELECT COUNT(*)
                    FROM task_runs tr
                    WHERE tr.lottery_id = l.id
                      AND tr.status IN ('queued','running')
                  ) AS active_runs,
                  (
                    SELECT COUNT(*)
                    FROM task_runs tr
                    WHERE tr.lottery_id = l.id
                      AND COALESCE(tr.task_mode, IF(tr.dry_run = 1, 'dry_run', 'real_run')) = 'dry_run'
                      AND tr.status = 'succeeded'
                  ) AS dry_success,
                  (
                    SELECT COUNT(*)
                    FROM task_runs tr
                    WHERE tr.lottery_id = l.id
                      AND tr.task_mode = 'shadow_run'
                      AND tr.status = 'succeeded'
                  ) AS shadow_success,
                  (
                    SELECT COUNT(*)
                    FROM task_runs tr
                    WHERE tr.lottery_id = l.id
                      AND tr.status = 'failed'
                  ) AS failed_runs,
                  (
                    SELECT ac.result
                    FROM adapter_calibrations ac
                    WHERE ac.platform = l.platform
                    ORDER BY ac.id DESC
                    LIMIT 1
                  ) AS latest_probe_result"""


MAX_IMPORT_TARGET_LINES = 1000
REPAIR_DISPATCH_INTENT_BINDING_READY = False


def clamp_limit(value: int, minimum: int = 1, maximum: int = 200) -> int:
    return min(max(int(value or minimum), minimum), maximum)


def _mysql_error_code(value) -> int | None:
    """Extract a numeric MySQL error code without depending on one driver type."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, (tuple, list)) and value:
        return _mysql_error_code(value[0])
    return None


def _is_mysql_duplicate_entry(exc: Exception) -> bool:
    """Return true only for MySQL duplicate-entry error 1062.

    ``databases`` may expose the DB-API exception directly or through a wrapper,
    so inspect the normal exception chain while avoiding message matching that
    could misclassify a timeout or an application error containing the word
    "duplicate".
    """
    pending = [exc]
    seen = set()
    while pending:
        current = pending.pop()
        marker = id(current)
        if marker in seen:
            continue
        seen.add(marker)

        if _mysql_error_code(getattr(current, "errno", None)) == 1062:
            return True
        if _mysql_error_code(getattr(current, "args", None)) == 1062:
            return True

        for attr in ("orig", "__cause__", "__context__"):
            nested = getattr(current, attr, None)
            if isinstance(nested, BaseException):
                pending.append(nested)
    return False


async def _record_post_commit_event(**event) -> str | None:
    """Record an event without turning a committed command into a false 500.

    The event store already retries and dead-letters critical events. This
    boundary also protects callers from an unexpected event-store exception and
    makes a returned ``None`` explicit in logs. Cancellation is deliberately not
    swallowed because ``CancelledError`` is not an ``Exception`` on supported
    Python versions.
    """
    try:
        event_id = await record_event(**event)
    except Exception as exc:
        structured_log(
            "error",
            "post_commit_event_record_failed",
            aggregate=event.get("aggregate"),
            aggregate_id=event.get("aggregate_id"),
            event_type=event.get("event_type"),
            error=str(exc),
        )
        return None
    if event_id is None:
        structured_log(
            "error",
            "post_commit_event_unrecorded",
            aggregate=event.get("aggregate"),
            aggregate_id=event.get("aggregate_id"),
            event_type=event.get("event_type"),
        )
    return event_id


def _sql_in_values(prefix: str, values) -> tuple[str, dict]:
    parameters = {}
    placeholders = []
    for index, value in enumerate(values):
        key = f"{prefix}_{index}"
        placeholders.append(f":{key}")
        parameters[key] = value
    return ", ".join(placeholders), parameters


@router.get("/adapters")
async def list_adapters():
    selector_config = await load_runtime_selector_config()
    return [
        {
            "platform": key,
            "label": cfg["label"],
            "dry_run": True,
            "real_actions": cfg.get("action_adapter", False) or platform_has_runtime_real_adapter(selector_config, key),
            "adapter_status": "configured" if platform_has_runtime_real_adapter(selector_config, key) else cfg.get("adapter_status", "planned"),
            "adapter_kind": platform_real_adapter_kind(selector_config, key),
            "phases": PHASES,
            "notes": "real actions require gray calibration"
            if cfg.get("action_adapter") or platform_selectors_complete(selector_config, key)
            else "login and dry-run only until adapter calibration is implemented",
        }
        for key, cfg in get_platforms().items()
    ]


@router.get("/adapters/config")
async def get_adapter_config_status():
    config = await load_runtime_selector_config()
    platforms = []
    for platform in get_platforms():
        phase_status = {}
        configured = config.get(platform, {})
        if not isinstance(configured, dict):
            configured = {}
        for phase in ADAPTER_PHASES:
            phase_status[phase] = phase_configured(platform, configured, phase)
        configured_complete = platform_selectors_complete(config, platform)
        platforms.append(
            {
                "platform": platform,
                "configured": configured_complete or platform_has_runtime_real_adapter(config, platform),
                "selector_configured": configured_complete,
                "adapter_kind": platform_real_adapter_kind(config, platform),
                "phases": phase_status,
            }
        )
    return {
        "preferred_env": "DPMS_ADAPTER_SELECTORS_B64",
        "fallback_env": "DPMS_ADAPTER_SELECTORS",
        "required_phases": list(ADAPTER_PHASES),
        "platforms": platforms,
    }


@router.put("/adapters/config")
async def save_adapter_config(data: AdapterSelectorConfigUpdate, request: Request):
    actor = require_min_role(request, "admin")
    if not isinstance(data.config, dict) or not data.config:
        raise HTTPException(400, detail="config must be a non-empty object")
    saved = []
    invalid = []
    for platform, config in data.config.items():
        if not get_platform(platform):
            invalid.append({"platform": platform, "error": "unsupported platform"})
            continue
        if not isinstance(config, dict):
            invalid.append({"platform": platform, "error": "platform config must be an object"})
            continue
        phase_status = {phase: phase_configured(platform, config, phase) for phase in ADAPTER_PHASES}
        configured_complete = selector_config_complete(platform, config)
        await save_platform_selector_config(platform, config)
        saved.append({"platform": platform, "configured": configured_complete, "phases": phase_status})
    if not saved:
        raise HTTPException(400, detail={"message": "no valid platform config", "invalid": invalid})
    await audit_event(
        request,
        action="adapter_selector_config.save",
        resource_type="adapter_selector_config",
        result="saved",
        risk_level="high",
        detail={"saved": saved, "invalid_count": len(invalid)},
    )
    for item in saved:
        await _record_post_commit_event(
            aggregate="platform",
            aggregate_id=item["platform"],
            event_type="AdapterSelectorConfigSaved",
            payload={"configured": item["configured"], "phases": item["phases"], "invalid_count": len(invalid)},
            actor_type="operator",
            actor_id=actor["actor_id"],
        )
    return {"status": "saved", "saved": saved, "invalid": invalid}


@router.delete("/adapters/config/{platform}")
async def clear_adapter_config(platform: str, request: Request):
    actor = require_min_role(request, "admin")
    require_confirmation(request)
    if not get_platform(platform):
        raise HTTPException(404, detail="Platform not found")
    await database.execute("DELETE FROM adapter_selector_configs WHERE platform = :platform", {"platform": platform})
    await audit_event(
        request,
        action="adapter_selector_config.clear",
        resource_type="adapter_selector_config",
        resource_id=platform,
        result="cleared",
        risk_level="high",
    )
    await _record_post_commit_event(
        aggregate="platform",
        aggregate_id=platform,
        event_type="AdapterSelectorConfigCleared",
        payload={"platform": platform},
        actor_type="operator",
        actor_id=actor["actor_id"],
    )
    return {"status": "cleared", "platform": platform}


@router.get("/")
async def list_lotteries(status: str = None, limit: int = 50):
    limit = clamp_limit(limit)
    query = "SELECT * FROM lotteries"
    values = {"limit": limit}
    if status:
        query += " WHERE status = :status"
        values["status"] = status
    query += " ORDER BY id DESC LIMIT :limit"
    rows = await database.fetch_all(query, values)
    return [serialize_lottery(r) for r in rows]


@router.get("/real-run/evidence")
async def list_real_run_evidence(
    status: str = None,
    limit: int = 50,
    account_id: int | None = None,
):
    selected_account = None
    if account_id is not None:
        if account_id <= 0:
            raise HTTPException(400, detail="account_id must be positive")
        selected_account = await database.fetch_one(
            "SELECT id, platform FROM accounts WHERE id = :account_id",
            {"account_id": account_id},
        )
        if not selected_account:
            raise HTTPException(404, detail="Account not found")
    query = "SELECT * FROM lotteries"
    values = {"limit": min(max(limit, 1), 100)}
    if status:
        query += " WHERE status = :status"
        values["status"] = status
    query += " ORDER BY id DESC LIMIT :limit"
    rows = await database.fetch_all(query, values)
    selector_config = await load_runtime_selector_config()
    real_run_enabled = await is_real_run_enabled()
    lottery_ids = [int(row["id"]) for row in rows]
    evidence_batch = await load_real_run_evidence_batch(rows, account_id=account_id)
    account_summaries = await real_run_account_risk_summaries(
        dict.fromkeys(str(row["platform"]) for row in rows)
    )
    completed_actions = await completed_real_run_actions_for_lotteries(lottery_ids)
    action_ledgers = await bilibili_action_ledgers_for_lotteries(lottery_ids, limit=12)
    items = []
    for row in rows:
        lottery = dict(row)
        lottery_id = int(lottery["id"])
        scoped_account_id = account_id
        account_scope_matches = bool(
            not selected_account
            or str(selected_account["platform"]) == str(lottery["platform"])
        )
        gate = await real_run_gate_status(
            lottery,
            selector_config=selector_config,
            real_run_enabled=real_run_enabled,
            account_summary=account_summaries[str(lottery["platform"])],
            evidence_batch=evidence_batch,
            account_id=scoped_account_id,
        )
        if not account_scope_matches:
            gate["allowed"] = False
            gate["blockers"] = ["account_platform_mismatch", *gate.get("blockers", [])]
            gate["next_action"] = "select_account"
        gate["selected_account_id"] = account_id
        gate["account_scope_matches_platform"] = account_scope_matches if account_id is not None else None
        gate["repair_plan"] = await build_lottery_repair_plan(
            lottery,
            completed_actions=completed_actions[lottery_id],
        )
        gate["action_ledger"] = action_ledgers[lottery_id]
        items.append(gate)
    return {"items": items, "selected_account_id": account_id}


def ordered_actions(actions) -> list[str]:
    if not isinstance(actions, list):
        return []
    selected = {str(action) for action in actions}
    return [action for action in PHASES if action in selected]


def missing_repair_actions(required_actions: list[str], completed_actions: list[str]) -> list[str]:
    completed = set(completed_actions)
    return [action for action in ordered_actions(required_actions) if action not in completed]


def require_dispatchable_lottery_state(locked, *, repair: bool = False) -> None:
    """Fail closed unless a locked lottery row is safe to claim.

    A terminal result is never a signal to replay the full action plan. Missing
    actions must use the dedicated repair path, and an unknown external outcome
    keeps ``execution_lock`` populated until an explicit reconciliation flow is
    implemented.
    """
    if not locked:
        raise HTTPException(404, detail="Lottery not found")
    status = str(locked["status"] or "").strip().lower()
    execution_lock = str(locked["execution_lock"] or "").strip()
    if execution_lock:
        raise HTTPException(409, detail="Lottery has an execution lock and requires settlement or reconciliation")
    if status != "pending":
        operation = "repair-dispatchable" if repair else "dispatchable"
        raise HTTPException(409, detail=f"Lottery status '{status}' is not {operation}")


def require_lottery_not_executing(locked, *, operation: str) -> None:
    """Protect execution semantics from concurrent operator mutations."""
    if not locked:
        raise HTTPException(404, detail="Lottery not found")
    status = str(locked["status"] or "").strip().lower()
    execution_lock = str(locked["execution_lock"] or "").strip()
    if execution_lock or status in {"claimed", "running"}:
        raise HTTPException(409, detail=f"Lottery cannot {operation} while an execution is active or unresolved")


def require_dispatch_snapshot_unchanged(locked, snapshot) -> None:
    """Reject a dispatch if its preflight snapshot changed before row claim."""
    locked_data = dict(locked)
    snapshot_data = dict(snapshot)
    for field in ("platform", "raw_url", "canonical_url", "rule_text"):
        if str(locked_data.get(field) or "") != str(snapshot_data.get(field) or ""):
            raise HTTPException(409, detail=f"Lottery {field} changed during dispatch preflight; retry review")
    if parse_json_field(locked_data.get("action_plan")) != parse_json_field(snapshot_data.get("action_plan")):
        raise HTTPException(409, detail="Lottery action plan changed during dispatch preflight; retry review")
    for field in (
        "authoritative_rule_snapshot_id",
        "rule_hash",
        "action_plan_hash",
    ):
        if str(locked_data.get(field) or "") != str(snapshot_data.get(field) or ""):
            raise HTTPException(409, detail=f"Lottery {field} changed during dispatch preflight; retry review")


def bilibili_plan_binding(
    lottery,
    *,
    require_executable: bool,
    execution_revision: int,
) -> dict:
    """Extract only a fully hash-bound v2 plan; legacy plans stay non-runnable."""

    try:
        plan = validate_action_plan_v2(
            parse_json_field(lottery["action_plan"]),
            require_executable=require_executable,
        )
        snapshot_id = int(lottery["authoritative_rule_snapshot_id"] or 0)
    except (ActionPlanV2Error, TypeError, ValueError, KeyError) as exc:
        code = exc.code if isinstance(exc, ActionPlanV2Error) else "action_plan_binding_invalid"
        raise HTTPException(409, detail={"message": "Bilibili Action Plan v2 is not dispatchable", "blockers": [code]}) from exc
    if (
        snapshot_id != plan.rule_snapshot_id
        or str(lottery["rule_hash"] or "") != plan.rule_hash
        or str(lottery["action_plan_hash"] or "") != plan.plan_hash
        or plan.execution_path_id != BILIBILI_API_EXECUTION_PATH
    ):
        raise HTTPException(
            409,
            detail={
                "message": "Bilibili Action Plan v2 binding changed; review and preflight again",
                "blockers": ["action_plan_rule_binding_mismatch"],
            },
        )
    try:
        target_hash = compute_target_hash(str(lottery["canonical_url"] or ""))
        config_hash = compute_bilibili_api_config_hash(execution_revision)
    except ActionPlanV2Error as exc:
        raise HTTPException(
            409,
            detail={
                "message": "Bilibili target or account revision is not hash-bindable",
                "blockers": [exc.code],
            },
        ) from exc
    return {
        "rule_snapshot_id": plan.rule_snapshot_id,
        "rule_hash": plan.rule_hash,
        "action_plan_hash": plan.plan_hash,
        "execution_path_id": plan.execution_path_id,
        "target_hash": target_hash,
        "config_hash": config_hash,
        "execution_revision": execution_revision,
        "required_actions": plan.required_actions,
        "follow_target_handle": plan.follow_target_handle,
        "action_plan": plan.plan,
    }


def require_no_completed_actions_for_full_real_dispatch(completed_actions: list[str]) -> None:
    if completed_actions:
        raise HTTPException(
            409,
            detail={
                "message": "Lottery already has confirmed real actions; use repair or reconciliation instead of full dispatch",
                "completed_actions": completed_actions,
            },
        )


def require_repair_plan_unchanged(current_plan: dict, preflight_plan: dict) -> None:
    if (
        not current_plan.get("eligible")
        or current_plan.get("repair_action_plan") != preflight_plan.get("repair_action_plan")
    ):
        raise HTTPException(
            409,
            detail={
                "message": "Completed-action evidence changed during repair preflight; rebuild the repair plan",
                "repair_plan": current_plan,
            },
        )


def normalize_action_ledger_row(row) -> dict:
    item = dict(row)
    item["ok"] = bool(item.get("ok"))
    return item


async def bilibili_action_ledger_for_lottery(lottery_id: int, limit: int = 20) -> list[dict]:
    try:
        rows = await database.fetch_all(
            """SELECT * FROM bilibili_action_ledger
               WHERE lottery_id = :lottery_id
               ORDER BY id DESC LIMIT :limit""",
            {"lottery_id": lottery_id, "limit": clamp_limit(limit)},
        )
    except Exception as exc:
        structured_log("warning", "bilibili_action_ledger_query_failed", lottery_id=lottery_id, error=str(exc))
        raise
    return [normalize_action_ledger_row(row) for row in rows]


async def bilibili_action_ledgers_for_lotteries(lottery_ids, limit: int = 20) -> dict[int, list[dict]]:
    """Load the newest N ledger rows per lottery with one MySQL 8 query."""
    ids = list(dict.fromkeys(int(lottery_id) for lottery_id in lottery_ids))
    ledgers = {lottery_id: [] for lottery_id in ids}
    if not ids:
        return ledgers
    lottery_clause, values = _sql_in_values("ledger_lottery", ids)
    values["ledger_limit"] = clamp_limit(limit)
    try:
        rows = await database.fetch_all(
            f"""SELECT ranked_ledger.*
                FROM (
                  SELECT bal.*,
                         ROW_NUMBER() OVER (
                           PARTITION BY bal.lottery_id
                           ORDER BY bal.id DESC
                         ) AS evidence_rank
                  FROM bilibili_action_ledger bal
                  WHERE bal.lottery_id IN ({lottery_clause})
                ) ranked_ledger
                WHERE evidence_rank <= :ledger_limit
                ORDER BY lottery_id, id DESC""",
            values,
        )
    except Exception as exc:
        structured_log(
            "warning",
            "bilibili_action_ledger_batch_query_failed",
            lottery_count=len(ids),
            error=str(exc),
        )
        # This endpoint is an evidence view, so storage failure must not be
        # rendered indistinguishably from a valid empty action history.
        raise
    for row in rows:
        try:
            lottery_id = int(row["lottery_id"])
        except (KeyError, TypeError, ValueError):
            continue
        if lottery_id not in ledgers:
            continue
        item = normalize_action_ledger_row(row)
        item.pop("evidence_rank", None)
        ledgers[lottery_id].append(item)
    return ledgers


async def completed_real_run_actions_from_ledger(lottery_id: int) -> list[str]:
    # This evidence decides whether a full real-run can safely be replayed.
    # Query failure is not equivalent to "no completed actions" and must
    # propagate so dispatch/repair fail closed.
    rows = await database.fetch_all(
        """SELECT DISTINCT phase
           FROM bilibili_action_ledger
           WHERE lottery_id = :lottery_id
             AND task_mode = 'real_run'
             AND ok = 1
             AND phase IS NOT NULL""",
        {"lottery_id": lottery_id},
    )
    completed = [row["phase"] for row in rows if row["phase"] in PHASES]
    return ordered_actions(completed)


@router.get("/action-ledger")
async def list_bilibili_action_ledger(limit: int = 100, lottery_id: int = None, task_id: str = None):
    limit = clamp_limit(limit)
    where = []
    values = {"limit": limit}
    if lottery_id is not None:
        where.append("lottery_id = :lottery_id")
        values["lottery_id"] = lottery_id
    if task_id:
        where.append("task_id = :task_id")
        values["task_id"] = task_id
    query = "SELECT * FROM bilibili_action_ledger"
    if where:
        query += " WHERE " + " AND ".join(where)
    query += " ORDER BY id DESC LIMIT :limit"
    rows = await database.fetch_all(query, values)
    return [normalize_action_ledger_row(row) for row in rows]


async def completed_real_run_actions(lottery_id: int) -> list[str]:
    ledger_completed = await completed_real_run_actions_from_ledger(lottery_id)
    event_rows = await database.fetch_all(
        """SELECT DISTINCT JSON_UNQUOTE(JSON_EXTRACT(e.payload, '$.phase')) AS phase
           FROM events e
           JOIN task_runs tr ON tr.task_id = e.correlation_id
           WHERE tr.lottery_id = :lottery_id
             AND tr.task_mode = 'real_run'
             AND e.event_type = 'TaskPhaseCompleted'""",
        {"lottery_id": lottery_id},
    )
    legacy_rows = await database.fetch_all(
        """SELECT DISTINCT tp.phase
           FROM task_phases tp
           JOIN task_runs tr ON tr.task_id = tp.task_id
           WHERE tp.lottery_id = :lottery_id
             AND tr.task_mode = 'real_run'""",
        {"lottery_id": lottery_id},
    )
    event_completed = [row["phase"] for row in event_rows if row["phase"] in PHASES]
    legacy_completed = [row["phase"] for row in legacy_rows if row["phase"] in PHASES]
    return ordered_actions([*ledger_completed, *event_completed, *legacy_completed])


async def completed_real_run_actions_for_lotteries(lottery_ids) -> dict[int, list[str]]:
    """Union completion evidence for many lotteries without per-row queries."""
    ids = list(dict.fromkeys(int(lottery_id) for lottery_id in lottery_ids))
    completed = {lottery_id: [] for lottery_id in ids}
    if not ids:
        return completed
    lottery_clause, values = _sql_in_values("completed_lottery", ids)
    ledger_rows = await database.fetch_all(
        f"""SELECT DISTINCT lottery_id, phase
            FROM bilibili_action_ledger
            WHERE lottery_id IN ({lottery_clause})
              AND task_mode = 'real_run'
              AND ok = 1
              AND phase IS NOT NULL""",
        values,
    )
    event_rows = await database.fetch_all(
        f"""SELECT DISTINCT tr.lottery_id,
                   JSON_UNQUOTE(JSON_EXTRACT(e.payload, '$.phase')) AS phase
            FROM events e
            JOIN task_runs tr ON tr.task_id = e.correlation_id
            WHERE tr.lottery_id IN ({lottery_clause})
              AND tr.task_mode = 'real_run'
              AND e.event_type = 'TaskPhaseCompleted'""",
        values,
    )
    legacy_rows = await database.fetch_all(
        f"""SELECT DISTINCT tp.lottery_id, tp.phase
            FROM task_phases tp
            JOIN task_runs tr ON tr.task_id = tp.task_id
            WHERE tp.lottery_id IN ({lottery_clause})
              AND tr.task_mode = 'real_run'""",
        values,
    )
    for row in [*ledger_rows, *event_rows, *legacy_rows]:
        try:
            lottery_id = int(row["lottery_id"])
        except (KeyError, TypeError, ValueError):
            continue
        phase = row["phase"]
        if lottery_id in completed and phase in PHASES:
            completed[lottery_id].append(phase)
    return {lottery_id: ordered_actions(phases) for lottery_id, phases in completed.items()}


async def build_lottery_repair_plan(lottery, *, completed_actions: list[str] | None = None) -> dict:
    lottery_data = dict(lottery)
    action_plan = parse_json_field(lottery_data.get("action_plan")) or {}
    required_actions = ordered_actions(action_plan.get("required_actions") if isinstance(action_plan, dict) else [])
    if completed_actions is None:
        completed_actions = await completed_real_run_actions(int(lottery_data["id"]))
    else:
        completed_actions = ordered_actions(completed_actions)
    missing_actions = missing_repair_actions(required_actions, completed_actions)

    reason = "missing_actions_available"
    if not isinstance(action_plan, dict) or not action_plan:
        reason = "action_plan_missing"
    elif action_plan.get("review_required"):
        reason = "rule_review_required"
    elif not required_actions:
        reason = "required_actions_missing"
    elif not completed_actions:
        reason = "no_real_actions_completed"
    elif not missing_actions:
        reason = "no_missing_actions"

    if reason == "missing_actions_available":
        status = str(lottery_data.get("status") or "").strip().lower()
        execution_lock = str(lottery_data.get("execution_lock") or "").strip()
        if execution_lock or status in {"claimed", "running"}:
            reason = "execution_in_flight_or_reconciliation_required"
        elif status != "pending":
            reason = "lottery_not_pending"

    eligible = reason == "missing_actions_available"
    repair_action_plan = None
    if eligible:
        repair_action_plan = {
            "version": 1,
            "is_lottery": bool(parsed_rule.get("is_lottery")),
            "required_actions": missing_actions,
            "review_required": False,
            "confidence": 1.0,
            "source": "missing_action_repair",
            "full_required_actions": required_actions,
            "completed_actions": completed_actions,
        }

    return {
        "eligible": eligible,
        "reason": reason,
        "required_actions": required_actions,
        "completed_actions": completed_actions,
        "missing_actions": missing_actions,
        "repair_action_plan": repair_action_plan,
    }


@router.get("/strategy/queue")
async def strategy_queue(limit: int = 20):
    selector_config = await load_runtime_selector_config()
    real_run_enabled = await is_real_run_enabled()
    platform_knowledge = await load_strategy_platform_knowledge()
    account_recommendations = await load_strategy_account_recommendations()
    rows = await database.fetch_all(
        f"""SELECT l.*,
                  {STRATEGY_TARGET_METRICS_SQL}
           FROM lotteries l
           WHERE l.status IN ('pending','claimed')
           ORDER BY l.value_score DESC, l.id ASC
           LIMIT :limit""",
        {"limit": min(max(limit, 1), 100)},
    )

    items = []
    for row in rows:
        item = await compute_strategy_item(
            dict(row),
            selector_config=selector_config,
            real_run_enabled=real_run_enabled,
            platform_knowledge=platform_knowledge,
            account_recommendations=account_recommendations,
        )
        items.append(item)

    items.sort(key=lambda row: row["strategy_score"], reverse=True)
    for rank, item in enumerate(items, start=1):
        item["rank"] = rank
    return {"items": items, "count": len(items)}


@router.get("/{lottery_id}/strategy/explain")
async def explain_lottery_strategy(lottery_id: int):
    selector_config = await load_runtime_selector_config()
    real_run_enabled = await is_real_run_enabled()
    row = await database.fetch_one(
        f"""SELECT l.*,
                  {STRATEGY_TARGET_METRICS_SQL}
           FROM lotteries l
           WHERE l.id = :lottery_id""",
        {"lottery_id": lottery_id},
    )
    if not row:
        raise HTTPException(404, detail="Lottery not found")

    item = dict(row)
    platform_knowledge = await load_strategy_platform_knowledge()
    account_recommendations = await load_strategy_account_recommendations(item["platform"])
    return await compute_strategy_item(
        item,
        selector_config=selector_config,
        real_run_enabled=real_run_enabled,
        platform_knowledge=platform_knowledge,
        account_recommendations=account_recommendations,
        include_breakdown=True,
    )


@router.get("/sources")
async def list_tracked_sources():
    rows = await database.fetch_all("SELECT * FROM tracked_sources ORDER BY id DESC")
    return [dict(row) for row in rows]


@router.post("/sources")
async def create_tracked_source(data: TrackedSourceCreate, request: Request):
    actor = require_min_role(request, "operator")
    if not get_platform(data.platform):
        raise HTTPException(400, detail=f"Unsupported platform: {data.platform}")
    if data.source_type not in {"url_list", "keyword", "up"}:
        raise HTTPException(400, detail="source_type must be url_list, keyword, or up")
    if data.scan_interval_minutes < 1:
        raise HTTPException(400, detail="scan_interval_minutes must be >= 1")
    source_id = await database.execute(
        """INSERT INTO tracked_sources (platform, source_type, source_value, scan_interval_minutes, active)
           VALUES (:platform, :source_type, :source_value, :scan_interval_minutes, 1)
           ON DUPLICATE KEY UPDATE scan_interval_minutes = :scan_interval_minutes, active = 1""",
        {
            "platform": data.platform,
            "source_type": data.source_type,
            "source_value": data.source_value.strip(),
            "scan_interval_minutes": data.scan_interval_minutes,
        },
    )
    await _record_post_commit_event(
        aggregate="source",
        aggregate_id=source_id or f"{data.platform}:{data.source_type}:{data.source_value.strip()}",
        event_type="DiscoverySourceCreated",
        payload={
            "platform": data.platform,
            "source_type": data.source_type,
            "scan_interval_minutes": data.scan_interval_minutes,
        },
        actor_type="operator",
        actor_id=actor["actor_id"],
    )
    return {"status": "created", "id": source_id}


@router.post("/sources/scan")
async def scan_tracked_sources(request: Request):
    actor = require_min_role(request, "operator")
    stats = await run_discovery()
    await _record_post_commit_event(
        aggregate="system",
        aggregate_id="discovery",
        event_type="DiscoveryScanCompleted",
        payload=stats,
        actor_type="operator",
        actor_id=actor["actor_id"],
    )
    return {"status": "scanned", **stats}


@router.post("/")
async def create_lottery(data: LotteryCreate, request: Request):
    actor = require_min_role(request, "operator")
    if not get_platform(data.platform):
        raise HTTPException(400, detail=f"Unsupported platform: {data.platform}")

    raw_url = data.raw_url.strip()
    target = validate_lottery_target(data.platform, raw_url)
    if not target.valid:
        raise HTTPException(400, detail=target.reason)
    try:
        canonical_url = await canonicalize_lottery_url(data.platform, raw_url)
    except Exception as exc:
        raise HTTPException(400, detail="lottery_target_canonicalization_failed") from exc
    try:
        lottery_id = await database.execute(
            """INSERT INTO lotteries (platform, source_type, source_id, raw_url, canonical_url, value_score, expires_at, status)
               VALUES (:platform, :source_type, :source_id, :raw_url, :canonical_url, :value_score, :expires_at, 'pending')""",
            {
                "platform": data.platform,
                "source_type": data.source_type,
                "source_id": data.source_id,
                "raw_url": raw_url,
                "canonical_url": canonical_url,
                "value_score": data.value_score,
                "expires_at": data.expires_at,
            },
        )
    except Exception as exc:
        if not _is_mysql_duplicate_entry(exc):
            raise
        row = await database.fetch_one(
            """SELECT id FROM lotteries
               WHERE url_hash = SHA2(:canonical_url, 256)
                 AND canonical_url = :canonical_url""",
            {"canonical_url": canonical_url},
        )
        if row:
            return {"status": "exists", "id": row["id"]}
        raise
    await _record_post_commit_event(
        aggregate="lottery",
        aggregate_id=lottery_id,
        event_type="LotteryDiscovered",
        payload={
            "platform": data.platform,
            "source_type": data.source_type,
            "source_id": data.source_id,
            "raw_url": raw_url,
            "canonical_url": canonical_url,
            "value_score": data.value_score,
            "manual": True,
        },
        actor_type="operator",
        actor_id=actor["actor_id"],
    )
    return {"status": "created", "id": lottery_id}


@router.post("/targets/import")
async def import_lottery_targets(data: LotteryTargetImport, request: Request):
    actor = require_min_role(request, "operator")
    if not get_platform(data.platform):
        raise HTTPException(400, detail=f"Unsupported platform: {data.platform}")
    if not data.content.strip():
        raise HTTPException(400, detail="content is required")

    rows = parse_target_lines(data.content, data.platform, data.value_score)
    if not rows:
        raise HTTPException(400, detail="No valid target lines found")
    if len(rows) > MAX_IMPORT_TARGET_LINES:
        raise HTTPException(413, detail=f"Too many target lines; max={MAX_IMPORT_TARGET_LINES}")

    import_id = str(uuid.uuid4())
    created = []
    duplicates = []
    invalid = []
    for row in rows:
        if row.get("error"):
            invalid.append(row)
            continue
        try:
            target = validate_lottery_target(row["platform"], row["raw_url"])
        except Exception as exc:
            invalid.append({"line": row["line"], "raw": row["raw"], "error": str(exc) or "target validation failed"})
            continue
        if not target.valid:
            invalid.append({"line": row["line"], "raw": row["raw"], "error": target.reason})
            continue
        try:
            canonical_url = await canonicalize_lottery_url(row["platform"], row["raw_url"])
        except Exception as exc:
            invalid.append({"line": row["line"], "raw": row["raw"], "error": str(exc) or "canonicalization failed"})
            continue
        try:
            lottery_id = await database.execute(
                """INSERT INTO lotteries (platform, source_type, source_id, raw_url, canonical_url, value_score, expires_at, status)
                   VALUES (:platform, :source_type, :source_id, :raw_url, :canonical_url, :value_score, :expires_at, 'pending')""",
                {
                    "platform": row["platform"],
                    "source_type": data.source_type,
                    "source_id": data.source_id,
                    "raw_url": row["raw_url"],
                    "canonical_url": canonical_url,
                    "value_score": row["value_score"],
                    "expires_at": row["expires_at"],
                },
            )
        except Exception as exc:
            existing = None
            if _is_mysql_duplicate_entry(exc):
                try:
                    existing = await database.fetch_one(
                        """SELECT id FROM lotteries
                           WHERE url_hash = SHA2(:canonical_url, 256)
                             AND canonical_url = :canonical_url""",
                        {"canonical_url": canonical_url},
                    )
                except Exception:
                    existing = None
            if existing:
                duplicates.append({"line": row["line"], "id": existing["id"], "url": row["raw_url"], "platform": row["platform"]})
            else:
                invalid.append({"line": row["line"], "raw": row["raw"], "error": str(exc) or "insert failed"})
            continue
        created.append({"line": row["line"], "id": lottery_id, "url": row["raw_url"], "platform": row["platform"]})
        await _record_post_commit_event(
            aggregate="lottery",
            aggregate_id=lottery_id,
            event_type="LotteryDiscovered",
            payload={
                "platform": row["platform"],
                "source_type": data.source_type,
                "source_id": data.source_id,
                "raw_url": row["raw_url"],
                "canonical_url": canonical_url,
                "value_score": row["value_score"],
                "line": row["line"],
                "import_id": import_id,
            },
            correlation_id=import_id,
            actor_type="operator",
            actor_id=actor["actor_id"],
        )

    result = {
        "status": "imported",
        "received": len(rows),
        "created_count": len(created),
        "duplicate_count": len(duplicates),
        "invalid_count": len(invalid),
        "created": created[:50],
        "duplicates": duplicates[:50],
        "invalid": invalid[:50],
    }
    await _record_post_commit_event(
        aggregate="lottery_import",
        aggregate_id=import_id,
        event_type="LotteryTargetImportCompleted",
        payload={
            "default_platform": data.platform,
            "source_type": data.source_type,
            "received": len(rows),
            "created_count": len(created),
            "duplicate_count": len(duplicates),
            "invalid_count": len(invalid),
        },
        correlation_id=import_id,
        actor_type="operator",
        actor_id=actor["actor_id"],
    )
    return result


@router.post("/{lottery_id}/dispatch")
async def dispatch_lottery(lottery_id: int, data: DispatchTaskRequest, request: Request):
    actor = require_min_role(request, "operator")
    lottery = await database.fetch_one("SELECT * FROM lotteries WHERE id = :id", {"id": lottery_id})
    if not lottery:
        raise HTTPException(404, detail="Lottery not found")

    platform_cfg = get_platform(lottery["platform"])
    if not platform_cfg:
        raise HTTPException(400, detail=f"Unsupported platform: {lottery['platform']}")

    selector_config = await load_runtime_selector_config()
    platform_selectors = selector_config.get(lottery["platform"], {})
    real_adapter_enabled = platform_cfg.get("action_adapter", False) or platform_has_runtime_real_adapter(selector_config, lottery["platform"])
    task_mode = resolve_task_mode(data)
    dry_run = task_mode != "real_run"
    target = validate_lottery_target(lottery["platform"], lottery["raw_url"])
    if task_mode in {"shadow_run", "real_run"} and not target.valid:
        raise HTTPException(400, detail=target.reason)
    if task_mode == "shadow_run" and lottery["platform"] == "bilibili":
        shadow_contract = await validate_real_run_evidence(lottery, account_id=None)
        if not shadow_contract.get("action_plan_ready"):
            raise HTTPException(
                409,
                detail={
                    "message": "Shadow-run requires an attested, exact Bilibili Action Plan v2",
                    "blockers": [
                        blocker
                        for blocker in shadow_contract.get("blockers", [])
                        if blocker not in {"execution_account_scope_required", "exact_execution_evidence_required"}
                    ],
                },
            )

    if task_mode == "real_run":
        require_min_role(request, "admin")
        try:
            require_confirmation(request)
            if not data.confirm:
                raise HTTPException(409, detail="Real-run body confirmation required")
            if not await is_real_run_enabled():
                raise HTTPException(403, detail="Global real-run switch is disabled")
            breaker_allowed, breaker_reason = await circuit_breaker_allows(lottery["platform"])
            if not breaker_allowed:
                raise HTTPException(423, detail=f"Circuit breaker blocks real-run: {breaker_reason}")
            if not real_adapter_enabled:
                raise HTTPException(
                    400,
                    detail=f"Real actions for {lottery['platform']} are not implemented yet. Use dry run or wait for adapter calibration.",
                )
        except HTTPException as exc:
            await audit_event(
                request,
                action="lottery.dispatch.real",
                resource_type="lottery",
                resource_id=lottery_id,
                result="blocked",
                risk_level="critical",
                detail={"platform": lottery["platform"], "reason": exc.detail},
            )
            await _record_post_commit_event(
                aggregate="lottery",
                aggregate_id=lottery_id,
                event_type="RealRunDenied",
                payload={"platform": lottery["platform"], "reason": exc.detail},
                actor_type="operator",
                actor_id=actor["actor_id"],
                critical=True,
            )
            await emit_real_run_gate_notification(lottery, exc.detail, actor_id=actor["actor_id"])
            raise
    elif task_mode == "shadow_run":
        breaker_allowed, breaker_reason = await circuit_breaker_allows(lottery["platform"])
        if not breaker_allowed:
            await audit_event(
                request,
                action="lottery.dispatch.shadow",
                resource_type="lottery",
                resource_id=lottery_id,
                result="blocked",
                risk_level="high",
                detail={"platform": lottery["platform"], "reason": breaker_reason},
            )
            await _record_post_commit_event(
                aggregate="lottery",
                aggregate_id=lottery_id,
                event_type="ShadowRunDenied",
                payload={"platform": lottery["platform"], "reason": breaker_reason},
                actor_type="operator",
                actor_id=actor["actor_id"],
            )
            raise HTTPException(423, detail=f"Circuit breaker blocks shadow-run: {breaker_reason}")

    account = await pick_account(data.account_id, lottery["platform"])
    if not account:
        await _record_post_commit_event(
            aggregate="lottery",
            aggregate_id=lottery_id,
            event_type="TaskDispatchBlocked",
            payload={"platform": lottery["platform"], "reason": "no_available_account", "dry_run": dry_run, "mode": task_mode},
            actor_type="operator",
            actor_id=actor["actor_id"],
        )
        raise HTTPException(400, detail="No available account. Create a ready account first.")

    # The Governance policy is now the single real-run authority (P1-4): a
    # decision is evaluated against the *chosen* account, recorded, and only an
    # "allow" outcome may dispatch. The recorded decision_id / policy_version are
    # bound to the task below, so every real run is traceable to the exact gate
    # decision that authorised it.
    decision_id = None
    policy_version = None
    decision_gate: dict = {}
    if task_mode == "real_run":
        decision = await evaluate_real_run_decision(lottery, account_id=account["id"], record=True)
        decision_id = decision["decision_id"]
        policy_version = decision["policy_version"]
        decision_gate = dict(decision.get("gate") or {})
        if not decision["allowed"]:
            await _record_post_commit_event(
                aggregate="lottery",
                aggregate_id=lottery_id,
                event_type="RealRunDenied",
                payload={
                    "platform": lottery["platform"],
                    "account_id": account["id"],
                    "blockers": decision["blockers"],
                    "failed_gates": decision["failed_gates"],
                    "decision_id": decision_id,
                    "policy_version": policy_version,
                },
                actor_type="operator",
                actor_id=actor["actor_id"],
                critical=True,
            )
            await emit_real_run_gate_notification(
                lottery,
                {
                    "message": "Real-run policy gate is not satisfied",
                    "blockers": decision["blockers"],
                    "failed_gates": decision["failed_gates"],
                    "account_id": account["id"],
                    "decision_id": decision_id,
                    "policy_version": policy_version,
                },
                actor_id=actor["actor_id"],
            )
            raise HTTPException(
                409,
                detail={
                    "message": "Real-run policy gate is not satisfied",
                    "blockers": decision["blockers"],
                    "failed_gates": decision["failed_gates"],
                    "decision_id": decision_id,
                    "policy_version": policy_version,
                },
            )

    task_id = str(uuid.uuid4())
    action_plan = parse_json_field(lottery["action_plan"]) or {}
    plan_binding = {
        "rule_snapshot_id": None,
        "rule_hash": None,
        "action_plan_hash": None,
        "execution_path_id": None,
        "target_hash": None,
        "config_hash": None,
        "execution_revision": None,
        "required_actions": (),
        "follow_target_handle": "",
        "action_plan": action_plan,
    }
    if lottery["platform"] == "bilibili" and task_mode in {"shadow_run", "real_run"}:
        plan_binding = bilibili_plan_binding(
            lottery,
            require_executable=(task_mode == "real_run"),
            execution_revision=int(account["execution_revision"] or 0),
        )
        action_plan = plan_binding["action_plan"]
    execution_evidence_id = (
        str(decision_gate.get("execution_evidence_id") or "").strip()
        if task_mode == "real_run"
        else ""
    )
    if task_mode == "real_run" and lottery["platform"] == "bilibili" and not execution_evidence_id:
        raise HTTPException(
            409,
            detail={
                "message": "Real-run policy decision has no exact execution evidence binding",
                "blockers": ["exact_execution_evidence_required"],
            },
        )
    # Atomic dispatch (P1-2): task_runs row, lottery claim, and the queued
    # stream message (as an outbox row) commit together. The append-only account
    # lease fences this account across every task type.
    async with database.transaction():
        locked = await database.fetch_one(
            """SELECT status, execution_lock, platform, raw_url, canonical_url,
                      rule_text, action_plan, authoritative_rule_snapshot_id,
                      rule_hash, action_plan_hash
               FROM lotteries WHERE id = :id FOR UPDATE""",
            {"id": lottery_id},
        )
        require_dispatchable_lottery_state(locked)
        require_dispatch_snapshot_unchanged(locked, lottery)
        if task_mode == "real_run":
            completed_actions = await completed_real_run_actions(lottery_id)
            require_no_completed_actions_for_full_real_dispatch(completed_actions)
            try:
                dynamic_id = extract_bilibili_dynamic_id(
                    str(lottery["canonical_url"] or ""),
                    str(lottery["raw_url"] or ""),
                )
            except BilibiliPreflightEvidenceError as exc:
                raise HTTPException(
                    409,
                    detail={
                        "message": "Bilibili target changed or is not an exact dynamic",
                        "blockers": [exc.code],
                    },
                ) from exc
            exact_evidence = await load_exact_bilibili_execution_evidence(
                lottery_id=lottery_id,
                account_id=int(account["id"]),
                rule_snapshot_id=plan_binding["rule_snapshot_id"],
                execution_path_id=plan_binding["execution_path_id"],
                target_hash=plan_binding["target_hash"],
                rule_hash=plan_binding["rule_hash"],
                action_plan_hash=plan_binding["action_plan_hash"],
                config_hash=plan_binding["config_hash"],
                dynamic_id=dynamic_id,
                required_actions=plan_binding["required_actions"],
                execution_revision=plan_binding["execution_revision"],
                follow_target_handle=plan_binding["follow_target_handle"],
                evidence_id=execution_evidence_id,
                for_update=True,
            )
            if not exact_evidence:
                raise HTTPException(409, detail="Exact execution evidence expired or changed during dispatch")
        try:
            account_lease = await acquire_account_operation_lease(
                int(account["id"]),
                operation_kind=task_mode,
                owner_id=task_id,
                expected_execution_revision=int(account["execution_revision"] or 0),
                expected_platform=str(lottery["platform"]),
                db=database,
            )
        except AccountOperationLeaseConflict as exc:
            raise HTTPException(
                409,
                detail={
                    "message": (
                        "Account changed during dispatch preflight"
                        if exc.code == "account_operation_account_changed"
                        else "Account is already leased by another operation"
                    ),
                    "account_id": exc.account_id,
                    "code": exc.code,
                },
            ) from exc
        await database.execute(
            """INSERT INTO task_runs
                 (task_id, account_id, lottery_id, status, dry_run, task_mode,
                  decision_id, policy_version, rule_snapshot_id, rule_hash,
                  action_plan_hash, execution_evidence_id, execution_path_id,
                  target_hash, config_hash,
                  account_lease_id, account_lease_generation,
                  reconciliation_required)
               VALUES
                 (:task_id, :account_id, :lottery_id, 'queued', :dry_run, :task_mode,
                  :decision_id, :policy_version, :rule_snapshot_id, :rule_hash,
                  :action_plan_hash, :execution_evidence_id, :execution_path_id,
                  :target_hash, :config_hash,
                  :account_lease_id, :account_lease_generation, 0)""",
            {
                "task_id": task_id,
                "account_id": account["id"],
                "lottery_id": lottery_id,
                "dry_run": int(dry_run),
                "task_mode": task_mode,
                "decision_id": decision_id,
                "policy_version": policy_version,
                "rule_snapshot_id": plan_binding["rule_snapshot_id"],
                "rule_hash": plan_binding["rule_hash"],
                "action_plan_hash": plan_binding["action_plan_hash"],
                "execution_evidence_id": execution_evidence_id or None,
                "execution_path_id": plan_binding["execution_path_id"],
                "target_hash": plan_binding["target_hash"],
                "config_hash": plan_binding["config_hash"],
                "account_lease_id": account_lease.lease_id,
                "account_lease_generation": account_lease.generation,
            },
        )
        await bind_lease_to_task(account_lease, task_id, db=database)
        message = build_lottery_task_message(
            task_id=task_id,
            account_id=account["id"],
            lottery_id=lottery_id,
            platform=lottery["platform"],
            raw_url=lottery["raw_url"],
            canonical_url=lottery["canonical_url"],
            task_mode=task_mode,
            dry_run=dry_run,
            platform_selectors=platform_selectors,
            action_plan=action_plan,
            rule_snapshot_id=plan_binding["rule_snapshot_id"],
            rule_hash=plan_binding["rule_hash"],
            action_plan_hash=plan_binding["action_plan_hash"],
            execution_evidence_id=execution_evidence_id,
            execution_path_id=plan_binding["execution_path_id"],
            target_hash=plan_binding["target_hash"],
            config_hash=plan_binding["config_hash"],
            execution_revision=plan_binding["execution_revision"],
            account_lease_id=account_lease.lease_id,
            account_lease_generation=account_lease.generation,
        )
        await database.execute(
            "UPDATE lotteries SET status = 'claimed', execution_lock = :task_id, locked_at = NOW() WHERE id = :id",
            {"task_id": task_id, "id": lottery_id},
        )
        await enqueue_outbox(message, "lottery_tasks", dedup_key=task_id)
        if task_mode == "real_run":
            await audit_event(
                request,
                action="lottery.dispatch.real",
                resource_type="lottery",
                resource_id=lottery_id,
                result="queued",
                risk_level="critical",
                detail={
                    "platform": lottery["platform"],
                    "task_id": task_id,
                    "account_id": account["id"],
                    "decision_id": decision_id,
                    "policy_version": policy_version,
                    "execution_evidence_id": execution_evidence_id,
                    "rule_snapshot_id": plan_binding["rule_snapshot_id"],
                    "action_plan_hash": plan_binding["action_plan_hash"],
                    "config_hash": plan_binding["config_hash"],
                    "execution_revision": plan_binding["execution_revision"],
                    "account_lease_id": account_lease.lease_id,
                    "account_lease_generation": account_lease.generation,
                },
            )
        elif task_mode == "shadow_run":
            await audit_event(
                request,
                action="lottery.dispatch.shadow",
                resource_type="lottery",
                resource_id=lottery_id,
                result="queued",
                risk_level="medium",
                detail={
                    "platform": lottery["platform"],
                    "task_id": task_id,
                    "account_id": account["id"],
                    "rule_snapshot_id": plan_binding["rule_snapshot_id"],
                    "action_plan_hash": plan_binding["action_plan_hash"],
                    "config_hash": plan_binding["config_hash"],
                    "execution_revision": plan_binding["execution_revision"],
                    "account_lease_id": account_lease.lease_id,
                    "account_lease_generation": account_lease.generation,
                },
            )
    # Best-effort immediate relay so dispatch latency stays low; if Redis is
    # momentarily unavailable the outbox dispatcher loop retries the committed row.
    try:
        await try_flush_dedup(task_id)
    except Exception as exc:
        structured_log("warning", "dispatch_immediate_flush_failed", task_id=task_id, error=str(exc))
    await _record_post_commit_event(
        aggregate="task",
        aggregate_id=task_id,
        event_type="TaskDispatched",
        payload={
            "platform": lottery["platform"],
            "account_id": account["id"],
            "lottery_id": lottery_id,
            "dry_run": dry_run,
            "mode": task_mode,
            "raw_url": lottery["raw_url"],
            "action_plan": action_plan,
            "decision_id": decision_id,
            "policy_version": policy_version,
            "rule_snapshot_id": plan_binding["rule_snapshot_id"],
            "rule_hash": plan_binding["rule_hash"],
            "action_plan_hash": plan_binding["action_plan_hash"],
            "execution_evidence_id": execution_evidence_id or None,
            "execution_path_id": plan_binding["execution_path_id"],
            "target_hash": plan_binding["target_hash"],
            "config_hash": plan_binding["config_hash"],
            "execution_revision": plan_binding["execution_revision"],
            "account_lease_id": account_lease.lease_id,
            "account_lease_generation": account_lease.generation,
        },
        correlation_id=task_id,
        actor_type="operator",
        actor_id=actor["actor_id"],
        critical=(task_mode == "real_run"),
    )
    await _record_post_commit_event(
        aggregate="lottery",
        aggregate_id=lottery_id,
        event_type="LotteryDispatchQueued",
        payload={
            "task_id": task_id,
            "account_id": account["id"],
            "dry_run": dry_run,
            "mode": task_mode,
            "execution_evidence_id": execution_evidence_id or None,
            "config_hash": plan_binding["config_hash"],
            "execution_revision": plan_binding["execution_revision"],
            "account_lease_id": account_lease.lease_id,
            "account_lease_generation": account_lease.generation,
        },
        correlation_id=task_id,
        actor_type="operator",
        actor_id=actor["actor_id"],
        critical=(task_mode == "real_run"),
    )
    return {
        "status": "queued",
        "task_id": task_id,
        "account_id": account["id"],
        "lottery_id": lottery_id,
        "mode": task_mode,
        "decision_id": decision_id,
        "policy_version": policy_version,
        "execution_evidence_id": execution_evidence_id or None,
        "config_hash": plan_binding["config_hash"],
        "execution_revision": plan_binding["execution_revision"],
        "account_lease_id": account_lease.lease_id,
        "account_lease_generation": account_lease.generation,
    }


@router.get("/{lottery_id}/repair-plan")
async def get_lottery_repair_plan(lottery_id: int, request: Request):
    require_min_role(request, "viewer")
    lottery = await database.fetch_one("SELECT * FROM lotteries WHERE id = :id", {"id": lottery_id})
    if not lottery:
        raise HTTPException(404, detail="Lottery not found")
    return {
        "lottery_id": lottery_id,
        "platform": lottery["platform"],
        "repair_plan": await build_lottery_repair_plan(lottery),
    }


@router.post("/{lottery_id}/repair-dispatch")
async def dispatch_lottery_repair(lottery_id: int, data: DispatchTaskRequest, request: Request):
    actor = require_min_role(request, "operator")
    lottery = await database.fetch_one("SELECT * FROM lotteries WHERE id = :id", {"id": lottery_id})
    if not lottery:
        raise HTTPException(404, detail="Lottery not found")

    repair_plan = await build_lottery_repair_plan(lottery)
    if not repair_plan["eligible"]:
        raise HTTPException(409, detail={"message": "Lottery has no safe missing-action repair plan", "repair_plan": repair_plan})
    if not REPAIR_DISPATCH_INTENT_BINDING_READY:
        raise HTTPException(
            409,
            detail={
                "code": "repair_intent_binding_not_implemented",
                "message": "Repair dispatch is blocked until its exact action intent is durably bound",
                "repair_plan": repair_plan,
            },
        )

    platform_cfg = get_platform(lottery["platform"])
    if not platform_cfg:
        raise HTTPException(400, detail=f"Unsupported platform: {lottery['platform']}")

    selector_config = await load_runtime_selector_config()
    platform_selectors = selector_config.get(lottery["platform"], {})
    real_adapter_enabled = platform_cfg.get("action_adapter", False) or platform_has_runtime_real_adapter(selector_config, lottery["platform"])
    task_mode = "real_run"
    dry_run = False
    target = validate_lottery_target(lottery["platform"], lottery["raw_url"])
    if not target.valid:
        raise HTTPException(400, detail=target.reason)

    require_min_role(request, "admin")
    try:
        require_confirmation(request)
        if not data.confirm:
            raise HTTPException(409, detail="Repair real-run body confirmation required")
        if not await is_real_run_enabled():
            raise HTTPException(403, detail="Global real-run switch is disabled")
        breaker_allowed, breaker_reason = await circuit_breaker_allows(lottery["platform"])
        if not breaker_allowed:
            raise HTTPException(423, detail=f"Circuit breaker blocks repair-run: {breaker_reason}")
        if not real_adapter_enabled:
            raise HTTPException(400, detail=f"Real actions for {lottery['platform']} are not implemented yet.")
        evidence = await validate_real_run_evidence(lottery, account_id=data.account_id)
        if not evidence["allowed"]:
            raise HTTPException(409, detail={"message": "Repair evidence gate is not satisfied", "blockers": evidence["blockers"]})
    except HTTPException as exc:
        await audit_event(
            request,
            action="lottery.dispatch.repair",
            resource_type="lottery",
            resource_id=lottery_id,
            result="blocked",
            risk_level="critical",
            detail={"platform": lottery["platform"], "reason": exc.detail, "repair_plan": repair_plan},
        )
        await _record_post_commit_event(
            aggregate="lottery",
            aggregate_id=lottery_id,
            event_type="LotteryRepairDenied",
            payload={"platform": lottery["platform"], "reason": exc.detail, "repair_plan": repair_plan},
            actor_type="operator",
            actor_id=actor["actor_id"],
            critical=True,
        )
        await emit_real_run_gate_notification(lottery, exc.detail, actor_id=actor["actor_id"])
        raise

    account = await pick_account(data.account_id, lottery["platform"])
    if not account:
        await _record_post_commit_event(
            aggregate="lottery",
            aggregate_id=lottery_id,
            event_type="LotteryRepairBlocked",
            payload={"platform": lottery["platform"], "reason": "no_available_account", "repair_plan": repair_plan},
            actor_type="operator",
            actor_id=actor["actor_id"],
            critical=True,
        )
        raise HTTPException(400, detail="No available account. Create a ready account first.")

    decision = await evaluate_real_run_decision(lottery, account_id=account["id"], record=True)
    decision_id = decision["decision_id"]
    policy_version = decision["policy_version"]
    if not decision["allowed"]:
        await _record_post_commit_event(
            aggregate="lottery",
            aggregate_id=lottery_id,
            event_type="LotteryRepairDenied",
            payload={
                "platform": lottery["platform"],
                "account_id": account["id"],
                "blockers": decision["blockers"],
                "failed_gates": decision["failed_gates"],
                "decision_id": decision_id,
                "policy_version": policy_version,
                "repair_plan": repair_plan,
            },
            actor_type="operator",
            actor_id=actor["actor_id"],
            critical=True,
        )
        raise HTTPException(
            409,
            detail={
                "message": "Repair policy gate is not satisfied",
                "blockers": decision["blockers"],
                "failed_gates": decision["failed_gates"],
                "decision_id": decision_id,
                "policy_version": policy_version,
                "repair_plan": repair_plan,
            },
        )

    task_id = str(uuid.uuid4())
    repair_action_plan = repair_plan["repair_action_plan"]
    message = build_lottery_task_message(
        task_id=task_id,
        account_id=account["id"],
        lottery_id=lottery_id,
        platform=lottery["platform"],
        raw_url=lottery["raw_url"],
        canonical_url=lottery["canonical_url"],
        task_mode=task_mode,
        dry_run=dry_run,
        platform_selectors=platform_selectors,
        action_plan=repair_action_plan,
    )

    async with database.transaction():
        locked = await database.fetch_one(
            """SELECT id, status, execution_lock, platform, raw_url, canonical_url, rule_text, action_plan
               FROM lotteries WHERE id = :id FOR UPDATE""",
            {"id": lottery_id},
        )
        require_dispatchable_lottery_state(locked, repair=True)
        require_dispatch_snapshot_unchanged(locked, lottery)
        current_repair_plan = await build_lottery_repair_plan(locked)
        require_repair_plan_unchanged(current_repair_plan, repair_plan)
        await database.execute(
            """INSERT INTO task_runs (task_id, account_id, lottery_id, status, dry_run, task_mode, decision_id, policy_version)
               VALUES (:task_id, :account_id, :lottery_id, 'queued', :dry_run, :task_mode, :decision_id, :policy_version)""",
            {
                "task_id": task_id,
                "account_id": account["id"],
                "lottery_id": lottery_id,
                "dry_run": int(dry_run),
                "task_mode": task_mode,
                "decision_id": decision_id,
                "policy_version": policy_version,
            },
        )
        await database.execute(
            "UPDATE lotteries SET status = 'claimed', execution_lock = :task_id, locked_at = NOW() WHERE id = :id",
            {"task_id": task_id, "id": lottery_id},
        )
        await enqueue_outbox(message, "lottery_tasks", dedup_key=task_id)
        await audit_event(
            request,
            action="lottery.dispatch.repair",
            resource_type="lottery",
            resource_id=lottery_id,
            result="queued",
            risk_level="critical",
            detail={
                "platform": lottery["platform"],
                "task_id": task_id,
                "account_id": account["id"],
                "decision_id": decision_id,
                "policy_version": policy_version,
                "repair_plan": repair_plan,
            },
        )

    try:
        await try_flush_dedup(task_id)
    except Exception as exc:
        structured_log("warning", "repair_dispatch_immediate_flush_failed", task_id=task_id, error=str(exc))

    await _record_post_commit_event(
        aggregate="task",
        aggregate_id=task_id,
        event_type="TaskDispatched",
        payload={
            "platform": lottery["platform"],
            "account_id": account["id"],
            "lottery_id": lottery_id,
            "dry_run": dry_run,
            "mode": task_mode,
            "raw_url": lottery["raw_url"],
            "action_plan": repair_action_plan,
            "repair_plan": repair_plan,
            "decision_id": decision_id,
            "policy_version": policy_version,
        },
        correlation_id=task_id,
        actor_type="operator",
        actor_id=actor["actor_id"],
        critical=True,
    )
    await _record_post_commit_event(
        aggregate="lottery",
        aggregate_id=lottery_id,
        event_type="LotteryRepairQueued",
        payload={"task_id": task_id, "account_id": account["id"], "mode": task_mode, "repair_plan": repair_plan},
        correlation_id=task_id,
        actor_type="operator",
        actor_id=actor["actor_id"],
        critical=True,
    )
    return {
        "status": "queued",
        "task_id": task_id,
        "account_id": account["id"],
        "lottery_id": lottery_id,
        "mode": task_mode,
        "repair_plan": repair_plan,
        "decision_id": decision_id,
        "policy_version": policy_version,
    }


@router.get("/{lottery_id}/action-plan/suggest")
async def suggest_lottery_action_plan(lottery_id: int, request: Request, rule_text: str | None = None):
    require_min_role(request, "viewer")
    lottery = await database.fetch_one("SELECT id, platform, rule_text FROM lotteries WHERE id = :id", {"id": lottery_id})
    if not lottery:
        raise HTTPException(404, detail="Lottery not found")

    text = rule_text if rule_text is not None else (lottery["rule_text"] or "")
    plan = parse_lottery_rule(text, lottery["platform"])
    return {"lottery_id": lottery_id, "platform": lottery["platform"], "rule_text": text, "suggested_action_plan": plan}


@router.put("/{lottery_id}/action-plan")
async def update_lottery_action_plan(lottery_id: int, data: LotteryActionPlanUpdate, request: Request):
    actor = require_min_role(request, "operator")
    submitted_actions = [str(action).strip() for action in data.required_actions]
    invalid = [action for action in submitted_actions if action not in ACTION_SET]
    async with database.transaction():
        lottery = await database.fetch_one(
            """SELECT id, platform, source_type, source_id, raw_url, canonical_url,
                      rule_text, status, execution_lock
               FROM lotteries WHERE id = :id FOR UPDATE""",
            {"id": lottery_id},
        )
        if not lottery:
            raise HTTPException(404, detail="Lottery not found")
        require_lottery_not_executing(lottery, operation="change its action plan")
        if invalid:
            raise HTTPException(400, detail={"message": "Unsupported lottery actions", "actions": invalid})
        if not submitted_actions:
            raise HTTPException(400, detail="At least one required action must be selected")
        if len(submitted_actions) != len(set(submitted_actions)):
            raise HTTPException(400, detail="Required actions must not contain duplicates")
        required_actions = [action for action in ACTION_ORDER if action in set(submitted_actions)]

        rule_text = protected_source_rule_text(lottery["rule_text"], data.rule_text)
        parsed_rule = parse_lottery_rule(rule_text, lottery["platform"])
        unsupported_actions = list(parsed_rule.get("unsupported_actions") or [])
        content_requirements = dict(
            parsed_rule.get("content_requirements")
            or {
                "follow_targets": [],
                "commented": {"topic_tags": [], "mentions": []},
                "reposted": {"topic_tags": [], "mentions": []},
            }
        )
        ambiguity_patterns = list(parsed_rule.get("ambiguity_patterns") or [])
        parsed_required_actions = {
            str(action)
            for action in (parsed_rule.get("required_actions") or [])
            if str(action)
        }
        selected_required_actions = set(required_actions)

        raw_payloads = dict(data.action_payloads or {})
        payload_validation_errors: list[str] = []
        if set(raw_payloads) != selected_required_actions:
            payload_validation_errors.append("action_plan_payload_binding_mismatch")
        if set(raw_payloads) - ACTION_SET:
            payload_validation_errors.append("action_plan_payload_unknown_action")
        action_payloads: dict[str, dict] = {}
        for action in required_actions:
            raw_payload = raw_payloads.get(action, {})
            try:
                action_payloads[action] = validate_action_payload(action, raw_payload)
            except ActionPlanV2Error as exc:
                action_payloads[action] = dict(raw_payload) if isinstance(raw_payload, dict) else {}
                payload_validation_errors.append(exc.code)
        payload_validation_errors = list(dict.fromkeys(payload_validation_errors))

        represented_requirements, unresolved_requirements, capability_blockers = (
            semantic_requirement_status(
                unsupported_actions,
                action_payloads,
                content_requirements,
            )
        )
        execution_path_id = str(data.execution_path_id or "").strip()
        if lottery["platform"] != "bilibili":
            capability_blockers.append("platform_execution_path_not_bound")
        elif execution_path_id != BILIBILI_API_EXECUTION_PATH:
            capability_blockers.append("bilibili_execution_path_not_supported")
        capability_blockers = list(dict.fromkeys(capability_blockers))

        snapshot = await ensure_rule_snapshot(
            dict(lottery),
            rule_text,
            complete=bool(data.rule_complete_confirmed),
            actor_id=actor["actor_id"],
            db=database,
        )
        semantic_review_blocked = bool(
            not parsed_rule.get("is_lottery")
            or parsed_required_actions != selected_required_actions
            or ambiguity_patterns
            or unresolved_requirements
            or payload_validation_errors
        )
        review_required = bool(
            not data.reviewed
            or not snapshot["is_complete"]
            or semantic_review_blocked
        )
        executable = bool(not review_required and not capability_blockers)
        plan = {
            "version": 2,
            "platform": lottery["platform"],
            "is_lottery": True,
            "required_actions": required_actions,
            "action_payloads": action_payloads,
            "content_requirements": content_requirements,
            "execution_path_id": execution_path_id,
            "rule_snapshot_id": snapshot["id"],
            "rule_hash": snapshot["rule_hash"],
            "review_required": review_required,
            "executable": executable,
            "confidence": 1.0 if not review_required else 0.5,
            "source": "operator_complete_attestation" if snapshot["is_complete"] else "operator_draft",
            "reviewed_by": actor["actor_id"] if data.reviewed else None,
            "rule_complete_confirmed": bool(snapshot["is_complete"]),
            "unsupported_actions": unsupported_actions,
            "represented_requirements": represented_requirements,
            "unresolved_requirements": unresolved_requirements,
            "ambiguity_patterns": ambiguity_patterns,
            "payload_validation_errors": payload_validation_errors,
            "capability_blockers": capability_blockers,
        }
        plan["plan_hash"] = compute_action_plan_hash(plan)
        await database.execute(
            """UPDATE lotteries
               SET rule_text = :rule_text,
                   action_plan = :action_plan,
                   authoritative_rule_snapshot_id = :authoritative_rule_snapshot_id,
                   rule_hash = :rule_hash,
                   action_plan_hash = :action_plan_hash
               WHERE id = :id""",
            {
                "id": lottery_id,
                "rule_text": rule_text,
                "action_plan": json.dumps(plan, ensure_ascii=False),
                "authoritative_rule_snapshot_id": snapshot["id"] if snapshot["is_complete"] else None,
                "rule_hash": snapshot["rule_hash"],
                "action_plan_hash": plan["plan_hash"],
            },
        )
        # The high-risk review write and its audit record commit atomically.
        # Otherwise an audit failure returns 500 after the plan already changed,
        # making an operator retry ambiguous and leaving an unaudited approval.
        await audit_event(
            request,
            action="lottery.action_plan.update",
            resource_type="lottery",
            resource_id=lottery_id,
            result="saved",
            risk_level="high",
            detail={
                "platform": lottery["platform"],
                "required_actions": required_actions,
                "reviewed": data.reviewed,
                "rule_complete_confirmed": bool(snapshot["is_complete"]),
                "semantic_review_blocked": semantic_review_blocked,
                "unsupported_actions": unsupported_actions,
                "unresolved_requirements": unresolved_requirements,
                "payload_validation_errors": payload_validation_errors,
                "capability_blockers": capability_blockers,
                "rule_snapshot_id": snapshot["id"],
                "rule_hash": snapshot["rule_hash"],
                "action_plan_hash": plan["plan_hash"],
            },
        )
    await _record_post_commit_event(
        aggregate="lottery",
        aggregate_id=lottery_id,
        event_type="LotteryActionPlanReviewed" if not plan["review_required"] else "LotteryActionPlanUpdated",
        payload={
            "required_actions": required_actions,
            "reviewed": data.reviewed,
            "review_required": plan["review_required"],
            "executable": plan["executable"],
            "unsupported_actions": unsupported_actions,
            "represented_requirements": represented_requirements,
            "unresolved_requirements": unresolved_requirements,
            "capability_blockers": capability_blockers,
            "rule_snapshot_id": snapshot["id"],
            "rule_hash": snapshot["rule_hash"],
            "action_plan_hash": plan["plan_hash"],
            "rule_text": rule_text,
        },
        actor_type="operator",
        actor_id=actor["actor_id"],
    )
    return {"status": "saved", "lottery_id": lottery_id, "action_plan": plan}


@router.get("/tasks/runs")
async def list_task_runs(limit: int = 50):
    limit = clamp_limit(limit)
    rows = await database.fetch_all(
        """SELECT tr.*, l.raw_url, l.platform
           FROM task_runs tr
           JOIN lotteries l ON tr.lottery_id = l.id
           ORDER BY tr.id DESC LIMIT :limit""",
        {"limit": limit},
    )
    return [dict(r) for r in rows]


@router.get("/probes")
async def list_adapter_probes(limit: int = 50):
    limit = clamp_limit(limit)
    rows = await database.fetch_all(
        """SELECT ac.*, l.raw_url
           FROM adapter_calibrations ac
           LEFT JOIN lotteries l ON ac.lottery_id = l.id
           ORDER BY ac.id DESC LIMIT :limit""",
        {"limit": limit},
    )
    result = []
    for row in rows:
        item = dict(row)
        item["result"] = parse_json_field(item.get("result"))
        result.append(item)
    return result


@router.post("/{lottery_id}/probe")
async def probe_lottery_adapter(lottery_id: int, data: AdapterProbeRequest, request: Request):
    actor = require_min_role(request, "operator")
    lottery = await database.fetch_one("SELECT * FROM lotteries WHERE id = :id", {"id": lottery_id})
    if not lottery:
        raise HTTPException(404, detail="Lottery not found")
    if not get_platform(lottery["platform"]):
        raise HTTPException(400, detail=f"Unsupported platform: {lottery['platform']}")
    target = validate_lottery_target(lottery["platform"], lottery["raw_url"])
    if not target.valid:
        raise HTTPException(400, detail=target.reason)

    account = await pick_account(data.account_id, lottery["platform"])
    if not account:
        raise HTTPException(400, detail=f"No calibrated ready account is available for {lottery['platform']}")

    plan_binding = {
        "rule_snapshot_id": None,
        "rule_hash": None,
        "action_plan_hash": None,
        "execution_path_id": f"{lottery['platform']}_selector_v1",
        "execution_revision": int(account["execution_revision"] or 0),
    }
    config_hash = compute_config_hash({})
    if lottery["platform"] == "bilibili":
        plan_readiness = await validate_real_run_evidence(lottery, account_id=None)
        if not plan_readiness.get("action_plan_ready"):
            raise HTTPException(
                409,
                detail={
                    "message": "Bilibili API-path probe requires an attested exact Action Plan v2",
                    "blockers": [
                        blocker
                        for blocker in plan_readiness.get("blockers", [])
                        if blocker not in {"execution_account_scope_required", "exact_execution_evidence_required"}
                    ],
                },
            )
        plan_binding = bilibili_plan_binding(
            lottery,
            require_executable=False,
            execution_revision=int(account["execution_revision"] or 0),
        )
        config_hash = plan_binding["config_hash"]

    probe_id = str(uuid.uuid4())
    target_url = validated_probe_navigation_url(lottery["raw_url"])
    target_hash = compute_target_hash(str(lottery["canonical_url"] or ""))
    message = {
        "probe_id": probe_id,
        "platform": lottery["platform"],
        "account_id": str(account["id"]),
        "lottery_id": str(lottery_id),
        "target_url": target_url,
        "canonical_url": str(lottery["canonical_url"] or ""),
        "execution_path_id": str(plan_binding["execution_path_id"] or ""),
        "target_hash": target_hash,
        "rule_snapshot_id": str(plan_binding["rule_snapshot_id"] or ""),
        "rule_hash": str(plan_binding["rule_hash"] or ""),
        "action_plan_hash": str(plan_binding["action_plan_hash"] or ""),
        "config_hash": config_hash,
        "execution_revision": str(plan_binding["execution_revision"] or ""),
    }
    outbox_key = f"adapter-probe:{probe_id}"
    # The canonical queued row and its stream intent commit together. Redis can
    # be temporarily unavailable without stranding a probe that can never be
    # reclaimed or safely retried.
    async with database.transaction():
        locked = await database.fetch_one(
            """SELECT id, platform, raw_url, canonical_url, rule_text, action_plan,
                      authoritative_rule_snapshot_id, rule_hash, action_plan_hash
               FROM lotteries WHERE id = :id FOR UPDATE""",
            {"id": lottery_id},
        )
        require_dispatch_snapshot_unchanged(locked, lottery)
        try:
            account_lease = await acquire_account_operation_lease(
                int(account["id"]),
                operation_kind="adapter_probe",
                owner_id=probe_id,
                expected_execution_revision=int(account["execution_revision"] or 0),
                expected_platform=str(lottery["platform"]),
                db=database,
            )
        except AccountOperationLeaseConflict as exc:
            raise HTTPException(
                409,
                detail={
                    "message": (
                        "Account changed during probe preflight"
                        if exc.code == "account_operation_account_changed"
                        else "Account is already leased by another operation"
                    ),
                    "account_id": exc.account_id,
                    "code": exc.code,
                },
            ) from exc
        message["account_lease_id"] = account_lease.lease_id
        message["account_lease_generation"] = str(account_lease.generation)
        await database.execute(
            """INSERT INTO adapter_calibrations
                 (probe_id, platform, account_id, lottery_id, target_url, status,
                  execution_path_id, target_hash, rule_snapshot_id, rule_hash,
                  action_plan_hash, config_hash, account_lease_id,
                  account_lease_generation)
               VALUES
                 (:probe_id, :platform, :account_id, :lottery_id, :target_url, 'queued',
                  :execution_path_id, :target_hash, :rule_snapshot_id, :rule_hash,
                  :action_plan_hash, :config_hash, :account_lease_id,
                  :account_lease_generation)""",
            {
                "probe_id": probe_id,
                "platform": lottery["platform"],
                "account_id": account["id"],
                "lottery_id": lottery_id,
                "target_url": target_url,
                "execution_path_id": plan_binding["execution_path_id"],
                "target_hash": target_hash,
                "rule_snapshot_id": plan_binding["rule_snapshot_id"],
                "rule_hash": plan_binding["rule_hash"],
                "action_plan_hash": plan_binding["action_plan_hash"],
                "config_hash": config_hash,
                "account_lease_id": account_lease.lease_id,
                "account_lease_generation": account_lease.generation,
            },
        )
        await enqueue_outbox(message, "adapter_probe_requests", dedup_key=outbox_key)
    try:
        await try_flush_dedup(outbox_key)
    except Exception as exc:
        structured_log(
            "warning",
            "adapter_probe_immediate_flush_failed",
            probe_id=probe_id,
            error=str(exc),
        )
    await _record_post_commit_event(
        aggregate="lottery",
        aggregate_id=lottery_id,
        event_type="AdapterProbeQueued",
        payload={
            "probe_id": probe_id,
            "platform": lottery["platform"],
            "account_id": account["id"],
            "target_url": target_url,
            "execution_path_id": plan_binding["execution_path_id"],
            "target_hash": target_hash,
            "rule_snapshot_id": plan_binding["rule_snapshot_id"],
            "rule_hash": plan_binding["rule_hash"],
            "action_plan_hash": plan_binding["action_plan_hash"],
            "config_hash": config_hash,
            "execution_revision": plan_binding["execution_revision"],
            "account_lease_id": account_lease.lease_id,
            "account_lease_generation": account_lease.generation,
        },
        correlation_id=probe_id,
        actor_type="operator",
        actor_id=actor["actor_id"],
    )
    return {
        "status": "queued",
        "probe_id": probe_id,
        "account_id": account["id"],
        "execution_path_id": plan_binding["execution_path_id"],
        "config_hash": config_hash,
        "execution_revision": plan_binding["execution_revision"],
        "account_lease_id": account_lease.lease_id,
        "account_lease_generation": account_lease.generation,
    }


@router.post("/probes/{probe_id}/apply-config")
async def apply_probe_recommended_config(probe_id: str, request: Request):
    """Apply a succeeded probe's recommended selector config to the runtime.

    Closes the last manual step: instead of copy-pasting ``_recommended_config``
    from a probe result into ``PUT /adapters/config``, an admin confirms here and
    the platform's recommendation is saved through the same path with the same
    completeness bar. This only *configures* the adapter 鈥?every other real-run
    gate (global switch, calibrated account, recent shadow-run, reviewed plan,
    no recent risk) still applies, so it can never by itself enable a real run.
    """
    actor = require_min_role(request, "admin")
    require_confirmation(request)
    probe = await database.fetch_one(
        "SELECT probe_id, platform, status, result FROM adapter_calibrations WHERE probe_id = :probe_id",
        {"probe_id": probe_id},
    )
    if not probe:
        raise HTTPException(404, detail="Probe not found")
    if probe["status"] != "succeeded":
        raise HTTPException(409, detail=f"Probe is not succeeded (status: {probe['status']})")

    platform = probe["platform"]
    recommended = recommended_config_from_probe(probe["result"], platform)
    if not recommended:
        raise HTTPException(409, detail="Probe has no recommended selector config to apply")
    if not selector_config_complete(platform, recommended):
        raise HTTPException(
            409,
            detail={
                "message": "Recommended config is incomplete; re-probe or finish it by hand before applying",
                "phases": {phase: phase_configured(platform, recommended, phase) for phase in ADAPTER_PHASES},
            },
        )

    await save_platform_selector_config(platform, recommended)
    phase_status = {phase: phase_configured(platform, recommended, phase) for phase in ADAPTER_PHASES}
    await audit_event(
        request,
        action="adapter_selector_config.apply_probe",
        resource_type="adapter_selector_config",
        resource_id=platform,
        result="saved",
        risk_level="high",
        detail={"probe_id": probe_id, "platform": platform, "phases": phase_status},
    )
    await _record_post_commit_event(
        aggregate="platform",
        aggregate_id=platform,
        event_type="AdapterSelectorConfigSaved",
        payload={"configured": True, "phases": phase_status, "source": "probe", "probe_id": probe_id},
        actor_type="operator",
        actor_id=actor["actor_id"],
    )
    return {"status": "saved", "platform": platform, "probe_id": probe_id, "configured": True, "phases": phase_status}


@router.get("/probes/{probe_id}/screenshot")
async def get_probe_screenshot(probe_id: str):
    row = await database.fetch_one(
        "SELECT screenshot_path FROM adapter_calibrations WHERE probe_id = :probe_id",
        {"probe_id": probe_id},
    )
    if not row or not row["screenshot_path"]:
        raise HTTPException(404, detail="Probe screenshot not found")

    path = Path(row["screenshot_path"]).resolve()
    safe_root = ADAPTER_PROBE_DIR.resolve()
    if not path.exists() or not path.is_file() or not path.is_relative_to(safe_root):
        raise HTTPException(404, detail="Probe screenshot not found")
    return FileResponse(path, media_type="image/png")


@router.get("/tasks/runs/{task_id}/screenshot")
async def get_task_screenshot(task_id: str):
    row = await database.fetch_one(
        "SELECT screenshot_path FROM task_runs WHERE task_id = :task_id",
        {"task_id": task_id},
    )
    if not row or not row["screenshot_path"]:
        raise HTTPException(404, detail="Task screenshot not found")

    path = Path(row["screenshot_path"]).resolve()
    safe_roots = [TASK_FAILURE_DIR.resolve(), TASK_SHADOW_DIR.resolve()]
    if not path.exists() or not path.is_file() or not any(path.is_relative_to(root) for root in safe_roots):
        raise HTTPException(404, detail="Task screenshot not found")
    return FileResponse(path, media_type="image/png")


@router.put("/{lottery_id}/result")
async def update_lottery_result(lottery_id: int, data: LotteryResultUpdate, request: Request):
    actor = require_min_role(request, "operator")
    if data.status not in {"participated", "won", "lost", "expired"}:
        raise HTTPException(400, detail="status must be participated, won, lost, or expired")

    async with database.transaction():
        lottery = await database.fetch_one(
            "SELECT id, status, execution_lock FROM lotteries WHERE id = :id FOR UPDATE",
            {"id": lottery_id},
        )
        require_lottery_not_executing(lottery, operation="record a result")
        await database.execute(
            "UPDATE lotteries SET status = :status WHERE id = :id",
            {"id": lottery_id, "status": data.status},
        )
        if data.note:
            await database.execute(
                """INSERT INTO notify_logs (channel, title, content, success)
                   VALUES ('manual', :title, :content, 1)""",
                {"title": f"Lottery {lottery_id} result", "content": data.note},
            )
        await audit_event(
            request,
            action="lottery.result.update",
            resource_type="lottery",
            resource_id=lottery_id,
            result="saved",
            risk_level="high",
            detail={"status": data.status},
        )
    event_type = {
        "participated": "LotteryJoined",
        "won": "LotteryWon",
        "lost": "LotteryLost",
        "expired": "LotteryExpired",
    }[data.status]
    await _record_post_commit_event(
        aggregate="lottery",
        aggregate_id=lottery_id,
        event_type=event_type,
        payload={"status": data.status, "note": data.note},
        actor_type="operator",
        actor_id=actor["actor_id"],
    )
    return {"status": "updated", "lottery_id": lottery_id, "result": data.status}


async def pick_account(account_id: int | None, platform: str):
    if account_id is not None:
        return await database.fetch_one(
            """SELECT * FROM accounts a
               WHERE a.id = :id
                 AND a.platform = :platform
                 AND a.status = 'ready'
                 AND OCTET_LENGTH(a.encrypted_credential) > 0
                 AND (
                   SELECT c.status FROM account_calibrations c
                   WHERE c.account_id = a.id
                   ORDER BY c.created_at DESC
                   LIMIT 1
                 ) = 'succeeded'
                 AND NOT EXISTS (
                   SELECT 1 FROM account_operation_leases lease
                   WHERE lease.account_id = a.id
                     AND lease.released_at IS NULL
                     AND lease.expires_at > NOW()
                 )""",
            {"id": account_id, "platform": platform},
        )

    recommendations = await load_strategy_account_recommendations(platform)
    recommended = first_or_none(recommendations.get(platform, []))
    if recommended:
        row = await database.fetch_one(
            """SELECT * FROM accounts a
               WHERE a.id = :id
                 AND a.platform = :platform
                 AND a.status = 'ready'
                 AND OCTET_LENGTH(a.encrypted_credential) > 0
                 AND (
                   SELECT c.status FROM account_calibrations c
                   WHERE c.account_id = a.id
                   ORDER BY c.created_at DESC
                   LIMIT 1
                 ) = 'succeeded'
                 AND NOT EXISTS (
                   SELECT 1 FROM account_operation_leases lease
                   WHERE lease.account_id = a.id
                     AND lease.released_at IS NULL
                     AND lease.expires_at > NOW()
                 )""",
            {"id": recommended["account_id"], "platform": platform},
        )
        if row and not (await recent_account_risk(int(row["id"])))["has_recent_risk"]:
            return row

    candidates = await database.fetch_all(
        """SELECT * FROM accounts a
           WHERE a.platform = :platform
             AND a.status = 'ready'
             AND OCTET_LENGTH(a.encrypted_credential) > 0
             AND (
               SELECT c.status FROM account_calibrations c
               WHERE c.account_id = a.id
               ORDER BY c.created_at DESC
               LIMIT 1
             ) = 'succeeded'
             AND NOT EXISTS (
               SELECT 1 FROM account_operation_leases lease
               WHERE lease.account_id = a.id
                 AND lease.released_at IS NULL
                 AND lease.expires_at > NOW()
             )
           ORDER BY daily_task_count ASC, id ASC
           LIMIT 25""",
        {"platform": platform},
    )
    for row in candidates:
        if not (await recent_account_risk(int(row["id"])))["has_recent_risk"]:
            return row

    return await database.fetch_one(
        """SELECT * FROM accounts a
           WHERE a.platform = :platform
             AND a.status = 'ready'
             AND OCTET_LENGTH(a.encrypted_credential) > 0
             AND (
               SELECT c.status FROM account_calibrations c
               WHERE c.account_id = a.id
               ORDER BY c.created_at DESC
               LIMIT 1
             ) = 'succeeded'
             AND NOT EXISTS (
               SELECT 1 FROM account_operation_leases lease
               WHERE lease.account_id = a.id
                 AND lease.released_at IS NULL
                 AND lease.expires_at > NOW()
             )
           ORDER BY daily_task_count ASC, id ASC
           LIMIT 1""",
        {"platform": platform},
    )


def resolve_task_mode(data: DispatchTaskRequest) -> str:
    if data.mode:
        return data.mode.value if hasattr(data.mode, "value") else str(data.mode)
    return "dry_run" if data.dry_run else "real_run"


async def compute_strategy_item(
    item: dict,
    *,
    selector_config: dict,
    real_run_enabled: bool,
    platform_knowledge: dict[str, dict],
    account_recommendations: dict[str, list[dict]],
    include_breakdown: bool = False,
) -> dict:
    """Score a single lottery row against the strategy gate and ranking model.

    Shared by ``/strategy/queue`` (ranking many targets) and
    ``/{lottery_id}/strategy/explain`` (explaining one target), so both
    surfaces stay consistent. ``include_breakdown`` adds the named score
    components used by the explain endpoint.
    """
    platform = item["platform"]
    cfg = get_platform(platform) or {}
    safe_accounts = int(item.get("safe_accounts") or 0)
    active_runs = int(item.get("active_runs") or 0)
    dry_success = int(item.get("dry_success") or 0)
    shadow_success = int(item.get("shadow_success") or 0)
    failed_runs = int(item.get("failed_runs") or 0)
    recent_risk = int(item.get("recent_platform_risk") or 0)
    probe_result = parse_json_field(item.get("latest_probe_result"))
    probe_summary = probe_result.get("_summary") if isinstance(probe_result, dict) else None
    adapter_kind = platform_real_adapter_kind(selector_config, platform)
    adapter_ready = bool(cfg.get("action_adapter")) or platform_has_runtime_real_adapter(selector_config, platform)
    breaker_allowed, breaker_reason = await circuit_breaker_allows(platform)
    target = validate_lottery_target(platform, item["raw_url"])
    target_real_valid = target.valid and not (adapter_kind == "api" and target.kind != "dynamic")
    probe_ready = platform_probe_ready_for_real_actions(platform, probe_summary)

    if target_real_valid:
        recommended_mode, reason_codes, blockers = choose_strategy_mode(
            safe_accounts=safe_accounts,
            active_runs=active_runs,
            dry_success=dry_success,
            shadow_success=shadow_success,
            adapter_ready=adapter_ready,
            probe_ready=probe_ready,
            real_run_enabled=real_run_enabled,
            breaker_allowed=breaker_allowed,
            breaker_reason=breaker_reason,
        )
    else:
        recommended_mode = "blocked"
        reason_codes = ["invalid_lottery_target"]
        blockers = [target.reason or "bilibili_dynamic_target_required"]
    if recent_risk:
        reason_codes.append("recent_platform_risk")
    if failed_runs:
        reason_codes.append("recent_failures")
    if item["value_score"] >= 70:
        reason_codes.append("high_value")

    knowledge = platform_knowledge.get(platform) or empty_platform_knowledge(platform)
    recommended_account = first_or_none(account_recommendations.get(platform, []))
    account_reputation_score = recommended_account["reputation_score"] if recommended_account else 0
    win_probability_breakdown = estimate_win_probability_breakdown(
        knowledge.get("win_rate"),
        knowledge.get("knowledge_confidence", 0),
    )
    estimated_win_probability = win_probability_breakdown["blended"]
    trust_score_breakdown = estimate_trust_score_breakdown(
        account_reputation_score=account_reputation_score,
        recent_platform_risk=recent_risk,
        knowledge_confidence=knowledge.get("knowledge_confidence", 0),
    )
    trust_score = trust_score_breakdown["trust_score"]
    expected_value = round(int(item["value_score"] or 0) * estimated_win_probability * trust_score, 4)
    if knowledge.get("knowledge_confidence", 0) >= 40:
        reason_codes.append("platform_knowledge_used")
    elif knowledge.get("total_lotteries", 0) or knowledge.get("total_runs", 0):
        reason_codes.append("low_knowledge_confidence")
    if knowledge.get("win_rate") is not None:
        reason_codes.append("historical_win_rate_used")
    if recommended_account:
        reason_codes.append("account_reputation_used")
        if account_reputation_score < 60:
            reason_codes.append("account_reputation_low")

    score_breakdown = strategy_score_breakdown(
        value_score=int(item["value_score"] or 0),
        recommended_mode=recommended_mode,
        dry_success=dry_success,
        shadow_success=shadow_success,
        failed_runs=failed_runs,
        recent_risk=recent_risk,
        knowledge_confidence=knowledge.get("knowledge_confidence", 0),
        estimated_win_probability=estimated_win_probability,
        account_reputation_score=account_reputation_score,
        expected_value=expected_value,
    )

    result = {
        "lottery_id": item["id"],
        "platform": platform,
        "raw_url": item["raw_url"],
        "status": item["status"],
        "value_score": item["value_score"],
        "strategy_score": score_breakdown["total"],
        "priority_tier": priority_tier(score_breakdown["total"]) if recommended_mode != "blocked" else "hold",
        "expected_value": expected_value,
        "estimated_win_probability": estimated_win_probability,
        "trust_score": trust_score,
        "recommended_mode": recommended_mode,
        "reason_codes": reason_codes,
        "blockers": blockers,
        "platform_knowledge": knowledge,
        "recommended_account": recommended_account,
        "safe_accounts": safe_accounts,
        "active_runs": active_runs,
        "dry_success": dry_success,
        "shadow_success": shadow_success,
        "failed_runs": failed_runs,
        "recent_platform_risk": recent_risk,
        "adapter_ready": adapter_ready,
        "probe_ready": probe_ready,
        "real_run_enabled": real_run_enabled,
        "breaker_allowed": breaker_allowed,
        "target_valid": target_real_valid,
        "target_kind": target.kind,
    }
    if include_breakdown:
        result["explain"] = {
            "mode": {
                "recommended_mode": recommended_mode,
                "reason_codes": reason_codes,
                "blockers": blockers,
                "breaker_allowed": breaker_allowed,
                "breaker_reason": breaker_reason,
                "target_valid": target_real_valid,
                "target_kind": target.kind,
                "target_error": None
                if target_real_valid
                else (target.reason or "bilibili_dynamic_target_required"),
            },
            "score": score_breakdown,
            "win_probability": win_probability_breakdown,
            "trust_score": trust_score_breakdown,
            "knowledge_confidence": strategy_knowledge_confidence_breakdown(knowledge),
        }
    return result


async def load_strategy_platform_knowledge(window_days: int = 30) -> dict[str, dict]:
    rows = await database.fetch_all(
        """SELECT platform,
                  COUNT(*) AS total_lotteries,
                  SUM(status = 'pending') AS pending_lotteries,
                  SUM(status = 'won') AS won_lotteries,
                  SUM(status = 'lost') AS lost_lotteries,
                  SUM(value_score >= 70) AS high_value_lotteries,
                  AVG(value_score) AS avg_value_score
           FROM lotteries
           WHERE extracted_at >= DATE_SUB(NOW(), INTERVAL :window_days DAY)
           GROUP BY platform""",
        {"window_days": window_days},
    )
    task_rows = await database.fetch_all(
        """SELECT l.platform,
                  COUNT(*) AS total_runs,
                  SUM(tr.status = 'succeeded') AS succeeded_runs,
                  SUM(tr.status = 'failed') AS failed_runs,
                  SUM(COALESCE(tr.task_mode, IF(tr.dry_run = 1, 'dry_run', 'real_run')) = 'shadow_run'
                      AND tr.status = 'succeeded') AS shadow_success,
                  SUM(COALESCE(tr.task_mode, IF(tr.dry_run = 1, 'dry_run', 'real_run')) = 'real_run'
                      AND tr.status = 'succeeded') AS real_success
           FROM task_runs tr
           JOIN lotteries l ON l.id = tr.lottery_id
           WHERE tr.created_at >= DATE_SUB(NOW(), INTERVAL :window_days DAY)
           GROUP BY l.platform""",
        {"window_days": window_days},
    )
    risk_rows = await database.fetch_all(
        """SELECT a.platform,
                  COUNT(*) AS risk_events
           FROM risk_events r
           JOIN accounts a ON a.id = r.account_id
           WHERE r.created_at >= DATE_SUB(NOW(), INTERVAL :window_days DAY)
           GROUP BY a.platform""",
        {"window_days": window_days},
    )

    result: dict[str, dict] = {}
    for platform in get_platforms():
        result[platform] = empty_platform_knowledge(platform)
    for row in rows:
        platform = row["platform"]
        result.setdefault(platform, empty_platform_knowledge(platform))
        won = as_int(row["won_lotteries"])
        lost = as_int(row["lost_lotteries"])
        result[platform].update(
            {
                "total_lotteries": as_int(row["total_lotteries"]),
                "pending_lotteries": as_int(row["pending_lotteries"]),
                "won_lotteries": won,
                "lost_lotteries": lost,
                "high_value_lotteries": as_int(row["high_value_lotteries"]),
                "avg_value_score": round(as_float(row["avg_value_score"]), 2),
                "win_rate": safe_ratio(won, won + lost),
            }
        )
    for row in task_rows:
        platform = row["platform"]
        result.setdefault(platform, empty_platform_knowledge(platform))
        succeeded = as_int(row["succeeded_runs"])
        total_runs = as_int(row["total_runs"])
        result[platform].update(
            {
                "total_runs": total_runs,
                "succeeded_runs": succeeded,
                "failed_runs": as_int(row["failed_runs"]),
                "task_success_rate": safe_ratio(succeeded, total_runs),
                "shadow_success": as_int(row["shadow_success"]),
                "real_success": as_int(row["real_success"]),
            }
        )
    for row in risk_rows:
        platform = row["platform"]
        result.setdefault(platform, empty_platform_knowledge(platform))
        result[platform]["risk_events"] = as_int(row["risk_events"])

    for item in result.values():
        item["knowledge_confidence"] = strategy_knowledge_confidence(item)
    return result


async def load_strategy_account_recommendations(platform: str | None = None, window_days: int = 30) -> dict[str, list[dict]]:
    platform_filter = "AND a.platform = :platform" if platform else ""
    values = {"window_days": window_days}
    if platform:
        values["platform"] = platform
    rows = await database.fetch_all(
        f"""SELECT a.id,
                   a.platform,
                   a.status,
                   a.risk_score,
                   a.daily_task_count,
                   a.last_active_at,
                   (
                     SELECT c.status
                     FROM account_calibrations c
                     WHERE c.account_id = a.id
                     ORDER BY c.created_at DESC
                     LIMIT 1
                   ) AS latest_calibration_status,
                   COALESCE(t.total_runs, 0) AS total_runs,
                   COALESCE(t.succeeded_runs, 0) AS succeeded_runs,
                   COALESCE(t.failed_runs, 0) AS failed_runs,
                   COALESCE(t.dry_runs, 0) AS dry_runs,
                   COALESCE(t.shadow_runs, 0) AS shadow_runs,
                   COALESCE(t.real_runs, 0) AS real_runs,
                   t.latest_run_at,
                   COALESCE(r.risk_events, 0) AS risk_events,
                   r.latest_risk_at
            FROM accounts a
            LEFT JOIN (
              SELECT account_id,
                     COUNT(*) AS total_runs,
                     SUM(status = 'succeeded') AS succeeded_runs,
                     SUM(status = 'failed') AS failed_runs,
                     SUM(COALESCE(task_mode, IF(dry_run = 1, 'dry_run', 'real_run')) = 'dry_run') AS dry_runs,
                     SUM(COALESCE(task_mode, IF(dry_run = 1, 'dry_run', 'real_run')) = 'shadow_run') AS shadow_runs,
                     SUM(COALESCE(task_mode, IF(dry_run = 1, 'dry_run', 'real_run')) = 'real_run') AS real_runs,
                     MAX(created_at) AS latest_run_at
              FROM task_runs
              WHERE created_at >= DATE_SUB(NOW(), INTERVAL :window_days DAY)
              GROUP BY account_id
            ) t ON t.account_id = a.id
            LEFT JOIN (
              SELECT account_id,
                     COUNT(*) AS risk_events,
                     MAX(created_at) AS latest_risk_at
              FROM risk_events
              WHERE created_at >= DATE_SUB(NOW(), INTERVAL :window_days DAY)
              GROUP BY account_id
            ) r ON r.account_id = a.id
            WHERE a.status = 'ready'
              AND OCTET_LENGTH(a.encrypted_credential) > 0
              AND (
                SELECT c.status
                FROM account_calibrations c
                WHERE c.account_id = a.id
                ORDER BY c.created_at DESC
                LIMIT 1
              ) = 'succeeded'
              {platform_filter}
            ORDER BY a.platform ASC, a.daily_task_count ASC, a.id ASC""",
        values,
    )

    grouped: dict[str, list[dict]] = {}
    for row in rows:
        item = dict(row)
        reputation = account_reputation(
            status=item["status"],
            risk_score=as_int(item["risk_score"]),
            latest_calibration_status=item.get("latest_calibration_status"),
            total_runs=as_int(item["total_runs"]),
            succeeded_runs=as_int(item["succeeded_runs"]),
            failed_runs=as_int(item["failed_runs"]),
            shadow_runs=as_int(item["shadow_runs"]),
            real_runs=as_int(item["real_runs"]),
            risk_events=as_int(item["risk_events"]),
        )
        recommendation = {
            "account_id": item["id"],
            "platform": item["platform"],
            "reputation_score": reputation,
            "account_tier": account_tier(reputation),
            "risk_score": as_int(item["risk_score"]),
            "risk_events": as_int(item["risk_events"]),
            "daily_task_count": as_int(item["daily_task_count"]),
            "total_runs": as_int(item["total_runs"]),
            "succeeded_runs": as_int(item["succeeded_runs"]),
            "failed_runs": as_int(item["failed_runs"]),
            "dry_runs": as_int(item["dry_runs"]),
            "shadow_runs": as_int(item["shadow_runs"]),
            "real_runs": as_int(item["real_runs"]),
            "task_success_rate": safe_ratio(as_int(item["succeeded_runs"]), as_int(item["total_runs"])),
            "latest_run_at": item["latest_run_at"],
            "latest_risk_at": item["latest_risk_at"],
            "last_active_at": item["last_active_at"],
        }
        grouped.setdefault(item["platform"], []).append(recommendation)

    for items in grouped.values():
        items.sort(
            key=lambda account: (
                account["reputation_score"],
                -account["daily_task_count"],
                -account["risk_events"],
                -account["failed_runs"],
            ),
            reverse=True,
        )
    return grouped


def safe_ratio(numerator: int, denominator: int) -> float | None:
    if not denominator:
        return None
    return round(numerator / denominator, 4)


def as_int(value) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def as_float(value) -> float:
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def serialize_lottery(row) -> dict:
    item = dict(row)
    item["action_plan"] = parse_json_field(item.get("action_plan"))
    return item


async def save_platform_selector_config(platform: str, config: dict):
    row = await database.fetch_one(
        "SELECT id FROM adapter_selector_configs WHERE platform = :platform",
        {"platform": platform},
    )
    values = {"platform": platform, "config_json": json.dumps(config, ensure_ascii=False)}
    if row:
        await database.execute(
            "UPDATE adapter_selector_configs SET config_json = :config_json, updated_at = NOW() WHERE platform = :platform",
            values,
        )
        return
    await database.execute(
        "INSERT INTO adapter_selector_configs (platform, config_json) VALUES (:platform, :config_json)",
        values,
    )


def parse_target_lines(content: str, default_platform: str, default_score: int):
    rows = []
    for index, raw_line in enumerate(content.splitlines(), start=1):
        raw = raw_line.strip()
        if not raw or raw.startswith("#"):
            continue
        parts = [part.strip() for part in raw.replace("\t", ",").split(",")]
        parts = [part for part in parts if part]
        platform = default_platform
        url = parts[0] if parts else ""
        score = default_score
        expires_at = None

        if len(parts) >= 2 and not looks_like_url(parts[0]):
            platform = parts[0]
            url = parts[1]
            if len(parts) >= 3:
                score = parse_int(parts[2], default_score)
            if len(parts) >= 4:
                expires_at = parts[3]
        elif len(parts) >= 2:
            score = parse_int(parts[1], default_score)
            if len(parts) >= 3:
                expires_at = parts[2]

        if not get_platform(platform):
            rows.append({"line": index, "raw": raw, "error": f"Unsupported platform: {platform}"})
            continue
        if not looks_like_url(url):
            rows.append({"line": index, "raw": raw, "error": "Invalid URL"})
            continue

        rows.append(
            {
                "line": index,
                "raw": raw,
                "platform": platform,
                "raw_url": url,
                "value_score": score,
                "expires_at": expires_at,
            }
        )
    return rows


def looks_like_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


async def canonicalize_lottery_url(platform: str, raw_url: str) -> str:
    return await canonicalize_platform_url(platform, raw_url)


def parse_int(value: str, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default
