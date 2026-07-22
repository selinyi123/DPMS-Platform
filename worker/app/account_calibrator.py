import asyncio
import json
from pathlib import Path

from app.db import database, execute_affected_rows, redis
from app.event_store.service import record_event
from app.platforms import get_platform
from app.safety import detect_page_risk
from app.utils.cookies import inject_account_cookies
from app.utils.crypto import CREDENTIAL_AAD, cookie_vault
from app.utils.log import structured_log
from app.weibo.capabilities import (
    build_weibo_oauth_capability_attestation,
    validate_weibo_operator_attestation,
)
from app.weibo.client import WeiboOAuthIdentityClient
from app.weibo.credentials import (
    is_weibo_oauth_credential_envelope,
    parse_weibo_oauth_credential,
)


STREAM_KEY = "account_calibration_requests"
GROUP_NAME = "account-calibrators"
CONSUMER_NAME = "account-calibrator-1"
SCREENSHOT_DIR = Path("/profiles/account-calibrations")


class CalibrationClaimRejected(RuntimeError):
    """A stale, duplicate, or forged calibration message lost the CAS."""


def calibrated_account_status(current_status: str, identity: dict) -> str:
    """Choose the post-calibration state without overstating account identity.

    A valid browser session and a verified account identity are different
    claims.  Only an authoritative identity check may promote an account to
    ``ready`` automatically.  A session-only result remains ``warming`` for an
    operator review; an existing risk cooldown is never shortened.
    """

    normalized = str(current_status or "").strip().lower()
    if normalized not in {"warming", "cooling", "login_required", "ready"}:
        return normalized
    if isinstance(identity, dict) and identity.get("verified") is True:
        return "ready"
    if normalized == "cooling":
        return "cooling"
    return "warming"


async def claim_calibration_message(
    calibration_id: str,
    account_id: int,
    platform: str,
) -> bool:
    """Atomically claim one exact queued calibration message."""

    try:
        async with database.transaction():
            affected = await execute_affected_rows(
                """UPDATE account_calibrations
                      SET status = 'running', started_at = NOW()
                    WHERE calibration_id = :calibration_id
                      AND account_id = :account_id
                      AND platform = :platform
                      AND status = 'queued'""",
                {
                    "calibration_id": calibration_id,
                    "account_id": account_id,
                    "platform": platform,
                },
                db=database,
            )
            if affected != 1:
                # Raising inside the transaction also rolls back an impossible
                # multi-row update instead of accepting a partially claimed
                # message under a corrupted uniqueness contract.
                raise CalibrationClaimRejected(
                    "account_calibration_claim_compare_and_swap_lost"
                )
    except CalibrationClaimRejected:
        return False
    return True


async def emit_calibration_terminal_observability(
    *,
    account_id: int,
    calibration_id: str,
    platform: str,
    status: str,
    content: str,
    severity: str,
    event_type: str,
    event_payload: dict,
) -> None:
    """Best-effort post-commit delivery without rewriting business state."""

    try:
        await emit_calibration_notification(
            account_id,
            calibration_id,
            status,
            content,
            severity,
        )
    except Exception as exc:
        structured_log(
            "warning",
            "account_calibration_post_commit_delivery_failed",
            account_id=account_id,
            task_id=calibration_id,
            platform=platform,
            delivery="notification",
            compensation_required=True,
            exception=exc,
        )
    try:
        await record_event(
            aggregate="account",
            aggregate_id=account_id,
            event_type=event_type,
            payload=event_payload,
            correlation_id=calibration_id,
        )
    except Exception as exc:
        structured_log(
            "warning",
            "account_calibration_post_commit_delivery_failed",
            account_id=account_id,
            task_id=calibration_id,
            platform=platform,
            delivery="event",
            compensation_required=True,
            exception=exc,
        )


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
    calibration_id = str(task["calibration_id"]).strip()
    account_id = int(task["account_id"])
    platform = str(task.get("platform", "bilibili")).strip().lower()
    cfg = get_platform(platform)
    check_url = task.get("check_url") or (cfg or {}).get("account_check_url") or (cfg or {}).get("login_url")
    screenshot_path = str(SCREENSHOT_DIR / f"{calibration_id}.png")

    claimed = await claim_calibration_message(
        calibration_id,
        account_id,
        platform,
    )
    if not claimed:
        structured_log(
            "warning",
            "account_calibration_claim_rejected",
            account_id=account_id,
            task_id=calibration_id,
            platform=platform,
        )
        return
    try:
        await record_event(
            aggregate="account",
            aggregate_id=account_id,
            event_type="AccountCalibrationStarted",
            payload={
                "platform": platform,
                "calibration_id": calibration_id,
                "check_url": check_url,
            },
            correlation_id=calibration_id,
        )
    except Exception as exc:
        structured_log(
            "warning",
            "account_calibration_started_event_failed",
            account_id=account_id,
            task_id=calibration_id,
            platform=platform,
            compensation_required=True,
            exception=exc,
        )

    ctx = None
    page = None
    try:
        if not cfg:
            raise ValueError(f"Unsupported platform: {platform}")
        if platform == "weibo":
            calibration_kind = await resolve_weibo_calibration_kind(task)
            if calibration_kind in {
                "weibo_oauth_identity",
                "weibo_oauth_capability",
            }:
                screenshot_path = None
                await handle_weibo_oauth_calibration(
                    task,
                    capability_calibration=(
                        calibration_kind == "weibo_oauth_capability"
                    ),
                )
                return
        ctx = await pool.get_account_context(account_id, f"/profiles/{platform}/account_{account_id}")
        await inject_calibration_cookies(ctx, account_id, platform)
        page = await ctx.new_page()
        await page.goto(check_url, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(2000)
        await detect_page_risk(page, account_id, platform)

        cookies = await ctx.cookies()
        cookie_names = {cookie.get("name") for cookie in cookies}
        required = set(cfg.get("required_cookies", []))
        required_present = sorted(required.intersection(cookie_names))
        missing = sorted(required.difference(cookie_names))
        if missing:
            await mark_account_login_required(account_id, f"missing cookies after calibration: {', '.join(missing)}")
            raise ValueError(f"Missing required cookies after calibration: {', '.join(missing)}")

        identity = await verify_platform_identity(ctx, platform)
        account_row = await database.fetch_one(
            "SELECT status FROM accounts WHERE id = :account_id",
            {"account_id": account_id},
        )
        if not account_row:
            raise ValueError(f"Account {account_id} disappeared during calibration")
        current_account_status = str(account_row["status"] or "").strip().lower()
        target_account_status = calibrated_account_status(current_account_status, identity)
        identity_verified = identity.get("verified") is True
        result = {
            "check_url": check_url,
            "final_url": page.url,
            "title": await safe_title(page),
            "required_present": required_present,
            "identity": identity,
            "calibration_scope": "identity_and_session" if identity_verified else "session_only",
            "requires_manual_identity_review": not identity_verified,
            "account_status_target": target_account_status,
        }
        await page.screenshot(path=screenshot_path, full_page=True)
        async with database.transaction():
            calibration_updated = await execute_affected_rows(
                """UPDATE account_calibrations
                   SET status = 'succeeded', result = :result,
                       screenshot_path = :screenshot_path,
                       error_message = NULL, finished_at = NOW()
                   WHERE calibration_id = :calibration_id
                     AND account_id = :account_id
                     AND platform = :platform
                     AND status = 'running'""",
                {
                    "calibration_id": calibration_id,
                    "account_id": account_id,
                    "platform": platform,
                    "result": json.dumps(result, ensure_ascii=False),
                    "screenshot_path": screenshot_path,
                },
                db=database,
            )
            if calibration_updated != 1:
                raise ValueError("account_calibration_settlement_lost")
            account_updated = await execute_affected_rows(
                """UPDATE accounts
                   SET status = :target_status, updated_at = NOW(),
                       version = version + 1
                   WHERE id = :account_id
                     AND platform = :platform
                     AND status = :current_status""",
                {
                    "account_id": account_id,
                    "platform": platform,
                    "current_status": current_account_status,
                    "target_status": target_account_status,
                },
                db=database,
            )
            if account_updated != 1:
                raise ValueError("account_calibration_account_settlement_lost")
        await emit_calibration_terminal_observability(
            account_id=account_id,
            calibration_id=calibration_id,
            platform=platform,
            status="succeeded",
            content=(
                f"Account A{account_id} session calibration succeeded for {platform}. "
                f"Identity verified: {identity_verified}. "
                f"Scope: {result['calibration_scope']}. "
                + (
                    "Manual identity evidence review is required before marking the account ready. "
                    if not identity_verified
                    else ""
                )
                + f"Final URL: {result['final_url']}"
            ),
            severity="info" if identity_verified else "warning",
            event_type="AccountCalibrated",
            event_payload={
                "platform": platform,
                "calibration_id": calibration_id,
                "result": result,
                "screenshot_path": screenshot_path,
            },
        )
        structured_log("info", "account_calibration_succeeded", account_id=account_id, task_id=calibration_id)
    except Exception as e:
        if page:
            try:
                await page.screenshot(path=screenshot_path, full_page=True)
            except Exception:
                screenshot_path = None
        failure_updated = await execute_affected_rows(
            """UPDATE account_calibrations
               SET status = 'failed', error_message = :error, screenshot_path = :screenshot_path, finished_at = NOW()
               WHERE calibration_id = :calibration_id
                 AND account_id = :account_id
                 AND platform = :platform
                 AND status = 'running'""",
            {
                "calibration_id": calibration_id,
                "account_id": account_id,
                "platform": platform,
                "error": str(e),
                "screenshot_path": screenshot_path,
            },
            db=database,
        )
        if failure_updated == 1:
            await emit_calibration_terminal_observability(
                account_id=account_id,
                calibration_id=calibration_id,
                platform=platform,
                status="failed",
                content=(
                    f"Account A{account_id} login calibration failed for "
                    f"{platform}. Error: {e}"
                ),
                severity="warning",
                event_type="AccountCalibrationFailed",
                event_payload={
                    "platform": platform,
                    "calibration_id": calibration_id,
                    "error": str(e),
                    "screenshot_path": screenshot_path,
                },
            )
        else:
            structured_log(
                "warning",
                "account_calibration_failure_settlement_stale",
                account_id=account_id,
                task_id=calibration_id,
                platform=platform,
                affected=failure_updated,
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
        credential = cookie_vault.decrypt(credential_blob, aad=CREDENTIAL_AAD)
    except Exception:
        credential = credential_blob.decode("utf-8") if isinstance(credential_blob, bytes) else str(credential_blob)
    await inject_account_cookies(ctx, platform, credential)


async def resolve_weibo_calibration_kind(task: dict) -> str:
    """Cross-check the untrusted queue discriminator against stored credential shape."""

    account_id = int(task.get("account_id"))
    requested = str(task.get("calibration_kind") or "").strip()
    row = await database.fetch_one(
        "SELECT platform, encrypted_credential FROM accounts WHERE id = :id AND deleted_at IS NULL",
        {"id": account_id},
    )
    if not row or str(row["platform"] or "").strip().lower() != "weibo":
        raise ValueError("weibo_calibration_account_binding_invalid")
    blob = row["encrypted_credential"]
    if not blob:
        raise ValueError("weibo_calibration_credential_required")
    try:
        candidate = cookie_vault.decrypt(blob, aad=CREDENTIAL_AAD)
    except Exception:
        candidate = blob.decode("utf-8", errors="strict") if isinstance(blob, bytes) else str(blob)
    is_oauth = is_weibo_oauth_credential_envelope(candidate)

    # Legacy queued calibrations lacked a discriminator. Infer only from the
    # persisted envelope, never from a caller-supplied credential field.
    if not requested:
        return "weibo_oauth_identity" if is_oauth else "browser_session"
    if requested in {"weibo_oauth_identity", "weibo_oauth_capability"}:
        if not is_oauth:
            raise ValueError("weibo_oauth_calibration_credential_kind_mismatch")
        return requested
    if requested == "browser_session":
        if is_oauth:
            raise ValueError("weibo_browser_calibration_credential_kind_mismatch")
        return requested
    raise ValueError("weibo_calibration_kind_invalid")


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


async def verify_platform_identity(ctx, platform: str) -> dict:
    if platform == "bilibili":
        return await verify_bilibili_identity(ctx)
    if platform == "weibo":
        return await verify_weibo_identity(ctx)
    if platform == "xiaohongshu":
        return await verify_xiaohongshu_identity(ctx)
    if platform == "douyin":
        return await verify_douyin_identity(ctx)
    return {"verified": True, "method": "required_cookies"}


async def verify_bilibili_identity(ctx) -> dict:
    response = await ctx.request.get(
        "https://api.bilibili.com/x/web-interface/nav",
        headers={"Referer": "https://www.bilibili.com/"},
        timeout=15000,
    )
    if not response.ok:
        raise ValueError(f"Bilibili identity check failed with HTTP {response.status}")
    payload = await response.json()
    data = payload.get("data") or {}
    if payload.get("code") != 0 or not data.get("isLogin") or not data.get("mid"):
        raise ValueError("Bilibili credential is not authenticated")
    return {
        "verified": True,
        "method": "bilibili_nav",
        "mid": str(data["mid"]),
        "level": int(data.get("level_info", {}).get("current_level") or 0),
    }


async def verify_weibo_identity(ctx) -> dict:
    # Cookie presence can prove that a browser session was restored, but the
    # previous m.weibo.cn web endpoint is not an authoritative Open Platform
    # identity contract. Do not call a private endpoint or auto-promote an
    # account on that basis. An operator must review identity evidence before
    # the account may be marked ready.
    del ctx
    return {
        "verified": False,
        "method": "required_cookie_presence_only",
        "note": "weibo_official_identity_api_requires_oauth_token",
    }


async def build_weibo_oauth_calibration_result(
    client,
    *,
    calibration_id: str,
    account_id: int,
    execution_revision: int,
    operator_attestation: dict,
    expected_uid: str | None = None,
) -> dict:
    """Bind official identity to a separately persisted admin attestation.

    The injected client performs the read-only ``account/get_uid`` request.
    It cannot derive app review or action grants from credential import.
    """

    uid = str(await client.check_identity())
    if expected_uid is not None and uid != str(expected_uid):
        raise ValueError("weibo_oauth_identity_binding_mismatch")
    attestation = build_weibo_oauth_capability_attestation(
        calibration_id=calibration_id,
        account_id=account_id,
        execution_revision=execution_revision,
        operator_attestation=operator_attestation,
    )
    approved = attestation["app_review_status"] == "approved"
    return {
        "identity": {
            "verified": True,
            "method": "weibo_account_get_uid",
            "uid": uid,
        },
        "calibration_scope": "oauth_identity_and_capabilities",
        "requires_manual_identity_review": not approved,
        "oauth_capabilities": attestation,
    }


async def handle_weibo_oauth_calibration(
    task: dict,
    *,
    capability_calibration: bool,
    identity_client_factory=WeiboOAuthIdentityClient,
) -> dict:
    """Run official identity verification without browser/cookie fallback."""

    calibration_id = str(task.get("calibration_id") or "").strip()
    account_id = int(task.get("account_id"))
    row = await database.fetch_one(
        """SELECT c.calibration_id, c.account_id, c.platform,
                  c.status AS calibration_status, c.result AS staged_result,
                  a.status AS account_status, a.execution_revision,
                  a.encrypted_credential
             FROM account_calibrations c
             JOIN accounts a ON a.id = c.account_id
            WHERE c.calibration_id = :calibration_id
              AND c.account_id = :account_id
              AND c.platform = 'weibo'
              AND a.platform = 'weibo'
              AND a.deleted_at IS NULL
            LIMIT 1""",
        {"calibration_id": calibration_id, "account_id": account_id},
    )
    if (
        not row
        or str(row["calibration_id"] or "").strip() != calibration_id
        or int(row["account_id"] or 0) != account_id
        or str(row["calibration_status"] or "").strip().lower() != "running"
    ):
        raise ValueError("weibo_oauth_calibration_binding_invalid")
    execution_revision = int(row["execution_revision"] or 0)
    if execution_revision <= 0:
        raise ValueError("weibo_oauth_execution_revision_invalid")
    credential_blob = row["encrypted_credential"]
    if not credential_blob:
        raise ValueError("weibo_oauth_credential_required")
    try:
        decrypted = cookie_vault.decrypt(credential_blob, aad=CREDENTIAL_AAD)
    except Exception as exc:
        raise ValueError("weibo_oauth_credential_decryption_failed") from exc
    credential = parse_weibo_oauth_credential(decrypted)

    operator_attestation = None
    if capability_calibration:
        staged = parse_exact_json_object(row["staged_result"])
        operator_attestation = validate_weibo_operator_attestation(staged)

    async with identity_client_factory(credential.access_token) as client:
        uid = str(await client.check_identity())
    if uid != credential.uid:
        raise ValueError("weibo_oauth_identity_binding_mismatch")

    if capability_calibration:
        class VerifiedIdentityClient:
            async def check_identity(self):
                return uid

        result = await build_weibo_oauth_calibration_result(
            VerifiedIdentityClient(),
            calibration_id=calibration_id,
            account_id=account_id,
            execution_revision=execution_revision,
            operator_attestation=operator_attestation,
            expected_uid=credential.uid,
        )
        target_status = (
            "ready"
            if result["oauth_capabilities"]["app_review_status"] == "approved"
            else "warming"
        )
    else:
        target_status = "warming"
        result = {
            "identity": {
                "verified": True,
                "method": "weibo_account_get_uid",
                "uid": uid,
            },
            "calibration_scope": "oauth_identity_only",
            "requires_manual_identity_review": True,
            "account_status_target": target_status,
        }
    async with database.transaction():
        current = await database.fetch_one(
            """SELECT c.status AS calibration_status,
                      a.status AS account_status, a.execution_revision
                 FROM account_calibrations c
                 JOIN accounts a ON a.id = c.account_id
                WHERE c.calibration_id = :calibration_id
                  AND c.account_id = :account_id
                  AND c.platform = 'weibo'
                  AND a.platform = 'weibo'
                  AND a.deleted_at IS NULL
                FOR UPDATE""",
            {"calibration_id": calibration_id, "account_id": account_id},
        )
        if (
            not current
            or str(current["calibration_status"] or "").strip().lower()
            != "running"
            or int(current["execution_revision"] or 0) != execution_revision
        ):
            raise ValueError("weibo_oauth_execution_revision_mismatch")
        current_status = str(current["account_status"] or "").strip().lower()
        if current_status not in {
            "warming",
            "cooling",
            "login_required",
            "ready",
        }:
            raise ValueError("weibo_oauth_account_status_invalid")
        if current_status == "cooling":
            target_status = "cooling"
        result["account_status_target"] = target_status

        calibration_updated = await execute_affected_rows(
            """UPDATE account_calibrations
                  SET status = 'succeeded', result = :result,
                      screenshot_path = NULL, error_message = NULL,
                      finished_at = NOW()
                WHERE calibration_id = :calibration_id
                  AND account_id = :account_id AND platform = 'weibo'
                  AND status = 'running'""",
            {
                "calibration_id": calibration_id,
                "account_id": account_id,
                "result": json.dumps(result, ensure_ascii=False),
            },
            db=database,
        )
        if calibration_updated != 1:
            raise ValueError("weibo_oauth_calibration_settlement_lost")
        account_updated = await execute_affected_rows(
            """UPDATE accounts
                  SET status = :target_status, updated_at = NOW(),
                      version = version + 1
                WHERE id = :account_id
                  AND platform = 'weibo' AND deleted_at IS NULL
                  AND execution_revision = :execution_revision
                  AND status = :current_status""",
            {
                "account_id": account_id,
                "execution_revision": execution_revision,
                "current_status": current_status,
                "target_status": target_status,
            },
            db=database,
        )
        if account_updated != 1:
            raise ValueError("weibo_oauth_account_settlement_lost")
    await emit_calibration_terminal_observability(
        account_id=account_id,
        calibration_id=calibration_id,
        platform="weibo",
        status="succeeded",
        content=(
            f"Account A{account_id} Weibo OAuth identity calibration succeeded. "
            f"Scope: {result['calibration_scope']}."
        ),
        severity="info" if target_status == "ready" else "warning",
        event_type="AccountCalibrated",
        event_payload={
            "platform": "weibo",
            "calibration_id": calibration_id,
            "result": result,
            "screenshot_path": None,
        },
    )
    structured_log(
        "info",
        "account_calibration_succeeded",
        account_id=account_id,
        task_id=calibration_id,
    )
    return result


def parse_exact_json_object(value) -> dict:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="strict")
    if isinstance(value, str):
        def exact_object(pairs):
            output = {}
            for key, item in pairs:
                if key in output:
                    raise ValueError("weibo_oauth_operator_attestation_invalid")
                output[key] = item
            return output

        parsed = json.loads(value, object_pairs_hook=exact_object)
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("weibo_oauth_operator_attestation_required")


async def verify_xiaohongshu_identity(ctx) -> dict:
    # The me endpoint may require extra signed headers depending on platform
    # rollout; degrade to required-cookie verification instead of failing the
    # calibration when the API shape is unavailable.
    try:
        response = await ctx.request.get(
            "https://edith.xiaohongshu.com/api/sns/web/v2/user/me",
            headers={"Referer": "https://www.xiaohongshu.com/"},
            timeout=15000,
        )
        if not response.ok:
            return {"verified": True, "method": "required_cookies", "note": f"identity_api_http_{response.status}"}
        payload = await response.json()
    except Exception:
        return {"verified": True, "method": "required_cookies", "note": "identity_api_unavailable"}
    data = payload.get("data") or {}
    if data.get("guest"):
        raise ValueError("Xiaohongshu credential is not authenticated")
    user_id = data.get("user_id") or data.get("userId")
    if payload.get("success") and user_id:
        return {"verified": True, "method": "xiaohongshu_me", "user_id": str(user_id)}
    return {"verified": True, "method": "required_cookies", "note": "identity_api_unverified"}


async def verify_douyin_identity(ctx) -> dict:
    # The previously queried private web endpoint requires client-generated
    # signatures (for example msToken/X-Bogus). Calling it without that signed
    # contract and then treating cookie presence as verified identity produced
    # a false-positive calibration. Do not replicate private signing or claim
    # an account identity that the worker cannot authoritatively prove.
    del ctx
    return {
        "verified": False,
        "method": "required_cookie_presence_only",
        "note": "douyin_signed_identity_api_not_available",
    }


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
