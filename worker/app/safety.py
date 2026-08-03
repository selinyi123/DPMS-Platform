import asyncio
from datetime import datetime

from app.db import database, execute_affected_rows, redis
from app.utils.log import structured_log


WINDOW_SECONDS = 10 * 60
# This Redis window counts task starts. Per-target remote POST attempts,
# including retries, are bounded separately by BiliEngineConfig.
WINDOW_MAX_TASK_STARTS = 2
DAILY_MAX_TASKS = 8
# Xiaohongshu's risk control is known to be stricter than the other
# platforms, so its accounts get tighter compliance limits by default.
PLATFORM_WINDOW_MAX_TASK_STARTS = {
    "xiaohongshu": 1,
}
PLATFORM_DAILY_MAX_TASKS = {
    "xiaohongshu": 5,
}
RISK_TEXTS = [
    "captcha",
    "geetest",
    "security verification",
    "verify your identity",
    "too many requests",
    "account is abnormal",
    "\u9a8c\u8bc1\u7801",
    "\u5b89\u5168\u9a8c\u8bc1",
    "\u8bf7\u5b8c\u6210\u9a8c\u8bc1",
    "\u64cd\u4f5c\u9891\u7e41",
    "\u8d26\u53f7\u5f02\u5e38",
]
# Platform-specific risk phrases. These are matched in addition to
# RISK_TEXTS and only ever trigger the existing stop+notify "cooling" path
# (account_calibrator/safety never bypass or hide these signals).
PLATFORM_RISK_TEXTS = {
    "xiaohongshu": [
        "\u8bbe\u5907\u73af\u5883\u5f02\u5e38",
        "\u5f53\u524d\u8d26\u53f7\u5b58\u5728\u5f02\u5e38\u884c\u4e3a",
        "\u8bbf\u95ee\u9891\u7387\u8fc7\u9ad8",
        "\u8bbf\u95ee\u592a\u9891\u7e41\uff0c\u8bf7\u7a0d\u540e\u91cd\u8bd5",
        "\u7b14\u8bb0\u4e0d\u5b58\u5728\u6216\u5df2\u88ab\u5220\u9664",
        "\u8d26\u53f7\u5df2\u88ab\u9650\u5236",
    ],
    "weibo": [
        "\u8bf7\u6c42\u9891\u7e41\uff0c\u7a0d\u540e\u518d\u8bd5",
        "\u7cfb\u7edf\u68c0\u6d4b\u5230\u5f02\u5e38\u8bbf\u95ee",
        "\u8be5\u5fae\u535a\u5df2\u88ab\u5220\u9664",
    ],
    "douyin": [
        "\u8bf7\u8fdb\u884c\u5b89\u5168\u9a8c\u8bc1\u540e\u7ee7\u7eed\u64cd\u4f5c",
        "\u5f53\u524d\u8bbe\u5907\u73af\u5883\u5f02\u5e38",
        "\u89c6\u9891\u4e0d\u5b58\u5728\u6216\u5df2\u88ab\u4f5c\u8005\u5220\u9664",
    ],
}
LOGIN_URL_MARKERS = (
    "passport.bilibili.com/login",
    "/passport/web/login",
    "/signin/login",
    "passport.weibo.com",
    "xiaohongshu.com/login",
)


class AccountStatusPersistenceFailed(RuntimeError):
    """The canonical account risk state and audit commit could not be confirmed."""

    def __init__(self, account_id: int, status: str, reason: str, cause: BaseException):
        self.account_id = account_id
        self.status = status
        self.reason = reason
        super().__init__(
            f"account_status_persistence_failed:{account_id}:{status}:{type(cause).__name__}"
        )


class AccountExecutionRevisionMismatch(RuntimeError):
    """A stale operation tried to write safety state for replaced credentials."""

    def __init__(self, account_id: int, expected_execution_revision: int):
        self.account_id = account_id
        self.expected_execution_revision = expected_execution_revision
        super().__init__(
            "account_execution_revision_mismatch:"
            f"{account_id}:{expected_execution_revision}"
        )


async def ensure_account_can_run(account_id: int, platform: str | None = None) -> None:
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
    daily_max_tasks = PLATFORM_DAILY_MAX_TASKS.get(platform or "", DAILY_MAX_TASKS)
    if int(row["daily_task_count"] or 0) >= daily_max_tasks:
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
    window_max_task_starts = PLATFORM_WINDOW_MAX_TASK_STARTS.get(
        platform or "", WINDOW_MAX_TASK_STARTS
    )
    if count > window_max_task_starts:
        await set_account_status(account_id, "cooling", "action_window")
        raise ValueError(f"Account {account_id} exceeded action window")


async def detect_page_risk(
    page,
    account_id: int,
    platform: str | None = None,
    *,
    expected_execution_revision: int | None = None,
) -> None:
    current_url = str(getattr(page, "url", "") or "").lower()
    if any(marker in current_url for marker in LOGIN_URL_MARKERS):
        await set_account_status(
            account_id,
            "login_required",
            "redirected_to_login",
            expected_execution_revision=expected_execution_revision,
        )
        raise ValueError(f"Login is required for account {account_id}")
    try:
        body = await page.locator("body").text_content(timeout=2000)
    except Exception:
        return
    body = body or ""
    lower = body.lower()
    risk_texts = RISK_TEXTS + PLATFORM_RISK_TEXTS.get(platform or "", [])
    if any(text.lower() in lower for text in risk_texts):
        await set_account_status(
            account_id,
            "cooling",
            "page_risk_signal",
            expected_execution_revision=expected_execution_revision,
        )
        raise ValueError(f"Risk signal detected on page for account {account_id}")


async def set_account_status(
    account_id: int,
    status: str,
    reason: str,
    *,
    expected_execution_revision: int | None = None,
):
    try:
        # The account state and its durable audit evidence form one safety
        # settlement.  Committing only one of them either releases a risky
        # account or makes the state change unauditable.
        async with database.transaction():
            if expected_execution_revision is None:
                await database.execute(
                    "UPDATE accounts SET status = :status, updated_at = NOW(), version = version + 1 WHERE id = :id",
                    {"id": account_id, "status": status},
                )
            else:
                affected = await execute_affected_rows(
                    """UPDATE accounts
                          SET status = :status, updated_at = NOW(),
                              version = version + 1
                        WHERE id = :id
                          AND deleted_at IS NULL
                          AND execution_revision = :execution_revision""",
                    {
                        "id": account_id,
                        "status": status,
                        "execution_revision": expected_execution_revision,
                    },
                    db=database,
                )
                if affected != 1:
                    raise AccountExecutionRevisionMismatch(
                        account_id, expected_execution_revision
                    )
            await database.execute(
                """INSERT INTO risk_events (account_id, event_type, detail)
                   VALUES (:account_id, :event_type, JSON_OBJECT('reason', :reason))""",
                {"account_id": account_id, "event_type": status, "reason": reason},
            )
    except asyncio.CancelledError:
        # Preserve cooperative shutdown semantics.  The claimed task/message
        # remains unsettled and Recovery applies the real-run quarantine path;
        # a cancellation during commit cannot truthfully be classified as a
        # confirmed rollback or a confirmed commit here.
        raise
    except AccountExecutionRevisionMismatch:
        raise
    except Exception as exc:
        raise AccountStatusPersistenceFailed(account_id, status, reason, exc) from exc

    # Notification delivery is not part of the canonical safety settlement.
    # A transient Redis outage must not turn an already-quarantined account
    # into a failed task whose generic cleanup can release it again.
    try:
        notification_id = await redis.xadd(
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
        if not notification_id:
            structured_log(
                "warning",
                "account_status_notification_failed",
                account_id=account_id,
                status=status,
                reason=reason,
                failure="enqueue_unconfirmed",
            )
    except Exception as exc:
        structured_log(
            "warning",
            "account_status_notification_failed",
            account_id=account_id,
            status=status,
            reason=reason,
            exception=exc,
        )
    structured_log("warning", "account_status_changed", account_id=account_id, status=status, reason=reason)
