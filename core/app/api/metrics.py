
import time, psutil, json, asyncio

from fastapi import APIRouter, HTTPException, Query, Request

from fastapi.responses import StreamingResponse

from app.config import settings
from app.adapter_config import (
    load_runtime_selector_config,
    platform_has_runtime_real_adapter,
    platform_probe_ready_for_real_actions,
    platform_real_adapter_kind,
    selector_config_complete,
)
from app.db import database, redis
from app.event_store.service import record_event
from app.api.notify import configured_channels
from app.models.schemas import RealRunSettingUpdate, RuntimeRollbackRequest
from app.platforms import get_platforms
from app.action_plan import action_order_for_platform
from app.services.real_run_readiness import (
    validate_weibo_oauth_capability_attestation,
)
from app.security import audit_event, is_real_run_enabled, require_confirmation, require_min_role, set_runtime_setting

from app.utils.log import structured_log



router = APIRouter()


EXTERNAL_ACTION_INTENT_STATUSES = frozenset(
    {"pending", "prepared", "started", "succeeded", "failed", "unknown"}
)
def _intent_observation(row):
    item = dict(row)
    # Remote references are written from third-party responses.  Even a
    # superficially harmless token may embed a session identifier, so the
    # general metrics API exposes presence only.  A future platform-specific
    # reconciliation flow can define a strict typed remote-reference schema.
    item.pop("remote_ref", None)
    item["remote_ref_redacted"] = bool(item.pop("has_remote_ref", 0))
    item["reconciliation_required"] = bool(item.get("reconciliation_required"))
    item["has_error"] = bool(item.get("has_error"))
    return item


async def _real_run_inflight_counts():
    row = await database.fetch_one(
        """SELECT COALESCE(SUM(status = 'queued'), 0) AS queued,
                  COALESCE(SUM(status = 'running'), 0) AS running
           FROM task_runs
           WHERE task_mode = 'real_run'"""
    )
    return {
        "queued": int(row["queued"] or 0) if row else 0,
        "running": int(row["running"] or 0) if row else 0,
    }


def _worker_gate_contract():
    return {
        "authoritative_source": (
            "process_env.REAL_RUN_ENABLED AND runtime_settings.real_run_enabled"
        ),
        "process_capability_required": True,
        "recheck_points": ["before_task_claim", "before_each_external_mutation"],
        "setting_change_cancels_tasks": False,
    }


def weibo_oauth_capability_summary(rows) -> dict:
    """Summarize generic platform readiness without overstating partial grants."""

    actions = action_order_for_platform("weibo")
    action_accounts = {action: 0 for action in actions}
    any_action_accounts = 0
    full_action_accounts = 0
    for row in rows:
        ready_actions = []
        for action in actions:
            attestation = validate_weibo_oauth_capability_attestation(
                row["result"],
                required_actions=(action,),
                account_id=int(row["id"]),
                execution_revision=int(row["execution_revision"] or 0),
                calibration_fresh=bool(row["calibration_fresh"]),
            )
            if attestation["ready"]:
                ready_actions.append(action)
                action_accounts[action] += 1
        any_action_accounts += int(bool(ready_actions))
        full_attestation = validate_weibo_oauth_capability_attestation(
            row["result"],
            required_actions=actions,
            account_id=int(row["id"]),
            execution_revision=int(row["execution_revision"] or 0),
            calibration_fresh=bool(row["calibration_fresh"]),
        )
        full_action_accounts += int(full_attestation["ready"])
    return {
        "full_action_accounts": full_action_accounts,
        "any_action_accounts": any_action_accounts,
        "action_accounts": action_accounts,
    }



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

        dry_run_supported = cfg.get("execution_mode") != "manual_assisted"

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
        adapter_kind = platform_real_adapter_kind(selector_config, platform)
        action_adapter_enabled = bool(cfg.get("action_adapter")) or platform_has_runtime_real_adapter(selector_config, platform)
        oauth_capability_accounts = 0
        oauth_any_capability_accounts = 0
        oauth_capability_actions = (
            {action: 0 for action in action_order_for_platform("weibo")}
            if platform == "weibo"
            else {}
        )
        if platform == "weibo":
            oauth_rows = await database.fetch_all(
                """SELECT a.id, a.execution_revision, c.result,
                          (c.created_at >= DATE_SUB(NOW(), INTERVAL 24 HOUR)) AS calibration_fresh
                     FROM accounts a
                     JOIN account_calibrations c
                       ON c.id = (
                         SELECT latest.id
                         FROM account_calibrations latest
                         WHERE latest.account_id = a.id
                           AND latest.platform = 'weibo'
                         ORDER BY latest.id DESC
                         LIMIT 1
                       )
                    WHERE a.platform = 'weibo'
                      AND a.status = 'ready'
                      AND a.deleted_at IS NULL
                      AND OCTET_LENGTH(a.encrypted_credential) > 0
                      AND c.status = 'succeeded'"""
            )
            capability_summary = weibo_oauth_capability_summary(oauth_rows)
            oauth_capability_accounts = capability_summary[
                "full_action_accounts"
            ]
            oauth_any_capability_accounts = capability_summary[
                "any_action_accounts"
            ]
            oauth_capability_actions = capability_summary["action_accounts"]
        probe_ready = (
            oauth_capability_accounts > 0
            if adapter_kind == "oauth"
            else platform_probe_ready_for_real_actions(platform, probe_summary)
        )
        real_actions_ready = action_adapter_enabled and probe_ready

        blockers = []
        blocker_codes = []

        if not safe_accounts["cnt"]:

            blockers.append("no calibrated ready account")
            blocker_codes.append("no_calibrated_ready_account")

        if not action_adapter_enabled:

            blockers.append("real adapter not enabled")
            blocker_codes.append("real_adapter_not_enabled")

        if adapter_kind == "oauth" and not oauth_capability_accounts:

            blockers.append("official OAuth capability evidence is missing")
            blocker_codes.append("weibo_oauth_capability_evidence_required")

        elif action_adapter_enabled and not real_actions_ready:

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

                "adapter_status": (
                    cfg.get("adapter_status", "planned")
                    if adapter_kind in {"oauth", "manual_assisted"}
                    else (
                        "configured"
                        if action_adapter_enabled
                        else cfg.get("adapter_status", "planned")
                    )
                ),
                "adapter_kind": adapter_kind,

                "action_adapter": action_adapter_enabled,
                "selector_observation_configured": runtime_adapter_ready,
                "oauth_capability_accounts": oauth_capability_accounts,
                "oauth_any_capability_accounts": oauth_any_capability_accounts,
                "oauth_capability_actions": oauth_capability_actions,

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

                "dry_run_supported": dry_run_supported,
                "ready_for_dry_run": dry_run_supported and bool(safe_accounts["cnt"]),
                "ready_for_shadow_run": bool(safe_accounts["cnt"]),

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

        "dry_run_supported": sum(1 for item in platforms if item["dry_run_supported"]),

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
    strategy_advice = await build_strategy_advice(summary)

    return {

        "platforms": platforms,

        "summary": summary,

        "actions": build_next_actions(platforms, summary),
        "production_checks": production_checks,
        "strategy_advice": strategy_advice,

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
    return selector_config_complete(platform, configured)


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

        if platform.get("adapter_kind") == "oauth" and not platform["real_actions_ready"]:

            actions.append({

                "code": "configure_weibo_oauth",

                "priority": "P0",

                "target": platform["platform"],

                "title": f"Authorize official OAuth actions for {platform['label']}",

                "detail": "Configure an approved OAuth application and refresh account-bound capability evidence. Advanced like/follow permissions remain denied until explicitly granted.",

            })

        if platform["action_adapter"] and platform.get("adapter_kind") == "selector" and not platform["real_actions_ready"]:

            actions.append({

                "code": "complete_adapter_probe",

                "priority": "P1",

                "target": platform["platform"],

                "title": f"Complete adapter probe for {platform['label']}",

                "detail": "Probe a low-risk real lottery page until follow, like, comment, and repost phases are visible; then review the recommended selector config.",

            })

        if not platform["action_adapter"] and platform.get("adapter_kind") != "manual_assisted":

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


async def build_strategy_advice(summary):
    window_days = 7
    mode_rows = await database.fetch_all(
        """SELECT COALESCE(task_mode, IF(dry_run = 1, 'dry_run', 'real_run')) AS mode,
                  status,
                  COUNT(*) AS cnt
           FROM task_runs
           WHERE created_at >= DATE_SUB(NOW(), INTERVAL 7 DAY)
           GROUP BY mode, status"""
    )
    risk_rows = await database.fetch_all(
        """SELECT account_id, COUNT(*) AS cnt
           FROM risk_events
           WHERE created_at >= DATE_SUB(NOW(), INTERVAL 7 DAY)
           GROUP BY account_id
           ORDER BY cnt DESC
           LIMIT 3"""
    )
    high_value_pending = await database.fetch_one(
        "SELECT COUNT(*) AS cnt FROM lotteries WHERE status = 'pending' AND value_score >= 70"
    )
    stale_active_tasks = await database.fetch_one(
        """SELECT COUNT(*) AS cnt
           FROM task_runs
           WHERE status IN ('queued','running')
             AND created_at < DATE_SUB(NOW(), INTERVAL 30 MINUTE)"""
    )

    mode_counts = [dict(row) for row in mode_rows]
    mode_success = {
        row["mode"]: int(row["cnt"])
        for row in mode_counts
        if row["status"] == "succeeded"
    }
    mode_failed = {
        row["mode"]: int(row["cnt"])
        for row in mode_counts
        if row["status"] == "failed"
    }
    shadow_success = mode_success.get("shadow_run", 0)
    dry_success = mode_success.get("dry_run", 0)
    real_success = mode_success.get("real_run", 0)
    failed_total = sum(mode_failed.values())
    advice = []

    if summary.get("notification_channels_configured", 0) == 0:
        advice.append(strategy_item(
            "configure_notifications",
            "P0",
            "notify",
            "Configure notification before autonomous runs",
            "No external notification channel is configured, so wins, failures, captcha, or bans may only be visible in logs.",
            {"configured_channels": 0},
        ))

    if summary.get("pending_targets", 0) > 0 and shadow_success == 0:
        advice.append(strategy_item(
            "run_shadow_before_real",
            "P0",
            "workflow",
            "Run shadow-run before real-run",
            "Pending targets exist, but no successful shadow-run was recorded in the last 7 days.",
            {"pending_targets": summary.get("pending_targets", 0), "shadow_success_7d": shadow_success},
        ))

    if dry_success > 0 and shadow_success == 0:
        advice.append(strategy_item(
            "promote_dry_to_shadow",
            "P1",
            "workflow",
            "Promote dry-run validation into shadow-run",
            "Dry-run is succeeding, so the next safe step is opening real pages with no side effects to validate login state and risk signals.",
            {"dry_success_7d": dry_success},
        ))

    if shadow_success > 0 and summary.get("real_run_ready", 0) == 0:
        advice.append(strategy_item(
            "complete_real_gate",
            "P1",
            "strategy",
            "Complete the real-run gate after shadow evidence",
            "Shadow-run evidence exists, but no platform currently satisfies real-run readiness.",
            {"shadow_success_7d": shadow_success, "real_run_ready": summary.get("real_run_ready", 0)},
        ))

    if failed_total > 0:
        advice.append(strategy_item(
            "review_failed_runs",
            "P1",
            "review",
            "Review failed runs before increasing volume",
            "Recent task failures should be inspected through evidence screenshots and event timeline before scaling the scheduler.",
            {"failed_runs_7d": failed_total},
        ))

    if summary.get("recent_risk_events_24h", 0) > 0 or risk_rows:
        advice.append(strategy_item(
            "cooldown_risky_accounts",
            "P1",
            "risk",
            "Cooldown and recalibrate risky accounts",
            "Risk events were recorded recently; affected accounts should be cooled, recalibrated, or assigned safer proxy exits.",
            {
                "risk_24h": summary.get("recent_risk_events_24h", 0),
                "hot_accounts": [dict(row) for row in risk_rows],
            },
        ))

    if int(high_value_pending["cnt"] if high_value_pending else 0) > 0 and summary.get("safe_accounts_total", 0) > 0:
        advice.append(strategy_item(
            "prioritize_high_value_targets",
            "P2",
            "strategy",
            "Prioritize high-value pending targets",
            "High-score pending targets exist and safe accounts are available; dispatch order should favor expected value after shadow-run clears.",
            {
                "high_value_pending": int(high_value_pending["cnt"]),
                "safe_accounts": summary.get("safe_accounts_total", 0),
            },
        ))

    if int(stale_active_tasks["cnt"] if stale_active_tasks else 0) > 0:
        advice.append(strategy_item(
            "recover_stale_tasks",
            "P1",
            "worker",
            "Recover stale active tasks",
            "Queued or running tasks older than 30 minutes were found; inspect Worker health and recovery daemon behavior.",
            {"stale_active_tasks": int(stale_active_tasks["cnt"])},
        ))

    if not advice and real_success > 0:
        advice.append(strategy_item(
            "continue_controlled_real_run",
            "P2",
            "strategy",
            "Continue controlled real-run cadence",
            "Recent real-run tasks succeeded and no urgent blocker was detected by the strategy review.",
            {"real_success_7d": real_success},
        ))

    return {
        "review_window_days": window_days,
        "mode_counts": mode_counts,
        "high_value_pending": int(high_value_pending["cnt"] if high_value_pending else 0),
        "stale_active_tasks": int(stale_active_tasks["cnt"] if stale_active_tasks else 0),
        "risk_hot_accounts": [dict(row) for row in risk_rows],
        "advice": advice[:8],
    }


def strategy_item(code, priority, target, title, detail, evidence):
    return {
        "code": code,
        "priority": priority,
        "target": target,
        "title": title,
        "detail": detail,
        "evidence": evidence,
    }


def build_production_checks(platforms, summary):
    platform_count = summary.get("platforms_total", 0)
    dry_ready = summary.get("dry_run_ready", 0)
    dry_supported = summary.get("dry_run_supported", platform_count)
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
            "passed": dry_supported > 0 and dry_ready == dry_supported,
            "title": "All dry-run-capable platforms have calibrated accounts",
            "detail": f"{dry_ready}/{dry_supported} dry-run-capable platform(s) are ready.",
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


@router.get("/external-action-intents")
async def external_action_intents(
    limit: int = Query(default=50, ge=1, le=200),
    status: str | None = Query(default=None),
    task_id: str | None = Query(default=None, min_length=1, max_length=64),
):
    """Read-only, payload-free view of durable external action attempts."""

    normalized_status = str(status or "").strip().lower()
    if normalized_status and normalized_status not in EXTERNAL_ACTION_INTENT_STATUSES:
        raise HTTPException(status_code=400, detail="Invalid external action intent status")
    normalized_task_id = str(task_id or "").strip()
    clauses = []
    values = {"limit": limit}
    if normalized_status:
        clauses.append("eai.status = :status")
        values["status"] = normalized_status
    if normalized_task_id:
        clauses.append("eai.task_id = :task_id")
        values["task_id"] = normalized_task_id
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = await database.fetch_all(
        f"""SELECT eai.intent_id, eai.task_id, eai.account_id, eai.lottery_id,
                   eai.lease_id, eai.lease_generation, eai.action,
                   eai.payload_hash, eai.status, eai.effect_certainty,
                   eai.attempt_no,
                   eai.started_at, eai.completed_at, eai.outcome,
                   CASE WHEN eai.remote_ref IS NULL THEN 0 ELSE 1 END AS has_remote_ref,
                   CASE WHEN eai.error_message IS NULL THEN 0 ELSE 1 END AS has_error,
                   eai.created_at, eai.updated_at,
                   tr.status AS task_status, tr.task_mode,
                   tr.reconciliation_required,
                   l.platform
            FROM external_action_intents eai
            JOIN task_runs tr ON tr.task_id = eai.task_id
            JOIN lotteries l ON l.id = eai.lottery_id
            {where}
            ORDER BY eai.updated_at DESC, eai.intent_id DESC
            LIMIT :limit""",
        values,
    )
    return {
        "items": [_intent_observation(row) for row in rows],
        "count": len(rows),
        "payload_exposed": False,
    }


@router.get("/reconciliation")
async def reconciliation_queue(
    limit: int = Query(default=50, ge=1, le=200),
):
    """Tasks quarantined by structured reconciliation state, newest first."""

    rows = await database.fetch_all(
        """SELECT tr.task_id, tr.account_id, tr.lottery_id, tr.task_mode,
                  tr.status AS task_status, tr.reconciliation_required,
                  tr.created_at, tr.started_at, tr.finished_at,
                  l.platform,
                  COUNT(eai.intent_id) AS intent_count,
                  COALESCE(SUM(eai.status = 'started'), 0) AS started_intent_count,
                  COALESCE(SUM(eai.status = 'unknown'), 0) AS unknown_intent_count,
                  COALESCE(SUM(eai.effect_certainty = 'unknown'), 0) AS unknown_effect_count,
                  COALESCE(SUM(eai.status = 'succeeded'), 0) AS succeeded_intent_count,
                  MAX(eai.updated_at) AS latest_intent_at
           FROM task_runs tr
           JOIN lotteries l ON l.id = tr.lottery_id
           LEFT JOIN external_action_intents eai ON eai.task_id = tr.task_id
           WHERE tr.reconciliation_required = 1
              OR EXISTS (
                   SELECT 1 FROM external_action_intents unsettled
                   WHERE unsettled.task_id = tr.task_id
                     AND unsettled.status IN ('started', 'unknown')
              )
           GROUP BY tr.task_id, tr.account_id, tr.lottery_id, tr.task_mode,
                    tr.status, tr.reconciliation_required, tr.created_at,
                    tr.started_at, tr.finished_at, l.platform
           ORDER BY COALESCE(MAX(eai.updated_at), tr.finished_at, tr.started_at, tr.created_at) DESC
           LIMIT :limit""",
        {"limit": limit},
    )
    items = []
    for row in rows:
        item = dict(row)
        item["reconciliation_required"] = bool(item.get("reconciliation_required"))
        for field in (
            "intent_count",
            "started_intent_count",
            "unknown_intent_count",
            "unknown_effect_count",
            "succeeded_intent_count",
        ):
            item[field] = int(item.get(field) or 0)
        items.append(item)
    return {"items": items, "count": len(items), "payload_exposed": False}



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
    breaker = await database.fetch_one(
        "SELECT status, reason, opened_at, updated_at FROM circuit_breakers WHERE scope = 'global'"
    )
    setting = await database.fetch_one(
        """SELECT updated_at FROM runtime_settings
           WHERE setting_key = 'real_run_enabled'"""
    )
    return {
        "real_run_enabled": await is_real_run_enabled(),
        "real_run_setting_updated_at": setting["updated_at"] if setting else None,
        "inflight_real_runs": await _real_run_inflight_counts(),
        "worker_gate_contract": _worker_gate_contract(),
        "global_circuit_breaker": dict(breaker) if breaker else None,
    }


@router.put("/runtime/settings/real-run")
async def update_real_run_setting(payload: RealRunSettingUpdate, request: Request):
    actor = require_min_role(request, "owner")
    require_confirmation(request)
    if payload.enabled and not settings.real_run_enabled:
        raise HTTPException(
            status_code=409,
            detail=(
                "Process REAL_RUN_ENABLED capability is disabled; restart the "
                "local services with an explicit deployment-level enablement "
                "before changing the runtime switch"
            ),
        )
    await set_runtime_setting("real_run_enabled", "true" if payload.enabled else "false")
    inflight = await _real_run_inflight_counts()
    contract = _worker_gate_contract()
    result = {
        "status": "updated",
        "real_run_enabled": payload.enabled,
        "inflight_real_runs": inflight,
        "worker_gate_contract": contract,
        "enforcement": (
            "enabled_for_fresh_worker_gate_checks"
            if payload.enabled
            else "disabled_at_next_worker_gate_check"
        ),
    }
    await audit_event(
        request,
        action="runtime.real_run.update",
        resource_type="runtime_setting",
        resource_id="real_run_enabled",
        result="enabled" if payload.enabled else "disabled",
        risk_level="critical" if payload.enabled else "high",
        detail={
            "enabled": payload.enabled,
            "global_circuit_breaker": "unchanged",
            "inflight_real_runs": inflight,
            "worker_gate_contract": contract,
        },
    )
    await record_event(
        aggregate="runtime",
        aggregate_id="real_run_enabled",
        event_type="RealRunRuntimeSettingChanged",
        payload=result,
        actor_type="operator",
        actor_id=actor["actor_id"],
    )
    return result


@router.post("/runtime/rollback")
async def runtime_rollback(payload: RuntimeRollbackRequest, request: Request):
    actor = require_min_role(request, "owner")
    require_confirmation(request)
    reason = (payload.reason or "manual runtime rollback").strip()[:255]

    queued_rows = await database.fetch_all(
        """SELECT task_id, lottery_id
           FROM task_runs
           WHERE task_mode = 'real_run'
             AND status = 'queued'"""
    )
    running_count = await database.fetch_one(
        """SELECT COUNT(*) AS cnt
           FROM task_runs
           WHERE task_mode = 'real_run'
             AND status = 'running'"""
    )

    await set_runtime_setting("real_run_enabled", "false")
    await database.execute(
        """INSERT INTO circuit_breakers (scope, status, reason, opened_at)
           VALUES ('global', 'open', :reason, NOW())
           ON DUPLICATE KEY UPDATE
             status = 'open',
             reason = :reason,
             opened_at = NOW(),
             updated_at = NOW()""",
        {"reason": reason},
    )
    breaker = await database.fetch_one(
        "SELECT status FROM circuit_breakers WHERE scope = 'global'"
    )
    if not breaker or str(breaker["status"] or "").strip().lower() != "open":
        raise HTTPException(500, detail="Global circuit breaker write was not persisted")
    await database.execute(
        """UPDATE lotteries l
           JOIN task_runs tr ON tr.lottery_id = l.id
           SET l.status = 'pending',
               l.execution_lock = NULL,
               l.locked_at = NULL
           WHERE tr.task_mode = 'real_run'
             AND tr.status = 'queued'"""
    )
    await database.execute(
        """UPDATE task_runs
           SET status = 'failed',
               error_message = :error_message,
               finished_at = NOW()
           WHERE task_mode = 'real_run'
             AND status = 'queued'""",
        {"error_message": f"Runtime rollback: {reason}"},
    )
    await redis.publish("worker:reload", "runtime_rollback")
    await redis.xadd(
        "notify_events",
        {
            "title": "DPMS runtime rollback applied",
            "content": f"Real-run disabled and global circuit breaker opened. Reason: {reason}",
            "status": "rollback",
            "channels": "all",
        },
    )

    queued_task_ids = [row["task_id"] for row in queued_rows]
    result = {
        "status": "rollback_applied",
        "real_run_enabled": False,
        "circuit_breaker": "global=open",
        "queued_real_runs_cancelled": len(queued_task_ids),
        "running_real_runs_observed": int(running_count["cnt"] if running_count else 0),
        "queued_task_ids": queued_task_ids,
        "reason": reason,
    }
    await audit_event(
        request,
        action="runtime.rollback",
        resource_type="runtime",
        resource_id="global",
        result="applied",
        risk_level="critical",
        detail=result,
    )
    await record_event(
        aggregate="runtime",
        aggregate_id="global",
        event_type="RuntimeRollbackApplied",
        payload=result,
        actor_type="operator",
        actor_id=actor["actor_id"],
    )
    return result
