from datetime import datetime, timedelta
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastapi import APIRouter, HTTPException, Request

from app.db import database
from app.event_store.service import record_event
from app.models.schemas import ProxyCheckRequest, ProxyCooldownRequest, ProxyCreate, ProxyStatusUpdate
from app.platforms import PLATFORMS
from app.security import audit_event, require_confirmation, require_min_role


router = APIRouter()

VALID_PROXY_STATUSES = {"active", "degraded", "dead"}


def _platform_check_domains() -> set[str]:
    domains = set()
    for cfg in PLATFORMS.values():
        domain = (cfg.get("cookie_domain") or "").lstrip(".").lower()
        if domain:
            domains.add(domain)
    return domains


# Proxy health checks may probe these platform domains without extra
# confirmation (P1-5). Any other target is treated as an operator-directed
# probe of an arbitrary host/port via the proxy (SSRF / internal-network
# scanning risk) and requires admin role + explicit confirmation.
ALLOWED_CHECK_DOMAINS = _platform_check_domains()


def _is_allowed_check_host(hostname: str | None) -> bool:
    host = (hostname or "").lower()
    return any(host == domain or host.endswith(f".{domain}") for domain in ALLOWED_CHECK_DOMAINS)


@router.get("/")
async def list_proxies():
    rows = await database.fetch_all(
        """SELECT p.*,
                  a.id AS account_id,
                  a.platform AS account_platform,
                  a.status AS account_status,
                  (
                    SELECT COUNT(*)
                    FROM risk_events r
                    WHERE r.account_id = a.id
                      AND TIMESTAMPDIFF(HOUR, r.created_at, NOW()) < 24
                  ) AS account_risk_24h
           FROM proxies p
           LEFT JOIN accounts a ON a.proxy_id = p.id
           ORDER BY p.id DESC"""
    )
    return [serialize_proxy(row) for row in rows]


@router.get("/summary")
async def proxy_summary():
    status_rows = await database.fetch_all("SELECT status, COUNT(*) AS cnt FROM proxies GROUP BY status")
    status_counts = {row["status"]: row["cnt"] for row in status_rows}
    cooling = await database.fetch_one(
        """SELECT COUNT(*) AS cnt FROM proxies
           WHERE cooldown_until IS NOT NULL AND cooldown_until > NOW()"""
    )
    assigned = await database.fetch_one("SELECT COUNT(*) AS cnt FROM accounts WHERE proxy_id IS NOT NULL")
    total = sum(status_counts.values())
    return {
        "total": total,
        "active": status_counts.get("active", 0),
        "degraded": status_counts.get("degraded", 0),
        "dead": status_counts.get("dead", 0),
        "cooling": cooling["cnt"],
        "assigned_accounts": assigned["cnt"],
        "unassigned": max(0, total - assigned["cnt"]),
    }


@router.post("/")
async def create_proxy(data: ProxyCreate, request: Request):
    actor = require_min_role(request, "operator")
    validate_proxy_url(data.proxy_url)
    proxy_type = (data.proxy_type or "socks5").lower()
    if proxy_type not in {"socks5", "http", "https"}:
        raise HTTPException(400, detail="proxy_type must be socks5, http, or https")
    proxy_id = await database.execute(
        """INSERT INTO proxies (proxy_url, proxy_type, provider, country, status, health_score)
           VALUES (:url, :proxy_type, :provider, :country, 'active', 100)""",
        {
            "url": data.proxy_url,
            "proxy_type": proxy_type,
            "provider": data.provider,
            "country": data.country,
        },
    )
    await audit_event(
        request,
        action="proxy.create",
        resource_type="proxy",
        resource_id=proxy_id,
        result="created",
        risk_level="medium",
        detail={
            "proxy_type": proxy_type,
            "provider": data.provider,
            "country": data.country,
            "proxy_url": mask_proxy_url(data.proxy_url),
        },
    )
    await record_event(
        aggregate="proxy",
        aggregate_id=proxy_id,
        event_type="ProxyCreated",
        payload={"proxy_type": proxy_type, "provider": data.provider, "country": data.country},
        actor_type="operator",
        actor_id=actor["actor_id"],
    )
    return {"status": "created", "id": proxy_id}


@router.put("/{proxy_id}/cooldown")
async def cool_proxy(proxy_id: int, data: ProxyCooldownRequest, request: Request):
    actor = require_min_role(request, "operator")
    minutes = max(1, min(int(data.minutes), 1440))
    row = await database.fetch_one("SELECT id FROM proxies WHERE id = :id", {"id": proxy_id})
    if not row:
        raise HTTPException(404, detail="Proxy not found")
    cooldown_until = datetime.now() + timedelta(minutes=minutes)
    await database.execute(
        """UPDATE proxies
           SET status = 'degraded',
               cooldown_until = :cooldown_until,
               last_check_at = NOW(),
               health_score = GREATEST(health_score - 15, 0)
           WHERE id = :id""",
        {"id": proxy_id, "cooldown_until": cooldown_until},
    )
    await audit_event(
        request,
        action="proxy.cooldown",
        resource_type="proxy",
        resource_id=proxy_id,
        result="cooling",
        risk_level="low",
        detail={"minutes": minutes, "reason": data.reason},
    )
    await record_event(
        aggregate="proxy",
        aggregate_id=proxy_id,
        event_type="ProxyCooldownSet",
        payload={"minutes": minutes, "reason": data.reason},
        actor_type="operator",
        actor_id=actor["actor_id"],
    )
    return {"status": "cooling", "id": proxy_id, "minutes": minutes, "reason": data.reason}


@router.post("/{proxy_id}/check")
async def check_proxy(proxy_id: int, request: Request, data: ProxyCheckRequest = ProxyCheckRequest()):
    actor = require_min_role(request, "operator")
    target_url = validate_check_url(data.target_url)
    if not _is_allowed_check_host(urlsplit(target_url).hostname):
        require_min_role(request, "admin")
        require_confirmation(request)
    timeout_seconds = max(2.0, min(float(data.timeout_seconds or 8.0), 20.0))
    row = await database.fetch_one("SELECT id, proxy_url, proxy_type FROM proxies WHERE id = :id", {"id": proxy_id})
    if not row:
        raise HTTPException(404, detail="Proxy not found")

    started = datetime.now()
    ok = False
    status_code = None
    error_message = None
    try:
        async with httpx.AsyncClient(
            proxy=row["proxy_url"],
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=True,
        ) as client:
            response = await client.get(target_url)
            status_code = response.status_code
            ok = 200 <= response.status_code < 500
    except Exception as e:
        error_message = str(e)[:240]

    latency_ms = int((datetime.now() - started).total_seconds() * 1000)
    if ok:
        await database.execute(
            """UPDATE proxies
               SET status = 'active',
                   health_score = LEAST(health_score + 10, 100),
                   cooldown_until = NULL,
                   last_check_at = NOW()
               WHERE id = :id""",
            {"id": proxy_id},
        )
        proxy_status = "active"
    else:
        await database.execute(
            """UPDATE proxies
               SET status = CASE WHEN status = 'dead' THEN 'dead' ELSE 'degraded' END,
                   health_score = GREATEST(health_score - 25, 0),
                   last_check_at = NOW()
               WHERE id = :id""",
            {"id": proxy_id},
        )
        proxy_status = "degraded"

    await audit_event(
        request,
        action="proxy.check",
        resource_type="proxy",
        resource_id=proxy_id,
        result="ok" if ok else "failed",
        risk_level="low",
        detail={
            "target_url": target_url,
            "status_code": status_code,
            "latency_ms": latency_ms,
            "error": error_message,
        },
    )
    await record_event(
        aggregate="proxy",
        aggregate_id=proxy_id,
        event_type="ProxyCheckCompleted",
        payload={
            "ok": ok,
            "target_url": target_url,
            "status_code": status_code,
            "latency_ms": latency_ms,
            "proxy_status": proxy_status,
        },
        actor_type="operator",
        actor_id=actor["actor_id"],
    )

    if ok:
        return {
            "status": "ok",
            "id": proxy_id,
            "proxy_status": "active",
            "target_url": target_url,
            "status_code": status_code,
            "latency_ms": latency_ms,
        }

    return {
        "status": "failed",
        "id": proxy_id,
        "proxy_status": "degraded",
        "target_url": target_url,
        "status_code": status_code,
        "latency_ms": latency_ms,
        "error": error_message or "proxy check failed",
    }


@router.put("/{proxy_id}/status")
async def update_proxy_status(proxy_id: int, data: ProxyStatusUpdate, request: Request):
    actor = require_min_role(request, "operator")
    status = (data.status or "").lower()
    if status not in VALID_PROXY_STATUSES:
        raise HTTPException(400, detail="status must be active, degraded, or dead")
    row = await database.fetch_one("SELECT id, status FROM proxies WHERE id = :id", {"id": proxy_id})
    if not row:
        raise HTTPException(404, detail="Proxy not found")
    await database.execute(
        """UPDATE proxies
           SET status = :status,
               cooldown_until = CASE WHEN :status = 'active' THEN NULL ELSE cooldown_until END,
               last_check_at = NOW()
           WHERE id = :id""",
        {"id": proxy_id, "status": status},
    )
    await audit_event(
        request,
        action="proxy.status_update",
        resource_type="proxy",
        resource_id=proxy_id,
        result="updated",
        risk_level="medium",
        detail={"from": row["status"], "to": status},
    )
    await record_event(
        aggregate="proxy",
        aggregate_id=proxy_id,
        event_type="ProxyStatusChanged",
        payload={"from": row["status"], "to": status},
        actor_type="operator",
        actor_id=actor["actor_id"],
    )
    return {"status": "updated", "id": proxy_id, "proxy_status": status}


def validate_proxy_url(value: str):
    parsed = urlsplit(value or "")
    if parsed.scheme not in {"socks5", "http", "https"}:
        raise HTTPException(400, detail="proxy_url must start with socks5://, http://, or https://")
    if not parsed.hostname or not parsed.port:
        raise HTTPException(400, detail="proxy_url must include host and port")


def validate_check_url(value: str):
    parsed = urlsplit(value or "")
    if parsed.scheme not in {"http", "https"}:
        raise HTTPException(400, detail="target_url must start with http:// or https://")
    if not parsed.hostname:
        raise HTTPException(400, detail="target_url must include a host")
    return value


def serialize_proxy(row):
    item = dict(row)
    item["proxy_url"] = mask_proxy_url(item.get("proxy_url"))
    item["assigned_account"] = None
    if item.pop("account_id", None):
        item["assigned_account"] = {
            "id": row["account_id"],
            "platform": row["account_platform"],
            "status": row["account_status"],
            "risk_24h": row["account_risk_24h"] or 0,
        }
    item.pop("account_platform", None)
    item.pop("account_status", None)
    item.pop("account_risk_24h", None)
    return item


def mask_proxy_url(value: str | None) -> str:
    if not value:
        return ""
    parsed = urlsplit(value)
    if "@" not in parsed.netloc:
        return value
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    return urlunsplit((parsed.scheme, f"***:***@{host}{port}", parsed.path, parsed.query, parsed.fragment))
