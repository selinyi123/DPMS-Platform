import json
import uuid
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from app.adapter_config import PHASES as ADAPTER_PHASES, load_runtime_selector_config, selector_config_complete
from app.db import database, redis
from app.event_store.service import record_event
from app.knowledge.service import account_reputation
from app.models.schemas import (
    AdapterSelectorConfigUpdate,
    AdapterProbeRequest,
    DispatchTaskRequest,
    LotteryCreate,
    LotteryResultUpdate,
    LotteryTargetImport,
    TrackedSourceCreate,
)
from app.platforms import get_platform, get_platforms
from app.services.discovery import run_discovery
from app.security import (
    audit_event,
    circuit_breaker_allows,
    is_real_run_enabled,
    require_confirmation,
    require_min_role,
)
from app.utils.canonicalizer import GenericCanonicalizer


router = APIRouter()
PHASES = ["followed", "liked", "commented", "reposted"]
TASK_FAILURE_DIR = Path("/profiles/task-failures")
TASK_SHADOW_DIR = Path("/profiles/shadow-runs")
ADAPTER_PROBE_DIR = Path("/profiles/adapter-probes")


@router.get("/adapters")
async def list_adapters():
    selector_config = await load_runtime_selector_config()
    return [
        {
            "platform": key,
            "label": cfg["label"],
            "dry_run": True,
            "real_actions": cfg.get("action_adapter", False) or platform_selectors_complete(selector_config, key),
            "adapter_status": "configured" if platform_selectors_complete(selector_config, key) else cfg.get("adapter_status", "planned"),
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
                "configured": configured_complete,
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
        await record_event(
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
    await record_event(
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
    query = "SELECT * FROM lotteries"
    values = {"limit": limit}
    if status:
        query += " WHERE status = :status"
        values["status"] = status
    query += " ORDER BY id DESC LIMIT :limit"
    rows = await database.fetch_all(query, values)
    return [dict(r) for r in rows]


@router.get("/real-run/evidence")
async def list_real_run_evidence(status: str = None, limit: int = 50):
    query = "SELECT * FROM lotteries"
    values = {"limit": min(max(limit, 1), 100)}
    if status:
        query += " WHERE status = :status"
        values["status"] = status
    query += " ORDER BY id DESC LIMIT :limit"
    rows = await database.fetch_all(query, values)
    selector_config = await load_runtime_selector_config()
    real_run_enabled = await is_real_run_enabled()
    items = []
    for row in rows:
        lottery = dict(row)
        gate = await real_run_gate_status(lottery, selector_config=selector_config, real_run_enabled=real_run_enabled)
        items.append(gate)
    return {"items": items}


@router.get("/strategy/queue")
async def strategy_queue(limit: int = 20):
    selector_config = await load_runtime_selector_config()
    real_run_enabled = await is_real_run_enabled()
    platform_knowledge = await load_strategy_platform_knowledge()
    account_recommendations = await load_strategy_account_recommendations()
    rows = await database.fetch_all(
        """SELECT l.*,
                  (
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
                  ) AS latest_probe_result
           FROM lotteries l
           WHERE l.status IN ('pending','claimed')
           ORDER BY l.value_score DESC, l.id ASC
           LIMIT :limit""",
        {"limit": min(max(limit, 1), 100)},
    )

    items = []
    for row in rows:
        item = dict(row)
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
        adapter_ready = bool(cfg.get("action_adapter")) or platform_selectors_complete(selector_config, platform)
        probe_ready = bool(probe_summary and probe_summary.get("ready_for_real_actions"))
        breaker_allowed, breaker_reason = await circuit_breaker_allows(platform)

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
        if recent_risk:
            reason_codes.append("recent_platform_risk")
        if failed_runs:
            reason_codes.append("recent_failures")
        if item["value_score"] >= 70:
            reason_codes.append("high_value")

        knowledge = platform_knowledge.get(platform) or empty_platform_knowledge(platform)
        recommended_account = first_or_none(account_recommendations.get(platform, []))
        account_reputation_score = recommended_account["reputation_score"] if recommended_account else 0
        estimated_win_probability = estimate_win_probability(
            knowledge.get("win_rate"),
            knowledge.get("knowledge_confidence", 0),
        )
        trust_score = estimate_trust_score(
            account_reputation_score=account_reputation_score,
            recent_platform_risk=recent_risk,
            knowledge_confidence=knowledge.get("knowledge_confidence", 0),
        )
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

        score = strategy_score(
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
        items.append(
            {
                "lottery_id": item["id"],
                "platform": platform,
                "raw_url": item["raw_url"],
                "status": item["status"],
                "value_score": item["value_score"],
                "strategy_score": score,
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
            }
        )

    items.sort(key=lambda row: row["strategy_score"], reverse=True)
    for rank, item in enumerate(items, start=1):
        item["rank"] = rank
    return {"items": items, "count": len(items)}


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
    await record_event(
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
    await record_event(
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

    canonical_url = data.canonical_url or data.raw_url.strip()
    try:
        lottery_id = await database.execute(
            """INSERT INTO lotteries (platform, source_type, source_id, raw_url, canonical_url, value_score, expires_at, status)
               VALUES (:platform, :source_type, :source_id, :raw_url, :canonical_url, :value_score, :expires_at, 'pending')""",
            {
                "platform": data.platform,
                "source_type": data.source_type,
                "source_id": data.source_id,
                "raw_url": data.raw_url.strip(),
                "canonical_url": canonical_url,
                "value_score": data.value_score,
                "expires_at": data.expires_at,
            },
        )
        await record_event(
            aggregate="lottery",
            aggregate_id=lottery_id,
            event_type="LotteryDiscovered",
            payload={
                "platform": data.platform,
                "source_type": data.source_type,
                "source_id": data.source_id,
                "raw_url": data.raw_url.strip(),
                "canonical_url": canonical_url,
                "value_score": data.value_score,
                "manual": True,
            },
            actor_type="operator",
            actor_id=actor["actor_id"],
        )
        return {"status": "created", "id": lottery_id}
    except Exception:
        row = await database.fetch_one(
            "SELECT id FROM lotteries WHERE canonical_url = :canonical_url",
            {"canonical_url": canonical_url},
        )
        if row:
            return {"status": "exists", "id": row["id"]}
        raise


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

    import_id = str(uuid.uuid4())
    created = []
    duplicates = []
    invalid = []
    for row in rows:
        if row.get("error"):
            invalid.append(row)
            continue
        try:
            canonical_url = await GenericCanonicalizer.canonicalize(row["platform"], row["raw_url"])
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
            created.append({"line": row["line"], "id": lottery_id, "url": row["raw_url"], "platform": row["platform"]})
            await record_event(
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
        except Exception:
            existing = await database.fetch_one(
                "SELECT id FROM lotteries WHERE canonical_url = :canonical_url",
                {"canonical_url": await GenericCanonicalizer.canonicalize(row["platform"], row["raw_url"])},
            )
            if existing:
                duplicates.append({"line": row["line"], "id": existing["id"], "url": row["raw_url"], "platform": row["platform"]})
            else:
                invalid.append({"line": row["line"], "raw": row["raw"], "error": "insert failed"})

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
    await record_event(
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
    real_adapter_enabled = platform_cfg.get("action_adapter", False) or platform_selectors_complete(selector_config, lottery["platform"])
    task_mode = resolve_task_mode(data)
    dry_run = task_mode != "real_run"

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
            evidence = await validate_real_run_evidence(lottery, account_id=data.account_id)
            if not evidence["allowed"]:
                raise HTTPException(409, detail={"message": "Real-run evidence gate is not satisfied", "blockers": evidence["blockers"]})
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
            await record_event(
                aggregate="lottery",
                aggregate_id=lottery_id,
                event_type="RealRunDenied",
                payload={"platform": lottery["platform"], "reason": exc.detail},
                actor_type="operator",
                actor_id=actor["actor_id"],
            )
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
            await record_event(
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
        await record_event(
            aggregate="lottery",
            aggregate_id=lottery_id,
            event_type="TaskDispatchBlocked",
            payload={"platform": lottery["platform"], "reason": "no_available_account", "dry_run": dry_run, "mode": task_mode},
            actor_type="operator",
            actor_id=actor["actor_id"],
        )
        raise HTTPException(400, detail="No available account. Create a ready account first.")

    if task_mode == "real_run":
        evidence = await validate_real_run_evidence(lottery, account_id=account["id"])
        if not evidence["allowed"]:
            await record_event(
                aggregate="lottery",
                aggregate_id=lottery_id,
                event_type="RealRunDenied",
                payload={"platform": lottery["platform"], "account_id": account["id"], "blockers": evidence["blockers"]},
                actor_type="operator",
                actor_id=actor["actor_id"],
            )
            raise HTTPException(409, detail={"message": "Real-run evidence gate is not satisfied", "blockers": evidence["blockers"]})

    task_id = str(uuid.uuid4())
    await database.execute(
        """INSERT INTO task_runs (task_id, account_id, lottery_id, status, dry_run, task_mode)
           VALUES (:task_id, :account_id, :lottery_id, 'queued', :dry_run, :task_mode)""",
        {
            "task_id": task_id,
            "account_id": account["id"],
            "lottery_id": lottery_id,
            "dry_run": int(dry_run),
            "task_mode": task_mode,
        },
    )
    await database.execute(
        "UPDATE lotteries SET status = 'claimed', execution_lock = :task_id, locked_at = NOW() WHERE id = :id",
        {"task_id": task_id, "id": lottery_id},
    )
    await redis.xadd(
        "lottery_tasks",
        {
            "task_id": task_id,
            "account_id": str(account["id"]),
            "lottery_id": str(lottery_id),
            "platform": lottery["platform"],
            "raw_url": lottery["raw_url"],
            "canonical_url": lottery["canonical_url"],
            "dry_run": "1" if dry_run else "0",
            "mode": task_mode,
            "selector_config": json.dumps(platform_selectors, ensure_ascii=False) if isinstance(platform_selectors, dict) else "{}",
        },
    )
    if task_mode == "real_run":
        await audit_event(
            request,
            action="lottery.dispatch.real",
            resource_type="lottery",
            resource_id=lottery_id,
            result="queued",
            risk_level="critical",
            detail={"platform": lottery["platform"], "task_id": task_id, "account_id": account["id"]},
        )
    elif task_mode == "shadow_run":
        await audit_event(
            request,
            action="lottery.dispatch.shadow",
            resource_type="lottery",
            resource_id=lottery_id,
            result="queued",
            risk_level="medium",
            detail={"platform": lottery["platform"], "task_id": task_id, "account_id": account["id"]},
        )
    await record_event(
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
        },
        correlation_id=task_id,
        actor_type="operator",
        actor_id=actor["actor_id"],
    )
    await record_event(
        aggregate="lottery",
        aggregate_id=lottery_id,
        event_type="LotteryDispatchQueued",
        payload={"task_id": task_id, "account_id": account["id"], "dry_run": dry_run, "mode": task_mode},
        correlation_id=task_id,
        actor_type="operator",
        actor_id=actor["actor_id"],
    )
    return {"status": "queued", "task_id": task_id, "account_id": account["id"], "lottery_id": lottery_id, "mode": task_mode}


@router.get("/tasks/runs")
async def list_task_runs(limit: int = 50):
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

    account = await pick_account(data.account_id, lottery["platform"])
    if not account:
        raise HTTPException(400, detail=f"No calibrated ready account is available for {lottery['platform']}")

    probe_id = str(uuid.uuid4())
    target_url = lottery["canonical_url"] or lottery["raw_url"]
    await database.execute(
        """INSERT INTO adapter_calibrations (probe_id, platform, account_id, lottery_id, target_url, status)
           VALUES (:probe_id, :platform, :account_id, :lottery_id, :target_url, 'queued')""",
        {
            "probe_id": probe_id,
            "platform": lottery["platform"],
            "account_id": account["id"],
            "lottery_id": lottery_id,
            "target_url": target_url,
        },
    )
    await redis.xadd(
        "adapter_probe_requests",
        {
            "probe_id": probe_id,
            "platform": lottery["platform"],
            "account_id": str(account["id"]),
            "lottery_id": str(lottery_id),
            "target_url": target_url,
        },
    )
    await record_event(
        aggregate="lottery",
        aggregate_id=lottery_id,
        event_type="AdapterProbeQueued",
        payload={"probe_id": probe_id, "platform": lottery["platform"], "account_id": account["id"], "target_url": target_url},
        correlation_id=probe_id,
        actor_type="operator",
        actor_id=actor["actor_id"],
    )
    return {"status": "queued", "probe_id": probe_id, "account_id": account["id"]}


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

    lottery = await database.fetch_one("SELECT id FROM lotteries WHERE id = :id", {"id": lottery_id})
    if not lottery:
        raise HTTPException(404, detail="Lottery not found")

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
    event_type = {
        "participated": "LotteryJoined",
        "won": "LotteryWon",
        "lost": "LotteryLost",
        "expired": "LotteryExpired",
    }[data.status]
    await record_event(
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
                 ) = 'succeeded'""",
            {"id": account_id, "platform": platform},
        )

    recommendations = await load_strategy_account_recommendations(platform)
    recommended = first_or_none(recommendations.get(platform, []))
    if recommended:
        return await database.fetch_one("SELECT * FROM accounts WHERE id = :id", {"id": recommended["account_id"]})

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
           ORDER BY daily_task_count ASC, id ASC
           LIMIT 1""",
        {"platform": platform},
    )


def resolve_task_mode(data: DispatchTaskRequest) -> str:
    if data.mode:
        return data.mode.value if hasattr(data.mode, "value") else str(data.mode)
    return "dry_run" if data.dry_run else "real_run"


def choose_strategy_mode(
    *,
    safe_accounts: int,
    active_runs: int,
    dry_success: int,
    shadow_success: int,
    adapter_ready: bool,
    probe_ready: bool,
    real_run_enabled: bool,
    breaker_allowed: bool,
    breaker_reason: str | None,
) -> tuple[str, list[str], list[str]]:
    reasons = []
    blockers = []
    if active_runs > 0:
        return "blocked", ["active_run_in_progress"], ["target already has an active run"]
    if safe_accounts <= 0:
        return "blocked", ["no_safe_account"], ["no calibrated ready account"]
    if not breaker_allowed:
        return "blocked", ["circuit_breaker_open"], [breaker_reason or "circuit breaker open"]
    if dry_success <= 0:
        return "dry_run", ["dry_validation_needed"], blockers
    if shadow_success <= 0:
        return "shadow_run", ["shadow_validation_needed"], blockers
    if adapter_ready and probe_ready and real_run_enabled:
        return "real_run", ["real_gate_ready"], blockers
    if not adapter_ready:
        reasons.append("adapter_not_ready")
    if not probe_ready:
        reasons.append("probe_not_ready")
    if not real_run_enabled:
        reasons.append("real_run_disabled")
    return "shadow_run", reasons or ["real_gate_not_ready"], blockers


def strategy_score(
    *,
    value_score: int,
    recommended_mode: str,
    dry_success: int,
    shadow_success: int,
    failed_runs: int,
    recent_risk: int,
    knowledge_confidence: int,
    estimated_win_probability: float,
    account_reputation_score: int,
    expected_value: float,
) -> float:
    mode_multiplier = {
        "real_run": 1.0,
        "shadow_run": 0.78,
        "dry_run": 0.52,
        "blocked": 0.0,
    }.get(recommended_mode, 0.0)
    validation_bonus = min(dry_success, 3) * 3 + min(shadow_success, 3) * 6
    risk_penalty = min(recent_risk * 8 + failed_runs * 7, 45)
    knowledge_bonus = min(knowledge_confidence * 0.08, 8)
    account_bonus = min(max(account_reputation_score - 50, 0) * 0.16, 8)
    expected_value_bonus = min(expected_value * 10, 12)
    probability_bonus = min(estimated_win_probability * 20, 10)
    return round(
        max(
            0,
            value_score * mode_multiplier
            + validation_bonus
            + knowledge_bonus
            + account_bonus
            + expected_value_bonus
            + probability_bonus
            - risk_penalty,
        ),
        2,
    )


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


def empty_platform_knowledge(platform: str) -> dict:
    return {
        "platform": platform,
        "total_lotteries": 0,
        "pending_lotteries": 0,
        "won_lotteries": 0,
        "lost_lotteries": 0,
        "high_value_lotteries": 0,
        "avg_value_score": 0,
        "win_rate": None,
        "total_runs": 0,
        "succeeded_runs": 0,
        "failed_runs": 0,
        "task_success_rate": None,
        "shadow_success": 0,
        "real_success": 0,
        "risk_events": 0,
        "knowledge_confidence": 0,
    }


def strategy_knowledge_confidence(item: dict) -> int:
    result_labels = as_int(item.get("won_lotteries")) + as_int(item.get("lost_lotteries"))
    return clamp(
        min(as_int(item.get("total_lotteries")) * 4, 28)
        + min(result_labels * 8, 24)
        + min(as_int(item.get("total_runs")) * 5, 22)
        + min(as_int(item.get("shadow_success")) * 8, 16)
        + min(as_int(item.get("real_success")) * 8, 10),
        0,
        100,
    )


def estimate_win_probability(win_rate: float | None, knowledge_confidence: int) -> float:
    baseline = 0.05
    if win_rate is None:
        return baseline
    confidence_weight = min(max(knowledge_confidence, 0) / 100, 0.75)
    return round(baseline * (1 - confidence_weight) + float(win_rate) * confidence_weight, 4)


def estimate_trust_score(*, account_reputation_score: int, recent_platform_risk: int, knowledge_confidence: int) -> float:
    account_trust = max(0.25, min(account_reputation_score, 100) / 100) if account_reputation_score else 0.35
    risk_penalty = min(recent_platform_risk * 0.08, 0.48)
    confidence_bonus = min(max(knowledge_confidence, 0) / 100 * 0.12, 0.12)
    return round(max(0.05, min(account_trust + confidence_bonus - risk_penalty, 1.0)), 4)


def first_or_none(items):
    return items[0] if items else None


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


def clamp(value: int | float, low: int, high: int) -> int:
    return int(max(low, min(high, round(value))))


def parse_json_field(value):
    if isinstance(value, (dict, list)) or value is None:
        return value
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    try:
        return json.loads(value)
    except Exception:
        return value


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


def platform_selectors_complete(selector_config: dict, platform: str) -> bool:
    configured = selector_config.get(platform, {})
    return selector_config_complete(platform, configured)


def phase_configured(platform: str, config: dict, phase: str) -> bool:
    value = config.get(phase) if isinstance(config, dict) else None
    if platform != "bilibili":
        return bool(value)
    if phase == "commented":
        return isinstance(value, dict) and bool(
            selector_values(value.get("input") or value.get("inputs"))
            and selector_values(value.get("submit") or value.get("submits"))
        )
    if isinstance(value, dict):
        value = value.get("click") or value.get("selectors") or value.get("buttons")
    return bool(selector_values(value))


def selector_values(value) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


async def validate_real_run_evidence(lottery, account_id: int | None = None) -> dict:
    blockers = []
    probe_values = {"platform": lottery["platform"], "lottery_id": lottery["id"]}
    task_values = {"lottery_id": lottery["id"]}
    account_filter = ""
    if account_id is not None:
        account_filter = "AND account_id = :account_id"
        probe_values["account_id"] = account_id
        task_values["account_id"] = account_id

    probe = await database.fetch_one(
        f"""SELECT result, status, created_at
            FROM adapter_calibrations
            WHERE platform = :platform
              AND lottery_id = :lottery_id
              AND status = 'succeeded'
              AND created_at >= DATE_SUB(NOW(), INTERVAL 24 HOUR)
              {account_filter}
            ORDER BY id DESC
            LIMIT 1""",
        probe_values,
    )
    probe_summary = None
    if probe and probe["result"]:
        probe_result = parse_json_field(probe["result"])
        probe_summary = probe_result.get("_summary") if isinstance(probe_result, dict) else None
    if not probe_summary or not probe_summary.get("ready_for_real_actions"):
        blockers.append("recent_complete_probe_required")

    shadow = await database.fetch_one(
        f"""SELECT task_id, finished_at
            FROM task_runs
            WHERE lottery_id = :lottery_id
              AND task_mode = 'shadow_run'
              AND status = 'succeeded'
              AND finished_at >= DATE_SUB(NOW(), INTERVAL 24 HOUR)
              {account_filter}
            ORDER BY id DESC
            LIMIT 1""",
        task_values,
    )
    if not shadow:
        blockers.append("recent_shadow_run_required")

    if account_id is not None:
        risk = await database.fetch_one(
            """SELECT COUNT(*) AS cnt
               FROM risk_events
               WHERE account_id = :account_id
                 AND created_at >= DATE_SUB(NOW(), INTERVAL 24 HOUR)""",
            {"account_id": account_id},
        )
        if int(risk["cnt"] or 0) > 0:
            blockers.append("recent_account_risk_event")

    return {
        "allowed": not blockers,
        "blockers": blockers,
        "probe_ready": bool(probe_summary and probe_summary.get("ready_for_real_actions")),
        "shadow_ready": bool(shadow),
    }


async def real_run_gate_status(lottery, *, selector_config: dict, real_run_enabled: bool, account_id: int | None = None) -> dict:
    platform = lottery["platform"]
    cfg = get_platform(platform) or {}
    safe_accounts = await database.fetch_one(
        """SELECT COUNT(*) AS cnt
           FROM accounts a
           WHERE a.platform = :platform
             AND a.status = 'ready'
             AND OCTET_LENGTH(a.encrypted_credential) > 0
             AND (
               SELECT c.status FROM account_calibrations c
               WHERE c.account_id = a.id
               ORDER BY c.created_at DESC
               LIMIT 1
             ) = 'succeeded'""",
        {"platform": platform},
    )
    selector_ready = platform_selectors_complete(selector_config, platform)
    adapter_enabled = bool(cfg.get("action_adapter")) or selector_ready
    evidence = await validate_real_run_evidence(lottery, account_id=account_id)
    blockers = list(evidence["blockers"])
    if not int(safe_accounts["cnt"] or 0):
        blockers.insert(0, "no_calibrated_ready_account")
    if not adapter_enabled:
        blockers.insert(0, "real_adapter_not_enabled")
    if not real_run_enabled:
        blockers.insert(0, "global_real_run_disabled")

    next_action = "real_run"
    if "no_calibrated_ready_account" in blockers:
        next_action = "add_account"
    elif "recent_account_risk_event" in blockers:
        next_action = "review_risk"
    elif "recent_complete_probe_required" in blockers:
        next_action = "probe"
    elif not selector_ready:
        next_action = "configure_adapter"
    elif "recent_shadow_run_required" in blockers:
        next_action = "shadow_run"
    elif "global_real_run_disabled" in blockers:
        next_action = "enable_real_run"

    return {
        "lottery_id": lottery["id"],
        "platform": platform,
        "allowed": not blockers,
        "blockers": blockers,
        "next_action": next_action,
        "real_run_enabled": real_run_enabled,
        "adapter_enabled": adapter_enabled,
        "selector_ready": selector_ready,
        "safe_accounts": int(safe_accounts["cnt"] or 0),
        "probe_ready": evidence["probe_ready"],
        "shadow_ready": evidence["shadow_ready"],
    }


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


def parse_int(value: str, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default
