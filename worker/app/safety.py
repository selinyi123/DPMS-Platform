from datetime import datetime

from app.db import database, redis
from app.utils.log import structured_log


WINDOW_SECONDS = 5 * 60
WINDOW_MAX_ACTIONS = 5
DAILY_MAX_TASKS = 30
RISK_TEXTS = [
    "captcha",
    "verify",
    "risk",
    "login",
    "\u9a8c\u8bc1\u7801",
    "\u5b89\u5168\u9a8c\u8bc1",
    "\u767b\u5f55",
]


async def ensure_account_can_run(account_id: int) -> None:
    row = await database.fetch_one(
        "SELECT status, daily_task_count, encrypted_credential FROM accounts WHERE id = :id",
        {"id": account_id},
    )
    if not row:
        raise ValueError(f"Account {account_id} not found")
    if row["status"] != "ready":
        raise ValueError(f"Account {account_id} is not ready")
    if not row["encrypted_credential"]:
        raise ValueError(f"Account {account_id} has no credential")
    if int(row["daily_task_count"] or 0) >= DAILY_MAX_TASKS:
        await set_account_status(account_id, "cooling", "daily_limit")
        raise ValueError(f"Account {account_id} reached daily limit")

    key = f"risk_window:{account_id}"
    now = datetime.now().timestamp()
    pipe = redis.pipeline()
    pipe.zadd(key, {str(now): now})
    pipe.zremrangebyscore(key, 0, now - WINDOW_SECONDS)
    pipe.zcard(key)
    pipe.expire(key, WINDOW_SECONDS * 2)
    _, _, count, _ = await pipe.execute()
    if count > WINDOW_MAX_ACTIONS:
        await set_account_status(account_id, "cooling", "action_window")
        raise ValueError(f"Account {account_id} exceeded action window")


async def detect_page_risk(page, account_id: int) -> None:
    try:
        body = await page.locator("body").text_content(timeout=2000)
    except Exception:
        return
    body = body or ""
    lower = body.lower()
    if any(text.lower() in lower for text in RISK_TEXTS):
        target = "login_required" if "login" in lower or "\u767b\u5f55" in body else "cooling"
        await set_account_status(account_id, target, "page_risk_signal")
        raise ValueError(f"Risk signal detected on page for account {account_id}")


async def set_account_status(account_id: int, status: str, reason: str):
    await database.execute(
        "UPDATE accounts SET status = :status, updated_at = NOW(), version = version + 1 WHERE id = :id",
        {"id": account_id, "status": status},
    )
    await database.execute(
        """INSERT INTO risk_events (account_id, event_type, detail)
           VALUES (:account_id, :event_type, JSON_OBJECT('reason', :reason))""",
        {"account_id": account_id, "event_type": status, "reason": reason},
    )
    await redis.xadd(
        "notify_events",
        {
            "event_type": "account_risk",
            "severity": "warning" if status in {"cooling", "login_required"} else "critical",
            "title": f"Account A{account_id} moved to {status}",
            "content": f"Account A{account_id} status changed to {status}. Reason: {reason}",
            "account_id": str(account_id),
            "status": status,
            "channels": "all",
        },
    )
    structured_log("warning", "account_status_changed", account_id=account_id, status=status, reason=reason)
