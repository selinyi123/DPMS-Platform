from datetime import datetime, timedelta
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastapi import APIRouter, HTTPException

from app.db import database
from app.models.schemas import ProxyCheckRequest, ProxyCooldownRequest, ProxyCreate, ProxyStatusUpdate


router = APIRouter()

VALID_PROXY_STATUSES = {"active", "degraded", "dead"}


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
async def create_proxy(data: ProxyCreate):
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
    return {"status": "created", "id": proxy_id}


@router.put("/{proxy_id}/cooldown")
async def cool_proxy(proxy_id: int, data: ProxyCooldownRequest):
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
    return {"status": "cooling", "id": proxy_id, "minutes": minutes, "reason": data.reason}


@router.post("/{proxy_id}/check")
async def check_proxy(proxy_id: int, data: ProxyCheckRequest = ProxyCheckRequest()):
    target_url = validate_check_url(data.target_url)
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
        return {
            "status": "ok",
            "id": proxy_id,
            "proxy_status": "active",
            "target_url": target_url,
            "status_code": status_code,
            "latency_ms": latency_ms,
        }

    await database.execute(
        """UPDATE proxies
           SET status = CASE WHEN status = 'dead' THEN 'dead' ELSE 'degraded' END,
               health_score = GREATEST(health_score - 25, 0),
               last_check_at = NOW()
           WHERE id = :id""",
        {"id": proxy_id},
    )
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
async def update_proxy_status(proxy_id: int, data: ProxyStatusUpdate):
    status = (data.status or "").lower()
    if status not in VALID_PROXY_STATUSES:
        raise HTTPException(400, detail="status must be active, degraded, or dead")
    row = await database.fetch_one("SELECT id FROM proxies WHERE id = :id", {"id": proxy_id})
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
