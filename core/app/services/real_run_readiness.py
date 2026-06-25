import json
from datetime import datetime, timedelta

from app.adapter_config import (
    STRUCTURED_SELECTOR_PLATFORMS,
    click_selectors,
    platform_has_api_real_adapter,
    platform_has_runtime_real_adapter,
    platform_real_adapter_kind,
    selector_config_complete,
    selector_values,
)
from app.db import database, redis
from app.platforms import get_platform
from app.services.lottery_rules import parse_lottery_rule
from app.utils.log import structured_log
from app.utils.lottery_targets import validate_lottery_target

ACCOUNT_RISK_COOLDOWN_HOURS = 24
MAX_ACCOUNT_RISK_COOLDOWN_HOURS = 24
ACCOUNT_RISK_COOLDOWN_BY_REASON = {
    # A local action burst should pause the account, not lock it out for a day.
    "action_window": 4,
    "sliding_window_exceeded": 4,
    # Harder risk signals keep the conservative 24 hour hold.
    "daily_limit": 24,
    "page_risk_signal": 24,
    "redirected_to_login": 24,
    "execution_timeout": 24,
    "bilibili_follow_captcha": 24,
    "bilibili_like_captcha": 24,
    "bilibili_comment_captcha": 24,
    "bilibili_repost_captcha": 24,
    "bilibili_follow_limit": 24,
    "bilibili_like_limit": 24,
    "bilibili_comment_limit": 24,
    "bilibili_repost_limit": 24,
    "bilibili_follow_risk": 24,
    "bilibili_like_risk": 24,
    "bilibili_comment_risk": 24,
    "bilibili_repost_risk": 24,
}


def parse_json_field(value):
    if isinstance(value, (dict, list)) or value is None:
        return value
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    try:
        return json.loads(value)
    except Exception:
        return value


def platform_selectors_complete(selector_config: dict, platform: str) -> bool:
    configured = selector_config.get(platform, {})
    return selector_config_complete(platform, configured)


def phase_configured(platform: str, config: dict, phase: str) -> bool:
    value = config.get(phase) if isinstance(config, dict) else None
    if platform not in STRUCTURED_SELECTOR_PLATFORMS:
        return bool(value)
    if phase == "commented":
        return isinstance(value, dict) and bool(
            selector_values(value.get("input") or value.get("inputs"))
            and selector_values(value.get("submit") or value.get("submits"))
        )
    return bool(click_selectors(value))


def action_plan_missing_rule_actions(lottery_data: dict, action_plan: dict | None) -> list[str]:
    if not isinstance(action_plan, dict):
        return []
    rule_text = str(lottery_data.get("rule_text") or "").strip()
    if not rule_text:
        return []
    platform = str(lottery_data.get("platform") or "bilibili")
    suggested = parse_lottery_rule(rule_text, platform)
    suggested_actions = suggested.get("required_actions") or []
    saved_actions = action_plan.get("required_actions") or []
    if not isinstance(suggested_actions, list) or not isinstance(saved_actions, list):
        return []
    saved = set(str(action) for action in saved_actions)
    return [str(action) for action in suggested_actions if str(action) not in saved]


def normalize_timestamp(value) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value).replace(" ", "T")


def normalize_datetime(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    if " " in text and "T" not in text:
        text = text.replace(" ", "T", 1)
    try:
        return datetime.fromisoformat(text).replace(tzinfo=None)
    except ValueError:
        return None


def row_value(row, key, default=None):
    if not row:
        return default
    try:
        return row[key]
    except Exception:
        if hasattr(row, "get"):
            return row.get(key, default)
    return default


def account_risk_reason(detail) -> str:
    parsed = parse_json_field(detail)
    if isinstance(parsed, dict):
        return str(parsed.get("reason") or "").strip()
    return ""


def account_risk_cooldown_hours(detail=None, event_type: str | None = None) -> int:
    reason = account_risk_reason(detail).lower()
    if reason in ACCOUNT_RISK_COOLDOWN_BY_REASON:
        return ACCOUNT_RISK_COOLDOWN_BY_REASON[reason]
    if str(event_type or "").lower() == "login_required":
        return 24
    return ACCOUNT_RISK_COOLDOWN_HOURS


def account_risk_cooldown_until(row) -> datetime | None:
    created_at = normalize_datetime(row_value(row, "created_at"))
    if not created_at:
        fallback = normalize_datetime(row_value(row, "cooldown_until"))
        return fallback
    hours = account_risk_cooldown_hours(row_value(row, "detail"), row_value(row, "event_type"))
    return created_at + timedelta(hours=hours)


def account_risk_is_active(row, now=None) -> bool:
    cooldown_until = account_risk_cooldown_until(row)
    if not cooldown_until:
        return True
    now_dt = normalize_datetime(now) or datetime.now()
    return cooldown_until > now_dt


async def current_db_time() -> datetime:
    row = await database.fetch_one("SELECT NOW() AS db_now")
    return normalize_datetime(row_value(row, "db_now")) or datetime.now()


def account_risk_payload(row) -> dict:
    if not row:
        return {"has_recent_risk": False, "cooldown_hours": ACCOUNT_RISK_COOLDOWN_HOURS}
    detail = parse_json_field(row_value(row, "detail"))
    event_type = row_value(row, "event_type")
    cooldown_hours = account_risk_cooldown_hours(detail, event_type)
    cooldown_until = account_risk_cooldown_until(row)
    return {
        "has_recent_risk": True,
        "cooldown_hours": cooldown_hours,
        "latest_event": {
            "id": row_value(row, "id"),
            "account_id": row_value(row, "account_id"),
            "event_type": event_type,
            "detail": detail if isinstance(detail, dict) else {},
            "created_at": normalize_timestamp(row_value(row, "created_at")),
        },
        "cooldown_until": normalize_timestamp(cooldown_until),
    }


async def recent_account_risk(account_id: int, *, now=None) -> dict:
    now_dt = normalize_datetime(now) or await current_db_time()
    rows = await database.fetch_all(
        f"""SELECT id, account_id, event_type, detail, created_at
           FROM risk_events
           WHERE account_id = :account_id
             AND created_at >= DATE_SUB(NOW(), INTERVAL {MAX_ACCOUNT_RISK_COOLDOWN_HOURS} HOUR)
           ORDER BY created_at DESC, id DESC
           LIMIT 50""",
        {"account_id": account_id},
    )
    for row in rows:
        if account_risk_is_active(row, now_dt):
            return account_risk_payload(row)
    return account_risk_payload(None)


async def real_run_account_risk_summary(platform: str) -> dict:
    ready_rows = await database.fetch_all(
        """SELECT a.id
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
    db_now = await current_db_time()
    runnable_count = 0
    latest_risk = None
    latest_risk_created_at = None
    for account in ready_rows:
        risk = await recent_account_risk(int(account["id"]), now=db_now)
        if not risk["has_recent_risk"]:
            runnable_count += 1
            continue
        created_at = normalize_datetime(risk["latest_event"].get("created_at"))
        if latest_risk is None or (created_at and (latest_risk_created_at is None or created_at > latest_risk_created_at)):
            latest_risk = risk
            latest_risk_created_at = created_at
    return {
        "ready_accounts": len(ready_rows),
        "runnable_accounts": runnable_count,
        "latest_recent_risk": latest_risk or account_risk_payload(None),
    }


async def validate_real_run_evidence(lottery, account_id: int | None = None) -> dict:
    blockers = []
    target = validate_lottery_target(lottery["platform"], lottery["raw_url"])
    if not target.valid:
        blockers.append("invalid_lottery_target")
    if platform_has_api_real_adapter(lottery["platform"]) and target.valid and target.kind != "dynamic":
        blockers.append("bilibili_dynamic_target_required")
    lottery_data = dict(lottery)
    action_plan = parse_json_field(lottery_data.get("action_plan"))
    required_actions = action_plan.get("required_actions", []) if isinstance(action_plan, dict) else []
    if not action_plan:
        blockers.append("lottery_action_plan_required")
    elif action_plan.get("review_required"):
        blockers.append("lottery_rule_review_required")
    elif not required_actions:
        blockers.append("lottery_required_actions_missing")
    elif action_plan_missing_rule_actions(lottery_data, action_plan):
        blockers.append("lottery_action_plan_stale")
    probe_values = {"platform": lottery["platform"], "lottery_id": lottery["id"]}
    task_values = {"lottery_id": lottery["id"]}
    account_filter = ""
    if account_id is not None:
        account_filter = "AND account_id = :account_id"
        probe_values["account_id"] = account_id
        task_values["account_id"] = account_id

    probe_summary = {"ready_for_real_actions": True, "adapter_kind": "api"} if platform_has_api_real_adapter(lottery["platform"]) else None
    if not platform_has_api_real_adapter(lottery["platform"]):
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

    account_risk = None
    if account_id is not None:
        account_risk = await recent_account_risk(account_id)
        if account_risk["has_recent_risk"]:
            blockers.append("recent_account_risk_event")

    return {
        "allowed": not blockers,
        "blockers": blockers,
        "probe_ready": bool(probe_summary and probe_summary.get("ready_for_real_actions")),
        "shadow_ready": bool(shadow),
        "action_plan_ready": bool(action_plan and required_actions and not action_plan.get("review_required")),
        "account_risk": account_risk,
    }


async def emit_real_run_gate_notification(lottery, reason, *, actor_id: str | None = None):
    platform = lottery["platform"]
    if platform not in STRUCTURED_SELECTOR_PLATFORMS:
        return
    platform_label = (get_platform(platform) or {}).get("label", platform)

    blockers = extract_real_run_blockers(reason)
    next_action = next_action_for_blockers(blockers)
    content_lines = [
        f"Platform: {platform}",
        f"Lottery: L{lottery['id']}",
        f"URL: {lottery['canonical_url'] or lottery['raw_url']}",
        f"Next action: {next_action}",
    ]
    if blockers:
        content_lines.append(f"Blockers: {', '.join(blockers)}")
    else:
        content_lines.append(f"Reason: {format_real_run_reason(reason)}")
    if actor_id:
        content_lines.append(f"Actor: {actor_id}")

    try:
        await redis.xadd(
            "notify_events",
            {
                "event_type": f"{platform}.real_run_gate.blocked",
                "severity": "warning",
                "title": f"{platform_label} real-run gate blocked: L{lottery['id']}",
                "content": "\n".join(content_lines),
                "channels": "all",
            },
        )
    except Exception as exc:
        structured_log(
            "error",
            "real_run_gate_notification_failed",
            lottery_id=lottery["id"],
            platform=lottery["platform"],
            exception=exc,
        )


def extract_real_run_blockers(reason) -> list[str]:
    if isinstance(reason, dict):
        blockers = reason.get("blockers")
        if isinstance(blockers, list):
            return [str(item) for item in blockers]
        nested = reason.get("reason")
        if nested is not None:
            return extract_real_run_blockers(nested)
    return []


def next_action_for_blockers(blockers: list[str]) -> str:
    if "invalid_lottery_target" in blockers:
        return "add_target"
    if "bilibili_dynamic_target_required" in blockers:
        return "add_target"
    if any(
        blocker in blockers
        for blocker in (
            "lottery_action_plan_required",
            "lottery_rule_review_required",
            "lottery_required_actions_missing",
            "lottery_action_plan_stale",
        )
    ):
        return "review_rule"
    if "no_calibrated_ready_account" in blockers:
        return "add_account"
    if "recent_account_risk_event" in blockers:
        return "review_risk"
    if "recent_complete_probe_required" in blockers:
        return "probe"
    if "real_adapter_not_enabled" in blockers:
        return "configure_adapter"
    if "recent_shadow_run_required" in blockers:
        return "shadow_run"
    if "global_real_run_disabled" in blockers:
        return "enable_real_run"
    return "review_gate"


def format_real_run_reason(reason) -> str:
    if isinstance(reason, dict):
        message = reason.get("message") or reason.get("detail")
        blockers = reason.get("blockers")
        if message and blockers:
            return f"{message}; blockers={', '.join(map(str, blockers))}"
        if message:
            return str(message)
        return json.dumps(reason, ensure_ascii=False)
    return str(reason)


async def real_run_gate_status(lottery, *, selector_config: dict, real_run_enabled: bool, account_id: int | None = None) -> dict:
    platform = lottery["platform"]
    cfg = get_platform(platform) or {}
    target = validate_lottery_target(platform, lottery["raw_url"])
    real_run_target_valid = target.valid and not (
        platform_has_api_real_adapter(platform) and target.kind != "dynamic"
    )
    account_summary = await real_run_account_risk_summary(platform)
    selector_ready = platform_selectors_complete(selector_config, platform)
    adapter_kind = platform_real_adapter_kind(selector_config, platform)
    adapter_enabled = bool(cfg.get("action_adapter")) or platform_has_runtime_real_adapter(selector_config, platform)
    evidence = await validate_real_run_evidence(lottery, account_id=account_id)
    blockers = list(evidence["blockers"])
    if not account_summary["ready_accounts"]:
        blockers.insert(0, "no_calibrated_ready_account")
    elif account_id is None and not account_summary["runnable_accounts"]:
        blockers.insert(0, "recent_account_risk_event")
    if not adapter_enabled:
        blockers.insert(0, "real_adapter_not_enabled")
    if not real_run_enabled:
        blockers.insert(0, "global_real_run_disabled")

    next_action = next_action_for_blockers(blockers) if blockers else "real_run"
    if next_action == "review_gate" and not selector_ready:
        next_action = "configure_adapter"

    return {
        "lottery_id": lottery["id"],
        "platform": platform,
        "status": lottery["status"],
        "raw_url": lottery["raw_url"],
        "target_valid": real_run_target_valid,
        "target_kind": target.kind,
        "target_error": None
        if real_run_target_valid
        else (target.reason or "bilibili_dynamic_target_required"),
        "allowed": not blockers,
        "blockers": blockers,
        "next_action": next_action,
        "real_run_enabled": real_run_enabled,
        "adapter_enabled": adapter_enabled,
        "adapter_kind": adapter_kind,
        "selector_ready": selector_ready,
        "api_adapter_ready": adapter_kind == "api",
        "safe_accounts": account_summary["ready_accounts"],
        "risk_clear_accounts": account_summary["runnable_accounts"],
        "account_risk": evidence["account_risk"] or account_summary["latest_recent_risk"],
        "probe_ready": evidence["probe_ready"],
        "shadow_ready": evidence["shadow_ready"],
        "action_plan_ready": evidence["action_plan_ready"],
        "action_plan": parse_json_field(dict(lottery).get("action_plan")),
    }
