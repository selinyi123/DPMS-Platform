import asyncio
import json
from pathlib import Path

from app.db import database, redis
from app.event_store.service import record_event
from app.platforms import get_platform
from app.safety import detect_page_risk
from app.utils.cookies import inject_account_cookies
from app.utils.crypto import cookie_vault
from app.utils.log import structured_log


STREAM_KEY = "account_calibration_requests"
GROUP_NAME = "account-calibrators"
CONSUMER_NAME = "account-calibrator-1"
SCREENSHOT_DIR = Path("/profiles/account-calibrations")


async def calibration_loop(pool, shutdown_event: asyncio.Event):
    try:
        await redis.xgroup_create(STREAM_KEY, GROUP_NAME, id="0", mkstream=True)
    except Exception:
        pass

    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    while not shutdown_event.is_set():
        try:
            messages = await asyncio.wait_for(
                redis.xreadgroup(GROUP_NAME, CONSUMER_NAME, {STREAM_KEY: ">"}, count=1, block=5000),
                timeout=1,
            )
            if not messages:
                continue
            for msg_id, data in messages[0][1]:
                await handle_calibration(pool, {key: value for key, value in data.items()})
                await redis.xack(STREAM_KEY, GROUP_NAME, msg_id)
        except asyncio.TimeoutError:
            continue
        except asyncio.CancelledError:
            break
        except Exception as e:
            structured_log("error", "account_calibration_loop_error", exception=e)
            await asyncio.sleep(3)


async def handle_calibration(pool, task: dict):
    calibration_id = task["calibration_id"]
    account_id = int(task["account_id"])
    platform = task.get("platform", "bilibili")
    cfg = get_platform(platform)
    check_url = task.get("check_url") or (cfg or {}).get("account_check_url") or (cfg or {}).get("login_url")
    screenshot_path = str(SCREENSHOT_DIR / f"{calibration_id}.png")

    await database.execute(
        "UPDATE account_calibrations SET status = 'running', started_at = NOW() WHERE calibration_id = :id",
        {"id": calibration_id},
    )
    await record_event(
        aggregate="account",
        aggregate_id=account_id,
        event_type="AccountCalibrationStarted",
        payload={"platform": platform, "calibration_id": calibration_id, "check_url": check_url},
        correlation_id=calibration_id,
    )

    ctx = None
    page = None
    try:
        if not cfg:
            raise ValueError(f"Unsupported platform: {platform}")
        ctx = await pool.get_account_context(account_id, f"/profiles/{platform}/account_{account_id}")
        await inject_calibration_cookies(ctx, account_id, platform)
        page = await ctx.new_page()
        await page.goto(check_url, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(2000)
        await detect_page_risk(page, account_id)

        cookies = await ctx.cookies()
        cookie_names = {cookie.get("name") for cookie in cookies}
        required = set(cfg.get("required_cookies", []))
        required_present = sorted(required.intersection(cookie_names))
        missing = sorted(required.difference(cookie_names))
        if missing:
            await mark_account_login_required(account_id, f"missing cookies after calibration: {', '.join(missing)}")
            raise ValueError(f"Missing required cookies after calibration: {', '.join(missing)}")

        result = {
            "check_url": check_url,
            "final_url": page.url,
            "title": await safe_title(page),
            "required_present": required_present,
        }
        await page.screenshot(path=screenshot_path, full_page=True)
        await database.execute(
            """UPDATE account_calibrations
               SET status = 'succeeded', result = :result, screenshot_path = :screenshot_path, finished_at = NOW()
               WHERE calibration_id = :id""",
            {"id": calibration_id, "result": json.dumps(result, ensure_ascii=False), "screenshot_path": screenshot_path},
        )
        await database.execute(
            """UPDATE accounts
               SET status = 'ready', updated_at = NOW(), version = version + 1
               WHERE id = :account_id AND status IN ('warming', 'cooling', 'login_required')""",
            {"account_id": account_id},
        )
        await emit_calibration_notification(
            account_id,
            calibration_id,
            "succeeded",
            f"Account A{account_id} login calibration succeeded for {platform}. Final URL: {result['final_url']}",
            "info",
        )
        await record_event(
            aggregate="account",
            aggregate_id=account_id,
            event_type="AccountCalibrated",
            payload={"platform": platform, "calibration_id": calibration_id, "result": result, "screenshot_path": screenshot_path},
            correlation_id=calibration_id,
        )
        structured_log("info", "account_calibration_succeeded", account_id=account_id, task_id=calibration_id)
    except Exception as e:
        if page:
            try:
                await page.screenshot(path=screenshot_path, full_page=True)
            except Exception:
                screenshot_path = None
        await database.execute(
            """UPDATE account_calibrations
               SET status = 'failed', error_message = :error, screenshot_path = :screenshot_path, finished_at = NOW()
               WHERE calibration_id = :id""",
            {"id": calibration_id, "error": str(e), "screenshot_path": screenshot_path},
        )
        await emit_calibration_notification(
            account_id,
            calibration_id,
            "failed",
            f"Account A{account_id} login calibration failed for {platform}. Error: {e}",
            "warning",
        )
        await record_event(
            aggregate="account",
            aggregate_id=account_id,
            event_type="AccountCalibrationFailed",
            payload={"platform": platform, "calibration_id": calibration_id, "error": str(e), "screenshot_path": screenshot_path},
            correlation_id=calibration_id,
        )
        structured_log("error", "account_calibration_failed", account_id=account_id, task_id=calibration_id, exception=e)
    finally:
        if page:
            await page.close()


async def inject_calibration_cookies(ctx, account_id: int, platform: str):
    row = await database.fetch_one(
        "SELECT encrypted_credential FROM accounts WHERE id = :id",
        {"id": account_id},
    )
    if not row or not row["encrypted_credential"]:
        raise ValueError(f"Account {account_id} has no imported login Cookie")
    credential_blob = row["encrypted_credential"]
    try:
        credential = cookie_vault.decrypt(credential_blob)
    except Exception:
        credential = credential_blob.decode("utf-8") if isinstance(credential_blob, bytes) else str(credential_blob)
    await inject_account_cookies(ctx, platform, credential)


async def mark_account_login_required(account_id: int, reason: str):
    await database.execute(
        """UPDATE accounts
           SET status = 'login_required', updated_at = NOW(), version = version + 1
           WHERE id = :account_id""",
        {"account_id": account_id},
    )
    await database.execute(
        """INSERT INTO risk_events (account_id, event_type, detail)
           VALUES (:account_id, 'login_required', JSON_OBJECT('reason', :reason))""",
        {"account_id": account_id, "reason": reason},
    )
    await record_event(
        aggregate="account",
        aggregate_id=account_id,
        event_type="AccountLoginRequired",
        payload={"reason": reason},
    )
    await record_event(
        aggregate="risk",
        aggregate_id=f"account:{account_id}",
        event_type="RiskDetected",
        payload={"account_id": account_id, "event_type": "login_required", "reason": reason},
    )


async def emit_calibration_notification(account_id: int, calibration_id: str, status: str, content: str, severity: str):
    await redis.xadd(
        "notify_events",
        {
            "event_type": "account_calibration",
            "severity": severity,
            "title": f"Account calibration {status}: A{account_id}",
            "content": content,
            "account_id": str(account_id),
            "calibration_id": calibration_id,
            "status": status,
            "channels": "all",
        },
    )


async def safe_title(page) -> str:
    try:
        return await page.title()
    except Exception:
        return ""
