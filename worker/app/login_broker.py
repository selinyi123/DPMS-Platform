import asyncio
import uuid
from pathlib import Path

from app.db import database, redis
from app.event_store.service import record_event
from app.platforms import get_platform
from app.utils.cookies import serialize_cookies
from app.utils.crypto import CREDENTIAL_AAD, cookie_vault
from app.utils.log import structured_log


STREAM_KEY = "login_requests"
GROUP_NAME = "login-workers"
CONSUMER_NAME = "login-worker-1"
PROFILE_ROOT = Path("/profiles/login-sessions")
MAX_ACTIVE_LOGIN_SESSIONS = 2


async def login_loop(pool, shutdown_event: asyncio.Event):
    try:
        await redis.xgroup_create(STREAM_KEY, GROUP_NAME, id="0", mkstream=True)
    except Exception:
        pass

    PROFILE_ROOT.mkdir(parents=True, exist_ok=True)
    active_tasks = set()
    semaphore = asyncio.Semaphore(MAX_ACTIVE_LOGIN_SESSIONS)

    while not shutdown_event.is_set():
        try:
            active_tasks = {task for task in active_tasks if not task.done()}
            msgs = await asyncio.wait_for(
                redis.xreadgroup(GROUP_NAME, CONSUMER_NAME, {STREAM_KEY: ">"}, count=1, block=5000),
                timeout=1,
            )
            if not msgs:
                continue
            for msg_id, data in msgs[0][1]:
                session = {k: v for k, v in data.items()}
                active_tasks.add(asyncio.create_task(handle_and_ack(pool, msg_id, session, semaphore)))
        except asyncio.TimeoutError:
            continue
        except asyncio.CancelledError:
            break
        except Exception as e:
            structured_log("error", "login_loop_error", exception=e)
            await asyncio.sleep(3)

    for task in active_tasks:
        task.cancel()


async def handle_and_ack(pool, msg_id: str, session: dict, semaphore: asyncio.Semaphore):
    try:
        async with semaphore:
            await handle_login_session(pool, session)
    finally:
        await redis.xack(STREAM_KEY, GROUP_NAME, msg_id)


async def handle_login_session(pool, session: dict):
    session_id = session["session_id"]
    platform = session.get("platform", "bilibili")
    cfg = get_platform(platform)
    if not cfg:
        await update_session(session_id, "failed", error_message=f"Unsupported platform: {platform}")
        await record_event(
            aggregate="browser",
            aggregate_id=session_id,
            event_type="QrLoginFailed",
            payload={"platform": platform, "error": f"Unsupported platform: {platform}"},
            correlation_id=session_id,
        )
        return
    login_url = session.get("login_url") or cfg["login_url"]
    profile_dir = PROFILE_ROOT / session_id / "profile"
    image_path = PROFILE_ROOT / f"{session_id}.png"

    await update_session(session_id, "opening", qr_image_path=str(image_path), error_message=None)
    await record_event(
        aggregate="browser",
        aggregate_id=session_id,
        event_type="QrLoginOpening",
        payload={"platform": platform, "login_url": login_url, "qr_image_path": str(image_path)},
        correlation_id=session_id,
    )
    ctx = await pool.get_transient_context(str(profile_dir))
    page = await ctx.new_page()
    try:
        await page.goto(login_url, wait_until="domcontentloaded", timeout=45000)
        await update_session(session_id, "waiting_scan", qr_image_path=str(image_path))
        await record_event(
            aggregate="browser",
            aggregate_id=session_id,
            event_type="QrLoginWaitingScan",
            payload={"platform": platform, "qr_image_path": str(image_path)},
            correlation_id=session_id,
        )

        deadline = asyncio.get_running_loop().time() + 300
        required = set(cfg.get("required_cookies", []))
        while asyncio.get_running_loop().time() < deadline:
            await page.screenshot(path=str(image_path), full_page=True)
            cookies = await ctx.cookies()
            names = {cookie.get("name") for cookie in cookies}
            if required and required.issubset(names):
                created = await create_account_from_cookies(platform, cookies)
                account_id = created["account_id"]
                await update_session(session_id, "confirmed", account_id=account_id)
                await record_event(
                    aggregate="browser",
                    aggregate_id=session_id,
                    event_type="QrLoginCompleted",
                    payload={"platform": platform, "account_id": account_id, "calibration": created.get("calibration")},
                    correlation_id=session_id,
                )
                await record_event(
                    aggregate="account",
                    aggregate_id=account_id,
                    event_type="AccountCreated",
                    payload={"platform": platform, "credential_imported": True, "source": "qr_login", "calibration": created.get("calibration")},
                    correlation_id=session_id,
                )
                structured_log("info", "qr_login_confirmed", account_id=account_id)
                return
            await asyncio.sleep(3)

        await update_session(session_id, "expired", error_message="QR login session expired")
        await record_event(
            aggregate="browser",
            aggregate_id=session_id,
            event_type="QrLoginExpired",
            payload={"platform": platform},
            correlation_id=session_id,
        )
    except Exception as e:
        await update_session(session_id, "failed", error_message=str(e))
        await record_event(
            aggregate="browser",
            aggregate_id=session_id,
            event_type="QrLoginFailed",
            payload={"platform": platform, "error": str(e)},
            correlation_id=session_id,
        )
        structured_log("error", "qr_login_failed", exception=e)
    finally:
        await page.close()
        await ctx.close()


async def create_account_from_cookies(platform: str, cookies: list[dict]) -> dict:
    credential = cookie_vault.encrypt(serialize_cookies(platform, cookies), aad=CREDENTIAL_AAD)
    fingerprint_id = await ensure_default_fingerprint(platform)
    account_id = await database.execute(
        """INSERT INTO accounts (platform, fingerprint_id, encrypted_credential, status)
           VALUES (:platform, :fingerprint_id, :credential, 'warming')""",
        {"platform": platform, "fingerprint_id": fingerprint_id, "credential": credential},
    )
    calibration = await queue_account_calibration(account_id, platform)
    return {"account_id": account_id, "calibration": calibration}


async def queue_account_calibration(account_id: int, platform: str):
    cfg = get_platform(platform)
    calibration_id = str(uuid.uuid4())
    check_url = cfg.get("account_check_url") or cfg["login_url"]
    await database.execute(
        """INSERT INTO account_calibrations (calibration_id, platform, account_id, check_url, status)
           VALUES (:calibration_id, :platform, :account_id, :check_url, 'queued')""",
        {
            "calibration_id": calibration_id,
            "platform": platform,
            "account_id": account_id,
            "check_url": check_url,
        },
    )
    await redis.xadd(
        "account_calibration_requests",
        {
            "calibration_id": calibration_id,
            "platform": platform,
            "account_id": str(account_id),
            "check_url": check_url,
        },
    )
    calibration = {"calibration_id": calibration_id, "status": "queued", "check_url": check_url}
    await record_event(
        aggregate="account",
        aggregate_id=account_id,
        event_type="AccountCalibrationQueued",
        payload={"platform": platform, "calibration": calibration},
        correlation_id=calibration_id,
    )
    return calibration


async def ensure_default_fingerprint(platform: str) -> int:
    user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"
    row = await database.fetch_one(
        "SELECT id FROM fingerprints WHERE platform = :platform AND user_agent = :ua",
        {"platform": platform, "ua": user_agent},
    )
    if row:
        return row["id"]
    return await database.execute(
        """INSERT INTO fingerprints (platform, user_agent, viewport_width, viewport_height, timezone, locale)
           VALUES (:platform, :ua, 1920, 1080, 'Asia/Shanghai', 'zh-CN')""",
        {"platform": platform, "ua": user_agent},
    )


async def update_session(session_id: str, status: str, **fields):
    assignments = ["status = :status", "updated_at = NOW()"]
    values = {"session_id": session_id, "status": status}
    if status == "confirmed":
        assignments.append("completed_at = NOW()")
    for key, value in fields.items():
        assignments.append(f"{key} = :{key}")
        values[key] = value
    await database.execute(
        f"UPDATE login_sessions SET {', '.join(assignments)} WHERE session_id = :session_id",
        values,
    )
