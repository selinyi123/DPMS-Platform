
import time, psutil, json, asyncio

from fastapi import APIRouter, Request

from fastapi.responses import StreamingResponse

from app.config import settings
from app.adapter_config import PHASES as ADAPTER_PHASES, load_runtime_selector_config
from app.db import database, redis
from app.api.notify import configured_channels
from app.models.schemas import RealRunSettingUpdate
from app.platforms import get_platforms
from app.security import audit_event, is_real_run_enabled, require_confirmation, require_min_role, set_runtime_setting

from app.utils.log import structured_log



router = APIRouter()



@router.get("/overview")

async def metrics_overview():

    pending_info = await redis.xpending("lottery_tasks", "workers")

    consumers = await redis.xinfo_consumers("lottery_tasks", "workers")

    workers_online = count_active_consumers(consumers)
    heartbeat_workers_online = await count_worker_heartbeats()
    workers_online = max(workers_online, heartbeat_workers_online)

    accounts = await database.fetch_all("SELECT status, COUNT(*) as cnt FROM accounts GROUP BY status")

    status_map = {r["status"]: r["cnt"] for r in accounts}

    today_count = await redis.get("daily_limit:total") or 0

    mem = psutil.virtual_memory()



    return {

        "pending": pending_info.get("pending", 0),

        "workers_online": workers_online,

        "accounts_ready": status_map.get("ready", 0),

        "accounts_cooling": status_map.get("cooling", 0),

        "accounts_frozen": status_map.get("frozen", 0),

        "today_tasks": int(today_count),

        "memory_mb": round(mem.used / (1024*1024)),

        "memory_percent": mem.percent,

    }


@router.get("/readiness")

async def readiness():

    workers_online = 0
    try:
        consumers = await redis.xinfo_consumers("lottery_tasks", "workers")
        workers_online = count_active_consumers(consumers)
    except Exception:
        workers_online = 0
    heartbeat_workers_online = await count_worker_heartbeats()
    workers_online = max(workers_online, heartbeat_workers_online)
    real_run_enabled = await is_real_run_enabled()

    selector_config = await load_runtime_selector_config()
    platforms = []

    for platform, cfg in get_platforms().items():

        safe_accounts = await database.fetch_one(

            """SELECT COUNT(*) AS cnt FROM accounts a
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

        latest_probe = await database.fetch_one(

            """SELECT status, result, error_message, created_at
               FROM adapter_calibrations
               WHERE platform = :platform
               ORDER BY id DESC
               LIMIT 1""",

            {"platform": platform},

        )

        probe_result = parse_json_field(latest_probe["result"]) if latest_probe and latest_probe["result"] else None

        probe_summary = probe_result.get("_summary") if isinstance(probe_result, dict) else None

        runtime_adapter_ready = platform_selectors_complete(selector_config, platform)
        action_adapter_enabled = bool(cfg.get("action_adapter")) or runtime_adapter_ready
        real_actions_ready = action_adapter_enabled and bool(probe_summary and probe_summary.get("ready_for_real_actions"))

        blockers = []
        blocker_codes = []

        if not safe_accounts["cnt"]:

            blockers.append("no calibrated ready account")
            blocker_codes.append("no_calibrated_ready_account")

        if not action_adapter_enabled:

            blockers.append("real adapter not enabled")
            blocker_codes.append("real_adapter_not_enabled")

        if action_adapter_enabled and not real_actions_ready:

            blockers.append("adapter probe is incomplete")
            blocker_codes.append("adapter_probe_incomplete")

        if not real_run_enabled:

            blockers.append("global real-run switch is disabled")
            blocker_codes.append("global_real_run_disabled")

        platforms.append(

            {

                "platform": platform,

                "label": cfg["label"],

                "safe_accounts": safe_accounts["cnt"],

                "qr_login": bool(cfg.get("qr_login")),

                "cookie_login": bool(cfg.get("cookie_login")),

                "adapter_status": "configured" if runtime_adapter_ready else cfg.get("adapter_status", "planned"),

                "action_adapter": action_adapter_enabled,

                "real_actions_ready": real_actions_ready,

                "latest_probe": {

                    "status": latest_probe["status"],

                    "created_at": latest_probe["created_at"],

                    "ready_phase_count": probe_summary.get("ready_phase_count") if probe_summary else None,

                    "ready_for_real_actions": probe_summary.get("ready_for_real_actions") if probe_summary else False,

                    "error_message": latest_probe["error_message"],

                } if latest_probe else None,

                "blockers": blockers,
                "blocker_codes": blocker_codes,

                "ready_for_dry_run": bool(safe_accounts["cnt"]),

                "ready_for_real_run": real_run_enabled and real_actions_ready and bool(safe_accounts["cnt"]),

            }

        )

    recent_risk = await database.fetch_one(

        """SELECT COUNT(*) AS cnt FROM risk_events
           WHERE created_at >= DATE_SUB(NOW(), INTERVAL 24 HOUR)"""

    )

    notification_configured = len(await configured_channels())

    proxy_exits = await database.fetch_one("SELECT COUNT(*) AS cnt FROM proxies")
    active_proxy_exits = await database.fetch_one("SELECT COUNT(*) AS cnt FROM proxies WHERE status = 'active' AND (cooldown_until IS NULL OR cooldown_until < NOW())")
    proxied_safe_accounts = await database.fetch_one(
        """SELECT COUNT(*) AS cnt
           FROM accounts a
           JOIN proxies p ON p.id = a.proxy_id
           WHERE a.status = 'ready'
             AND OCTET_LENGTH(a.encrypted_credential) > 0
             AND p.status = 'active'
             AND (p.cooldown_until IS NULL OR p.cooldown_until < NOW())"""
    )
    pending_targets = await database.fetch_one(
        "SELECT COUNT(*) AS cnt FROM lotteries WHERE status IN ('pending', 'claimed')"
    )

    summary = {

        "platforms_total": len(platforms),

        "dry_run_ready": sum(1 for item in platforms if item["ready_for_dry_run"]),

        "real_run_ready": sum(1 for item in platforms if item["ready_for_real_run"]),

        "safe_accounts_total": sum(item["safe_accounts"] for item in platforms),

        "notification_channels_configured": notification_configured,

        "recent_risk_events_24h": recent_risk["cnt"],

        "proxy_exits_total": proxy_exits["cnt"],
        "active_proxy_exits": active_proxy_exits["cnt"],
        "proxied_safe_accounts": proxied_safe_accounts["cnt"],
        "pending_targets": pending_targets["cnt"],
        "workers_online": workers_online,
        "worker_heartbeats_online": heartbeat_workers_online,
        "real_run_enabled": real_run_enabled,

    }

    production_checks = build_production_checks(platforms, summary)
    summary["production_ready"] = all(check["passed"] for check in production_checks if check["priority"] == "P0")

    return {

        "platforms": platforms,

        "summary": summary,

        "actions": build_next_actions(platforms, summary),
        "production_checks": production_checks,

    }



async def log_stream():

    pubsub = redis.pubsub()

    await pubsub.subscribe("structured_logs")

    try:

        while True:

            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1)

            if message:

                yield f"data: {message['data']}\n\n"

            else:

                yield ": heartbeat\n\n"

            await asyncio.sleep(0.1)

    except asyncio.CancelledError:

        pass

    finally:

        try:

            await pubsub.unsubscribe("structured_logs")

            await pubsub.close()

        except Exception:

            pass



@router.get("/stream")

async def stream():

    return StreamingResponse(log_stream(), media_type="text/event-stream")



@router.get("/risk/events")

async def risk_events(limit: int = 50):

    rows = await database.fetch_all(

        "SELECT * FROM risk_events ORDER BY created_at DESC LIMIT :limit",

        {"limit": limit}

    )

    result = []
    for row in rows:
        item = dict(row)
        item["detail"] = parse_json_field(item.get("detail"))
        result.append(item)
    return result


def parse_json_field(value):

    if isinstance(value, (dict, list)) or value is None:

        return value

    if isinstance(value, bytes):

        value = value.decode("utf-8", errors="replace")

    try:

        return json.loads(value)

    except Exception:

        return value


def count_active_consumers(consumers, idle_limit_ms: int = 30000) -> int:
    active = 0
    for consumer in consumers or []:
        idle = consumer.get("idle") if isinstance(consumer, dict) else None
        pending = consumer.get("pending", 0) if isinstance(consumer, dict) else 0
        try:
            idle_ms = int(idle)
        except Exception:
            idle_ms = idle_limit_ms + 1
        try:
            pending_count = int(pending)
        except Exception:
            pending_count = 0
        if idle_ms <= idle_limit_ms or pending_count > 0:
            active += 1
    return active


async def count_worker_heartbeats(stale_seconds: int = 45) -> int:
    try:
        row = await database.fetch_one(
            """SELECT COUNT(*) AS cnt
               FROM worker_heartbeats
               WHERE status = 'ok'
                 AND TIMESTAMPDIFF(SECOND, last_seen_at, NOW()) <= :stale_seconds""",
            {"stale_seconds": stale_seconds},
        )
        return int(row["cnt"] if row else 0)
    except Exception:
        return 0


def platform_selectors_complete(selector_config: dict, platform: str) -> bool:
    configured = selector_config.get(platform, {})
    if not isinstance(configured, dict):
        return False
    return all(bool(configured.get(phase)) for phase in ADAPTER_PHASES)


def build_next_actions(platforms, summary):

    actions = []

    if summary["notification_channels_configured"] == 0:

        actions.append({

            "code": "configure_notification",

            "priority": "P0",

            "target": "notifications",

            "title": "Configure at least one notification channel",

            "detail": "Set SERVERCHAN_KEY, FEISHU_WEBHOOK, GENERIC_WEBHOOK_URL, or TELEGRAM_BOT_TOKEN plus TELEGRAM_CHAT_ID, then send a notification test.",

        })

    if summary["recent_risk_events_24h"] > 0:

        actions.append({

            "code": "review_risk",

            "priority": "P1",

            "target": "risk-center",

            "title": "Review recent account risk signals",

            "detail": "Open Risk Center, inspect the latest risk events, and recalibrate any affected account before returning it to the ready pool.",

        })

    if summary.get("proxy_exits_total", 0) == 0:

        actions.append({

            "code": "add_proxy_exit",

            "priority": "P1",

            "target": "proxy-pool",

            "title": "Add isolated proxy exits for production accounts",

            "detail": "Open Safety, add one proxy exit per account, and keep risky exits in cooldown before enabling higher-volume multi-platform automation.",

        })

    for platform in platforms:

        if platform["safe_accounts"] == 0:

            actions.append({

                "code": "add_calibrated_account",

                "priority": "P0",

                "target": platform["platform"],

                "title": f"Add a calibrated safe account for {platform['label']}",

                "detail": "Use QR login or cookie import, then run account calibration so dry-run dispatch can safely auto-pick the account.",

            })

        if platform["action_adapter"] and not platform["real_actions_ready"]:

            actions.append({

                "code": "complete_adapter_probe",

                "priority": "P1",

                "target": platform["platform"],

                "title": f"Complete adapter probe for {platform['label']}",

                "detail": "Probe a low-risk real lottery page until follow, like, comment, and repost phases are visible; then review the recommended selector config.",

            })

        if not platform["action_adapter"]:

            actions.append({

                "code": "enable_real_adapter",

                "priority": "P1",

                "target": platform["platform"],

                "title": f"Enable real action adapter for {platform['label']}",

                "detail": "After a safe account is calibrated, configure DPMS_ADAPTER_SELECTORS_B64 with selectors verified by adapter probe evidence.",

            })

    if summary["dry_run_ready"] > 0 and summary["real_run_ready"] == 0:

        actions.append({

            "code": "keep_dry_run",

            "priority": "P2",

            "target": "workflow",

            "title": "Keep production dispatch in dry-run mode",

            "detail": "Dry-run validation can continue, but real execution should remain gated until at least one platform has a complete probe and real adapter readiness.",

        })

    return actions


def build_production_checks(platforms, summary):
    platform_count = summary.get("platforms_total", 0)
    dry_ready = summary.get("dry_run_ready", 0)
    real_ready = summary.get("real_run_ready", 0)
    safe_accounts = summary.get("safe_accounts_total", 0)
    checks = [
        {
            "code": "worker_online",
            "priority": "P0",
            "passed": summary.get("workers_online", 0) > 0,
            "title": "At least one worker is online",
            "detail": f"{summary.get('workers_online', 0)} worker(s) are visible by Redis consumer state or DB heartbeat.",
        },
        {
            "code": "real_run_global_switch",
            "priority": "P0",
            "passed": bool(summary.get("real_run_enabled")),
            "title": "Global real-run switch is enabled only after production approval",
            "detail": "REAL_RUN_ENABLED/runtime real_run_enabled is currently enabled." if summary.get("real_run_enabled") else "Global real-run is disabled by default.",
        },
        {
            "code": "notification_ready",
            "priority": "P0",
            "passed": summary.get("notification_channels_configured", 0) > 0,
            "title": "At least one notification channel is configured",
            "detail": f"{summary.get('notification_channels_configured', 0)} notification channel(s) can dispatch alerts.",
        },
        {
            "code": "target_pool_ready",
            "priority": "P0",
            "passed": summary.get("pending_targets", 0) > 0,
            "title": "At least one pending lottery target exists",
            "detail": f"{summary.get('pending_targets', 0)} pending or claimed target(s) are available for dispatch.",
        },
        {
            "code": "all_platforms_dry_ready",
            "priority": "P0",
            "passed": platform_count > 0 and dry_ready == platform_count,
            "title": "All platforms have calibrated accounts for dry-run dispatch",
            "detail": f"{dry_ready}/{platform_count} platform(s) are dry-run ready.",
        },
        {
            "code": "real_run_available",
            "priority": "P0",
            "passed": real_ready > 0,
            "title": "At least one platform is ready for real-run execution",
            "detail": f"{real_ready}/{platform_count} platform(s) are real-run ready.",
        },
        {
            "code": "active_proxy_exit",
            "priority": "P1",
            "passed": summary.get("active_proxy_exits", 0) > 0,
            "title": "At least one active proxy exit is available",
            "detail": f"{summary.get('active_proxy_exits', 0)} active proxy exit(s) are available.",
        },
        {
            "code": "safe_accounts_proxied",
            "priority": "P1",
            "passed": safe_accounts > 0 and summary.get("proxied_safe_accounts", 0) >= safe_accounts,
            "title": "Ready accounts are assigned active proxy exits",
            "detail": f"{summary.get('proxied_safe_accounts', 0)}/{safe_accounts} ready account(s) have active proxy exits.",
        },
        {
            "code": "recent_risk_clear",
            "priority": "P1",
            "passed": summary.get("recent_risk_events_24h", 0) == 0,
            "title": "No account risk events in the last 24 hours",
            "detail": f"{summary.get('recent_risk_events_24h', 0)} recent risk event(s) were recorded.",
        },
    ]
    return checks



@router.post("/worker/restart")

async def restart_worker(request: Request):
    require_min_role(request, "admin")
    require_confirmation(request)

    await redis.publish("worker:reload", "restart_requested")
    await audit_event(
        request,
        action="worker.restart",
        resource_type="worker",
        resource_id="all",
        result="signaled",
        risk_level="high",
    )

    return {"status": "restart_requested"}



@router.post("/worker/reload")

async def reload_worker(request: Request):
    require_min_role(request, "admin")
    require_confirmation(request)

    await redis.publish("worker:reload", "1")
    await audit_event(
        request,
        action="worker.reload",
        resource_type="worker",
        resource_id="all",
        result="signaled",
        risk_level="high",
    )

    return {"status": "reload_signaled"}


@router.get("/runtime/settings")
async def runtime_settings():
    return {
        "real_run_enabled": await is_real_run_enabled(),
    }


@router.put("/runtime/settings/real-run")
async def update_real_run_setting(payload: RealRunSettingUpdate, request: Request):
    require_min_role(request, "owner")
    require_confirmation(request)
    await set_runtime_setting("real_run_enabled", "true" if payload.enabled else "false")
    await audit_event(
        request,
        action="runtime.real_run.update",
        resource_type="runtime_setting",
        resource_id="real_run_enabled",
        result="enabled" if payload.enabled else "disabled",
        risk_level="critical" if payload.enabled else "high",
        detail={"enabled": payload.enabled},
    )
    return {"status": "updated", "real_run_enabled": payload.enabled}
