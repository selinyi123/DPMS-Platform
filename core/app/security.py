import hmac
import json
from typing import Any

from fastapi import HTTPException, Request

from app.config import settings
from app.db import database


ROLE_LEVELS = {
    "viewer": 0,
    "operator": 1,
    "admin": 2,
    "owner": 3,
}

SENSITIVE_KEY_PARTS = (
    "cookie",
    "credential",
    "password",
    "secret",
    "token",
    "signature",
    "webhook",
    "key",
)


def parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "enabled"}


def actor_from_request(request: Request) -> dict[str, str] | None:
    provided = request.headers.get("x-admin-token", "")
    auth_type = "x-admin-token"
    if not provided and request.method in {"GET", "HEAD"}:
        # Browser-native EventSource, <img>, and normal links cannot attach
        # custom headers. Restrict query-token auth to read-only requests.
        provided = request.query_params.get("admin_token", "")
        auth_type = "admin_token_query"
    if settings.admin_token and provided and hmac.compare_digest(provided, settings.admin_token):
        return {
            "actor_id": "admin-token",
            "actor_name": "Admin token",
            "role": "owner",
            "auth_type": auth_type,
        }
    return None


async def authenticate_request(request: Request) -> dict[str, str]:
    actor = actor_from_request(request)
    if not actor:
        raise HTTPException(status_code=401, detail="Admin token required")
    request.state.actor = actor
    return actor


def current_actor(request: Request) -> dict[str, str]:
    actor = getattr(request.state, "actor", None) or actor_from_request(request)
    if not actor:
        raise HTTPException(status_code=401, detail="Admin token required")
    request.state.actor = actor
    return actor


def require_min_role(request: Request, min_role: str) -> dict[str, str]:
    actor = current_actor(request)
    actor_level = ROLE_LEVELS.get(actor.get("role", "viewer"), -1)
    required_level = ROLE_LEVELS[min_role]
    if actor_level < required_level:
        raise HTTPException(status_code=403, detail=f"{min_role} role required")
    return actor


def require_confirmation(request: Request) -> None:
    confirmed = request.headers.get("x-confirm-action", "")
    if not parse_bool(confirmed):
        raise HTTPException(status_code=409, detail="Dangerous action confirmation required")


async def get_runtime_setting(key: str, default: str = "") -> str:
    row = await database.fetch_one(
        "SELECT setting_value FROM runtime_settings WHERE setting_key = :key",
        {"key": key},
    )
    if not row:
        return default
    return str(row["setting_value"])


async def set_runtime_setting(key: str, value: str) -> None:
    await database.execute(
        """INSERT INTO runtime_settings (setting_key, setting_value)
           VALUES (:key, :value)
           ON DUPLICATE KEY UPDATE setting_value = :value, updated_at = NOW()""",
        {"key": key, "value": value},
    )
    persisted = await database.fetch_one(
        "SELECT setting_value FROM runtime_settings WHERE setting_key = :key",
        {"key": key},
    )
    if not persisted or str(persisted["setting_value"]) != str(value):
        raise RuntimeError(f"Runtime setting write was not persisted: {key}")


async def is_real_run_enabled() -> bool:
    # Two independent keys are required.  The deployment-level environment
    # flag is the hard ceiling; the mutable database flag can only disable a
    # process that was explicitly started with real-run capability.  Missing
    # runtime state is fail-closed instead of inheriting a permissive default.
    if not settings.real_run_enabled:
        return False
    value = await get_runtime_setting("real_run_enabled", "false")
    return parse_bool(value)


async def circuit_breaker_allows(platform: str) -> tuple[bool, str | None]:
    rows = await database.fetch_all(
        """SELECT scope, status, reason
           FROM circuit_breakers
           WHERE scope IN (:global_scope, :platform_scope)
             AND status <> 'closed'""",
        {"global_scope": "global", "platform_scope": f"platform:{platform}"},
    )
    if not rows:
        return True, None
    reasons = []
    for row in rows:
        reason = row["reason"] or row["status"]
        reasons.append(f"{row['scope']}={reason}")
    return False, "; ".join(reasons)


async def audit_event(
    request: Request,
    *,
    action: str,
    resource_type: str,
    resource_id: Any | None = None,
    result: str,
    risk_level: str = "low",
    detail: dict[str, Any] | None = None,
) -> None:
    actor = current_actor(request)
    client_host = client_ip(request)
    await database.execute(
        """INSERT INTO audit_logs
           (actor_id, actor_role, action, resource_type, resource_id, result, risk_level, detail, ip_address, user_agent)
           VALUES (:actor_id, :actor_role, :action, :resource_type, :resource_id, :result, :risk_level, :detail, :ip_address, :user_agent)""",
        {
            "actor_id": actor["actor_id"],
            "actor_role": actor["role"],
            "action": action,
            "resource_type": resource_type,
            "resource_id": str(resource_id) if resource_id is not None else None,
            "result": result,
            "risk_level": risk_level,
            "detail": json.dumps(redact_detail(detail or {}), ensure_ascii=False),
            "ip_address": client_host,
            "user_agent": (request.headers.get("user-agent") or "")[:255],
        },
    )


def client_ip(request: Request) -> str | None:
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        first = forwarded_for.split(",", 1)[0].strip()
        if first:
            return first[:64]
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip[:64]
    return request.client.host if request.client else None


def redact_detail(value: Any) -> Any:
    if isinstance(value, dict):
        output = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(part in lowered for part in SENSITIVE_KEY_PARTS):
                output[key] = "<redacted>"
            else:
                output[key] = redact_detail(item)
        return output
    if isinstance(value, list):
        return [redact_detail(item) for item in value]
    return value
