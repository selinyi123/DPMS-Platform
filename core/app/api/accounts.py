import uuid
import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from app.db import database, execute_affected_rows, redis
from app.event_store.service import record_event
from app.adapter_config import load_runtime_selector_config, selector_config_complete
from app.models.schemas import AccountCalibrationRequest, AccountCreate, AccountCredentialUpdate, AccountHealthRecheckRequest, AccountProxyUpdate, AccountUpdateStatus, QRLoginStart
from app.platforms import get_platform, get_platforms
from app.services.bilibili_qr import generate_bilibili_qr, poll_bilibili_qr
from app.services.risk_engine import check_all_accounts_health
from app.security import audit_event, require_confirmation, require_min_role
from app.services.state_machine import transition_account
from app.utils.cookies import parse_cookie_payload, validate_required_cookies
from app.utils.crypto import CREDENTIAL_AAD, cookie_vault


PROFILES_DIR = Path("/profiles")
CALIBRATION_DIR = PROFILES_DIR / "account-calibrations"
router = APIRouter()


async def lock_account_for_execution_contract_mutation(account_id: int):
    """Fence credential/proxy identity changes against every active operation.

    Operation acquisition locks the same account row before inserting its
    append-only lease.  Taking that lock first here makes the active-lease
    check and the subsequent revision bump one serializable decision.
    """

    row = await database.fetch_one(
        """SELECT id, platform, status, proxy_id, deleted_at
           FROM accounts
           WHERE id = :id
           FOR UPDATE""",
        {"id": account_id},
    )
    if not row:
        raise HTTPException(404, detail="Account not found")
    active_lease = await database.fetch_one(
        """SELECT lease_id
           FROM account_operation_leases
           WHERE account_id = :account_id
             AND released_at IS NULL
             AND expires_at > NOW()
           ORDER BY generation DESC
           LIMIT 1
           FOR UPDATE""",
        {"account_id": account_id},
    )
    if active_lease:
        raise HTTPException(
            409,
            detail={
                "message": "Account has an active operation lease",
                "code": "account_operation_lease_active",
                "account_id": account_id,
            },
        )
    return row


async def execute_locked_account_update(query: str, values: dict) -> int:
    await database.execute(query, values)
    affected = await database.fetch_one("SELECT ROW_COUNT() AS affected")
    if affected is None:
        raise RuntimeError("database_affected_row_count_unavailable")
    return int(affected["affected"] or 0)


@router.get("/platforms")
async def list_platforms():
    selector_config = await load_runtime_selector_config()
    return [
        {
            "id": key,
            "label": value["label"],
            "qr_login": value.get("qr_login", False),
            "cookie_login": value.get("cookie_login", True),
            "action_adapter": value.get("action_adapter", False) or platform_selectors_complete(selector_config, key),
            "adapter_status": "configured" if platform_selectors_complete(selector_config, key) else value.get("adapter_status", "planned"),
            "cookie_domain": value.get("cookie_domain"),
            "account_check_url": value.get("account_check_url"),
        }
        for key, value in get_platforms().items()
    ]


@router.get("/")
async def list_accounts():
    rows = await database.fetch_all(
        """SELECT a.*,
                  (
                    SELECT JSON_OBJECT(
                      'id', r.id,
                      'event_type', r.event_type,
                      'detail', r.detail,
                      'created_at', r.created_at
                    )
                    FROM risk_events r
                    WHERE r.account_id = a.id
                    ORDER BY r.created_at DESC
                    LIMIT 1
                  ) AS latest_risk_event
                  ,(
                    SELECT JSON_OBJECT(
                      'calibration_id', c.calibration_id,
                      'status', c.status,
                      'result', c.result,
                      'error_message', c.error_message,
                      'screenshot_path', c.screenshot_path,
                      'created_at', c.created_at,
                      'finished_at', c.finished_at
                    )
                    FROM account_calibrations c
                    WHERE c.account_id = a.id
                    ORDER BY c.created_at DESC
                    LIMIT 1
                  ) AS latest_calibration
                  ,(
                    SELECT JSON_OBJECT(
                      'task_id', tr.task_id,
                      'lottery_id', tr.lottery_id,
                      'status', tr.status,
                      'dry_run', tr.dry_run,
                      'task_mode', tr.task_mode,
                      'started_at', tr.started_at,
                      'finished_at', tr.finished_at,
                      'error_message', tr.error_message
                    )
                    FROM task_runs tr
                    WHERE tr.account_id = a.id
                      AND tr.status IN ('queued', 'running')
                    ORDER BY tr.id DESC
                    LIMIT 1
                  ) AS current_task_run
                  ,(
                    SELECT JSON_OBJECT(
                      'task_id', tr.task_id,
                      'lottery_id', tr.lottery_id,
                      'status', tr.status,
                      'dry_run', tr.dry_run,
                      'task_mode', tr.task_mode,
                      'started_at', tr.started_at,
                      'finished_at', tr.finished_at,
                      'error_message', tr.error_message
                    )
                    FROM task_runs tr
                    WHERE tr.account_id = a.id
                    ORDER BY tr.id DESC
                    LIMIT 1
                  ) AS latest_task_run
           FROM accounts a
           WHERE a.deleted_at IS NULL
           ORDER BY a.id DESC"""
    )
    result = []
    for row in rows:
        item = dict(row)
        credential = item.pop("encrypted_credential", None)
        item["credential_ready"] = bool(credential)
        if item.get("latest_risk_event"):
            item["latest_risk_event"] = parse_json_field(item["latest_risk_event"])
        if item.get("latest_calibration"):
            item["latest_calibration"] = parse_json_field(item["latest_calibration"])
        if item.get("current_task_run"):
            item["current_task_run"] = parse_json_field(item["current_task_run"])
        if item.get("latest_task_run"):
            item["latest_task_run"] = parse_json_field(item["latest_task_run"])
        result.append(item)
    return result


@router.post("/")
async def create_account(data: AccountCreate, request: Request):
    actor = require_min_role(request, "operator")
    if not get_platform(data.platform):
        raise HTTPException(400, detail=f"Unsupported platform: {data.platform}")
    if data.proxy_id is not None:
        await validate_proxy_assignment(data.proxy_id)
    fingerprint_id = data.fingerprint_id or await ensure_default_fingerprint(data.platform)
    credential = b""
    status = "login_required"
    if data.encrypted_credential:
        try:
            normalized = normalize_and_validate_credential(data.platform, data.encrypted_credential)
        except Exception as e:
            raise HTTPException(400, detail=str(e))
        credential = cookie_vault.encrypt(normalized, aad=CREDENTIAL_AAD)
        status = "warming"

    account_id = await database.execute(
        """INSERT INTO accounts (platform, fingerprint_id, proxy_id, encrypted_credential, status)
           VALUES (:platform, :fid, :pid, :cred, :status)""",
        {
            "platform": data.platform,
            "fid": fingerprint_id,
            "pid": data.proxy_id,
            "cred": credential,
            "status": status,
        },
    )
    calibration = None
    if credential:
        calibration = await queue_account_calibration(account_id, data.platform)
    await audit_event(
        request,
        action="account.create",
        resource_type="account",
        resource_id=account_id,
        result="created",
        risk_level="high" if credential else "medium",
        detail={"platform": data.platform, "credential_imported": bool(credential), "calibration": calibration},
    )
    await record_event(
        aggregate="account",
        aggregate_id=account_id,
        event_type="AccountCreated",
        payload={
            "platform": data.platform,
            "status": status,
            "credential_imported": bool(credential),
            "calibration": calibration,
        },
        correlation_id=calibration.get("calibration_id") if calibration else None,
        actor_type="operator",
        actor_id=actor["actor_id"],
    )
    if credential:
        await record_event(
            aggregate="account",
            aggregate_id=account_id,
            event_type="AccountCredentialImported",
            payload={"platform": data.platform, "calibration": calibration},
            correlation_id=calibration.get("calibration_id") if calibration else None,
            actor_type="operator",
            actor_id=actor["actor_id"],
        )
    return {"status": "created", "id": account_id, "calibration": calibration}


@router.post("/login/qr")
async def start_qr_login(data: QRLoginStart, request: Request):
    actor = require_min_role(request, "operator")
    platform_cfg = get_platform(data.platform)
    if not platform_cfg:
        raise HTTPException(400, detail=f"Unsupported platform: {data.platform}")
    if not platform_cfg.get("qr_login"):
        raise HTTPException(400, detail=f"QR login is not supported for platform: {data.platform}")
    session_id = str(uuid.uuid4())
    image_path = f"/profiles/login-sessions/{session_id}.png"

    if data.platform == "bilibili":
        try:
            qr_content, provider_key = await generate_bilibili_qr()
        except Exception as exc:
            raise HTTPException(502, detail="Bilibili QR service is unavailable") from exc
        await database.execute(
            """INSERT INTO login_sessions
               (session_id, platform, status, login_url, provider_key, qr_image_path, expires_at)
               VALUES (:session_id, :platform, 'waiting_scan', :login_url, :provider_key, NULL, DATE_ADD(NOW(), INTERVAL 3 MINUTE))""",
            {
                "session_id": session_id,
                "platform": data.platform,
                "login_url": qr_content,
                "provider_key": provider_key,
            },
        )
        await audit_event(
            request,
            action="account.login_qr.start",
            resource_type="login_session",
            resource_id=session_id,
            result="waiting_scan",
            risk_level="medium",
            detail={"platform": data.platform, "login_mode": "official_qr"},
        )
        await record_event(
            aggregate="browser",
            aggregate_id=session_id,
            event_type="QrLoginWaitingScan",
            payload={"platform": data.platform, "login_mode": "official_qr"},
            correlation_id=session_id,
            actor_type="operator",
            actor_id=actor["actor_id"],
        )
        return {
            "session_id": session_id,
            "status": "waiting_scan",
            "login_mode": "official_qr",
            "qr_content": qr_content,
        }

    await database.execute(
        """INSERT INTO login_sessions (session_id, platform, status, login_url, qr_image_path, expires_at)
           VALUES (:session_id, :platform, 'queued', :login_url, :image_path, DATE_ADD(NOW(), INTERVAL 5 MINUTE))""",
        {
            "session_id": session_id,
            "platform": data.platform,
            "login_url": platform_cfg["login_url"],
            "image_path": image_path,
        },
    )
    await redis.xadd(
        "login_requests",
        {"session_id": session_id, "platform": data.platform, "login_url": platform_cfg["login_url"]},
    )
    await audit_event(
        request,
        action="account.login_qr.start",
        resource_type="login_session",
        resource_id=session_id,
        result="queued",
        risk_level="medium",
        detail={"platform": data.platform},
    )
    await record_event(
        aggregate="browser",
        aggregate_id=session_id,
        event_type="QrLoginRequested",
        payload={"platform": data.platform, "login_url": platform_cfg["login_url"]},
        correlation_id=session_id,
        actor_type="operator",
        actor_id=actor["actor_id"],
    )
    return {"session_id": session_id, "status": "queued", "login_mode": "browser"}


@router.get("/login/qr/{session_id}")
async def get_qr_login_status(session_id: str):
    row = await database.fetch_one("SELECT * FROM login_sessions WHERE session_id = :sid", {"sid": session_id})
    if not row:
        raise HTTPException(404, detail="Login session not found")
    return serialize_login_session(row)


@router.post("/login/qr/{session_id}/poll")
async def poll_qr_login_status(session_id: str, request: Request):
    actor = require_min_role(request, "operator")
    await database.execute(
        """UPDATE login_sessions
           SET status = 'expired', error_message = 'QR login session expired', updated_at = NOW()
           WHERE session_id = :sid
             AND expires_at IS NOT NULL
             AND expires_at <= NOW()
             AND status NOT IN ('confirmed','expired','failed')""",
        {"sid": session_id},
    )
    row = await database.fetch_one("SELECT * FROM login_sessions WHERE session_id = :sid", {"sid": session_id})
    if not row:
        raise HTTPException(404, detail="Login session not found")
    if row["platform"] != "bilibili" or not row["provider_key"]:
        return serialize_login_session(row)
    if row["status"] in {"confirmed", "expired", "failed"}:
        return serialize_login_session(row)

    try:
        result = await poll_bilibili_qr(row["provider_key"])
    except Exception as exc:
        await database.execute(
            """UPDATE login_sessions
               SET error_message = :error, updated_at = NOW()
               WHERE session_id = :session_id""",
            {"session_id": session_id, "error": f"Temporary QR status check error: {exc}"},
        )
        current = await database.fetch_one("SELECT * FROM login_sessions WHERE session_id = :sid", {"sid": session_id})
        return serialize_login_session(current)

    if result.status != "confirmed":
        await database.execute(
            """UPDATE login_sessions
               SET status = :status, error_message = NULL, updated_at = NOW()
               WHERE session_id = :session_id AND status NOT IN ('confirmed','expired','failed')""",
            {"session_id": session_id, "status": result.status},
        )
        if result.status == "expired":
            await record_event(
                aggregate="browser",
                aggregate_id=session_id,
                event_type="QrLoginExpired",
                payload={"platform": "bilibili", "login_mode": "official_qr"},
                correlation_id=session_id,
                actor_type="operator",
                actor_id=actor["actor_id"],
            )
        updated = await database.fetch_one("SELECT * FROM login_sessions WHERE session_id = :sid", {"sid": session_id})
        return serialize_login_session(updated)

    account_id = await finalize_bilibili_qr_login(session_id, result.cookies or [])
    updated = await database.fetch_one("SELECT * FROM login_sessions WHERE session_id = :sid", {"sid": session_id})
    await record_event(
        aggregate="browser",
        aggregate_id=session_id,
        event_type="QrLoginCompleted",
        payload={"platform": "bilibili", "account_id": account_id, "login_mode": "official_qr"},
        correlation_id=session_id,
        actor_type="operator",
        actor_id=actor["actor_id"],
    )
    return serialize_login_session(updated)


@router.get("/login/qr/{session_id}/image")
async def get_qr_login_image(session_id: str):
    row = await database.fetch_one("SELECT qr_image_path FROM login_sessions WHERE session_id = :sid", {"sid": session_id})
    if not row or not row["qr_image_path"]:
        raise HTTPException(404, detail="QR image not ready")

    path = Path(row["qr_image_path"]).resolve()
    if not path.exists() or not path.is_file() or not path.is_relative_to(PROFILES_DIR):
        raise HTTPException(404, detail="QR image not ready")
    return FileResponse(path, media_type="image/png")


@router.put("/{account_id}/credential")
async def update_credential(account_id: int, data: AccountCredentialUpdate, request: Request):
    actor = require_min_role(request, "operator")
    row = await database.fetch_one(
        "SELECT platform, status FROM accounts WHERE id = :id",
        {"id": account_id},
    )
    if not row:
        raise HTTPException(404, detail="Account not found")
    if row["status"] == "executing":
        raise HTTPException(409, detail="Cannot replace a credential while the account is executing")
    if row["status"] == "banned":
        raise HTTPException(409, detail="Cannot replace a credential on a banned account")

    try:
        normalized = normalize_and_validate_credential(row["platform"], data.encrypted_credential)
    except Exception as e:
        raise HTTPException(400, detail=str(e))

    encrypted_credential = cookie_vault.encrypt(normalized, aad=CREDENTIAL_AAD)
    async with database.transaction():
        locked = await lock_account_for_execution_contract_mutation(account_id)
        if locked["deleted_at"]:
            raise HTTPException(409, detail="Cannot replace a credential on a deleted account")
        if locked["status"] in {"executing", "banned"}:
            raise HTTPException(
                409, detail=f"Cannot replace a credential while account is {locked['status']}"
            )
        if str(locked["platform"]) != str(row["platform"]):
            raise HTTPException(409, detail="Account platform changed; retry credential update")
        updated = await execute_locked_account_update(
            """UPDATE accounts
               SET encrypted_credential = :credential, status = 'warming', updated_at = NOW(),
                   version = version + 1, execution_revision = execution_revision + 1
               WHERE id = :id AND status NOT IN ('executing', 'banned')
                 AND deleted_at IS NULL
                 AND NOT EXISTS (
                   SELECT 1 FROM account_operation_leases lease
                   WHERE lease.account_id = accounts.id
                     AND lease.released_at IS NULL
                     AND lease.expires_at > NOW()
                 )""",
            {"id": account_id, "credential": encrypted_credential},
        )
    if int(updated or 0) != 1:
        raise HTTPException(409, detail="Account changed; credential update was not applied")
    calibration = await queue_account_calibration(account_id, row["platform"])
    await audit_event(
        request,
        action="account.credential.update",
        resource_type="account",
        resource_id=account_id,
        result="updated",
        risk_level="high",
        detail={"platform": row["platform"], "calibration": calibration},
    )
    await record_event(
        aggregate="account",
        aggregate_id=account_id,
        event_type="AccountCredentialImported",
        payload={"platform": row["platform"], "calibration": calibration},
        correlation_id=calibration.get("calibration_id") if calibration else None,
        actor_type="operator",
        actor_id=actor["actor_id"],
    )
    return {"status": "credential_updated", "calibration": calibration}


@router.put("/{account_id}/status")
async def change_status(account_id: int, data: AccountUpdateStatus, request: Request):
    actor = require_min_role(request, "operator")
    try:
        row = await database.fetch_one(
            """SELECT status,
                      OCTET_LENGTH(encrypted_credential) AS credential_size,
                      (
                        SELECT c.status FROM account_calibrations c
                        WHERE c.account_id = accounts.id
                        ORDER BY c.created_at DESC
                        LIMIT 1
                      ) AS latest_calibration_status
               FROM accounts WHERE id = :id""",
            {"id": account_id},
        )
        if not row:
            raise HTTPException(404, detail="Account not found")
        if data.target == "ready" and row["credential_size"] == 0:
            raise HTTPException(400, detail="Cannot mark an account ready before importing a credential")
        if data.target == "ready" and row["latest_calibration_status"] != "succeeded":
            raise HTTPException(400, detail="Cannot mark an account ready before successful login calibration")
        await transition_account(account_id, data.version, data.target)
        await record_event(
            aggregate="account",
            aggregate_id=account_id,
            event_type="AccountStatusChanged",
            payload={"from": row["status"], "to": data.target, "version": data.version},
            actor_type="operator",
            actor_id=actor["actor_id"],
        )
        return {"status": "ok"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(409, detail=str(e))


@router.put("/{account_id}/proxy")
async def update_account_proxy(account_id: int, data: AccountProxyUpdate, request: Request):
    actor = require_min_role(request, "operator")
    async with database.transaction():
        row = await lock_account_for_execution_contract_mutation(account_id)
        if row["deleted_at"]:
            raise HTTPException(409, detail="Cannot change proxy on a deleted account")
        if row["status"] == "executing":
            raise HTTPException(409, detail="Cannot change proxy while account is executing")
        if row["proxy_id"] == data.proxy_id:
            return {
                "status": "proxy_unassigned" if data.proxy_id is None else "proxy_assigned",
                "id": account_id,
                "proxy_id": data.proxy_id,
                "changed": False,
            }
        if data.proxy_id is not None:
            await validate_proxy_assignment(data.proxy_id, account_id)
        updated = await execute_locked_account_update(
            """UPDATE accounts
               SET proxy_id = :proxy_id, updated_at = NOW(), version = version + 1,
                   execution_revision = execution_revision + 1
               WHERE id = :id AND status <> 'executing' AND deleted_at IS NULL
                 AND NOT EXISTS (
                   SELECT 1 FROM account_operation_leases lease
                   WHERE lease.account_id = accounts.id
                     AND lease.released_at IS NULL
                     AND lease.expires_at > NOW()
                 )""",
            {"id": account_id, "proxy_id": data.proxy_id},
        )
        if int(updated or 0) != 1:
            raise HTTPException(409, detail="Account changed; proxy update was not applied")

    if data.proxy_id is None:
        await record_event(
            aggregate="account",
            aggregate_id=account_id,
            event_type="AccountProxyUnassigned",
            payload={"previous_status": row["status"]},
            actor_type="operator",
            actor_id=actor["actor_id"],
        )
        return {"status": "proxy_unassigned", "id": account_id, "proxy_id": None}

    await record_event(
        aggregate="account",
        aggregate_id=account_id,
        event_type="AccountProxyAssigned",
        payload={"proxy_id": data.proxy_id, "previous_status": row["status"]},
        actor_type="operator",
        actor_id=actor["actor_id"],
    )
    return {"status": "proxy_assigned", "id": account_id, "proxy_id": data.proxy_id}


@router.delete("/{account_id}")
async def delete_account(account_id: int, request: Request):
    actor = require_min_role(request, "admin")
    require_confirmation(request)
    async with database.transaction():
        row = await lock_account_for_execution_contract_mutation(account_id)
        if row["deleted_at"]:
            return {"status": "already_deleted", "id": account_id}
        if row["status"] == "executing":
            raise HTTPException(409, detail="Cannot delete an account while it is executing")
        updated = await execute_locked_account_update(
            """UPDATE accounts
               SET status = 'frozen', encrypted_credential = '', proxy_id = NULL,
                   deleted_at = NOW(), deleted_by = :deleted_by,
                   delete_reason = 'operator soft delete', updated_at = NOW(), version = version + 1,
                   execution_revision = execution_revision + 1
               WHERE id = :id AND status <> 'executing' AND deleted_at IS NULL
                 AND NOT EXISTS (
                   SELECT 1 FROM account_operation_leases lease
                   WHERE lease.account_id = accounts.id
                     AND lease.released_at IS NULL
                     AND lease.expires_at > NOW()
                 )""",
            {"id": account_id, "deleted_by": actor["actor_id"]},
        )
        if int(updated or 0) != 1:
            raise HTTPException(409, detail="Account changed; delete was not applied")
        await database.execute(
            "UPDATE login_sessions SET account_id = NULL WHERE account_id = :id",
            {"id": account_id},
        )
    await audit_event(
        request,
        action="account.delete",
        resource_type="account",
        resource_id=account_id,
        result="soft_deleted",
        risk_level="high",
        detail={"previous_status": row["status"], "credential_removed": True},
    )
    await record_event(
        aggregate="account",
        aggregate_id=account_id,
        event_type="AccountSoftDeleted",
        payload={"previous_status": row["status"], "credential_removed": True},
        actor_type="operator",
        actor_id=actor["actor_id"],
    )
    return {"status": "soft_deleted", "id": account_id}


@router.post("/health/recheck")
async def recheck_account_health(data: AccountHealthRecheckRequest):
    return await check_all_accounts_health(
        cooldown_minutes=data.cooldown_minutes,
        stale_execution_minutes=data.stale_execution_minutes,
    )


@router.get("/calibrations")
async def list_account_calibrations(limit: int = 50):
    rows = await database.fetch_all(
        """SELECT c.*, a.status AS account_status
           FROM account_calibrations c
           JOIN accounts a ON a.id = c.account_id
           ORDER BY c.created_at DESC
           LIMIT :limit""",
        {"limit": max(1, min(limit, 200))},
    )
    result = []
    for row in rows:
        item = dict(row)
        item["result"] = parse_json_field(item.get("result"))
        result.append(item)
    return result


@router.post("/{account_id}/calibrate")
async def calibrate_account(account_id: int, data: AccountCalibrationRequest, request: Request):
    actor = require_min_role(request, "operator")
    row = await database.fetch_one(
        "SELECT platform, status, OCTET_LENGTH(encrypted_credential) AS credential_size FROM accounts WHERE id = :id",
        {"id": account_id},
    )
    if not row:
        raise HTTPException(404, detail="Account not found")
    if row["credential_size"] == 0:
        raise HTTPException(400, detail="Cannot calibrate an account before importing a credential")
    if row["status"] == "executing":
        raise HTTPException(409, detail="Cannot calibrate an account while it is executing")
    if row["status"] == "banned":
        if not data.force:
            raise HTTPException(409, detail="Cannot calibrate a banned account without an admin override")
        require_min_role(request, "admin")
        require_confirmation(request)
    updated = await execute_affected_rows(
        """UPDATE accounts
           SET status = 'warming', updated_at = NOW(), version = version + 1
           WHERE id = :id AND status <> 'executing'""",
        {"id": account_id},
        db=database,
    )
    if int(updated or 0) != 1:
        raise HTTPException(409, detail="Account started executing; calibration was not queued")
    calibration = await queue_account_calibration(account_id, row["platform"])
    await record_event(
        aggregate="account",
        aggregate_id=account_id,
        event_type="AccountCalibrationQueued",
        payload={"platform": row["platform"], "force": data.force, "calibration": calibration},
        correlation_id=calibration.get("calibration_id") if calibration else None,
        actor_type="operator",
        actor_id=actor["actor_id"],
    )
    return {"status": "queued", "calibration": calibration}


@router.get("/calibrations/{calibration_id}/screenshot")
async def get_account_calibration_screenshot(calibration_id: str):
    row = await database.fetch_one(
        "SELECT screenshot_path FROM account_calibrations WHERE calibration_id = :calibration_id",
        {"calibration_id": calibration_id},
    )
    if not row or not row["screenshot_path"]:
        raise HTTPException(404, detail="Calibration screenshot not ready")
    path = Path(row["screenshot_path"]).resolve()
    if not path.exists() or not path.is_file() or not path.is_relative_to(CALIBRATION_DIR):
        raise HTTPException(404, detail="Calibration screenshot not ready")
    return FileResponse(path, media_type="image/png")


async def queue_account_calibration(account_id: int, platform: str):
    platform_cfg = get_platform(platform)
    if not platform_cfg:
        raise HTTPException(400, detail=f"Unsupported platform: {platform}")
    calibration_id = str(uuid.uuid4())
    check_url = platform_cfg.get("account_check_url") or platform_cfg["login_url"]
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
    return {"calibration_id": calibration_id, "status": "queued", "check_url": check_url}


def parse_json_field(value):
    if isinstance(value, (dict, list)) or value is None:
        return value
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    try:
        return json.loads(value)
    except Exception:
        return value


def normalize_and_validate_credential(platform: str, payload: str) -> str:
    platform_cfg = get_platform(platform)
    if not platform_cfg:
        raise ValueError(f"Unsupported platform: {platform}")
    cookies = parse_cookie_payload(platform, payload)
    validate_required_cookies(cookies, platform_cfg.get("required_cookies", []))
    return json.dumps(cookies, ensure_ascii=False, separators=(",", ":"))


def serialize_login_session(row) -> dict:
    item = dict(row)
    official_qr = item["platform"] == "bilibili" and bool(item.get("provider_key"))
    item.pop("provider_key", None)
    item["login_mode"] = "official_qr" if official_qr else "browser"
    if item["login_mode"] == "official_qr":
        item["qr_content"] = item.pop("login_url")
        item["qr_image_path"] = None
    return item


async def finalize_bilibili_qr_login(session_id: str, cookies: list[dict]) -> int:
    validate_required_cookies(cookies, get_platform("bilibili").get("required_cookies", []))
    normalized = json.dumps(cookies, ensure_ascii=False, separators=(",", ":"))
    async with database.transaction():
        row = await database.fetch_one(
            "SELECT status, account_id FROM login_sessions WHERE session_id = :sid FOR UPDATE",
            {"sid": session_id},
        )
        if not row:
            raise HTTPException(404, detail="Login session not found")
        if row["status"] == "confirmed" and row["account_id"]:
            return int(row["account_id"])
        if row["status"] in {"expired", "failed"}:
            raise HTTPException(409, detail=f"Login session is {row['status']}")
        fingerprint_id = await ensure_default_fingerprint("bilibili")
        account_id = await database.execute(
            """INSERT INTO accounts (platform, fingerprint_id, encrypted_credential, status)
               VALUES ('bilibili', :fingerprint_id, :credential, 'warming')""",
            {"fingerprint_id": fingerprint_id, "credential": cookie_vault.encrypt(normalized, aad=CREDENTIAL_AAD)},
        )
        await database.execute(
            """UPDATE login_sessions
               SET status = 'confirmed', account_id = :account_id, completed_at = NOW(),
                   error_message = NULL, updated_at = NOW()
               WHERE session_id = :session_id""",
            {"session_id": session_id, "account_id": account_id},
        )
    calibration = None
    try:
        calibration = await queue_account_calibration(account_id, "bilibili")
    except Exception as exc:
        await database.execute(
            """UPDATE login_sessions
               SET error_message = :error, updated_at = NOW()
               WHERE session_id = :session_id""",
            {
                "session_id": session_id,
                "error": f"Account created, but calibration queue failed: {exc}",
            },
        )
        await record_event(
            aggregate="account",
            aggregate_id=account_id,
            event_type="AccountCalibrationQueueFailed",
            payload={"platform": "bilibili", "error": str(exc), "manual_retry_available": True},
            correlation_id=session_id,
        )
    await record_event(
        aggregate="account",
        aggregate_id=account_id,
        event_type="AccountCreated",
        payload={
            "platform": "bilibili",
            "credential_imported": True,
            "source": "official_qr",
            "calibration": calibration,
        },
        correlation_id=session_id,
    )
    await record_event(
        aggregate="account",
        aggregate_id=account_id,
        event_type="AccountCredentialImported",
        payload={"platform": "bilibili", "source": "official_qr", "calibration": calibration},
        correlation_id=session_id,
    )
    return account_id


# A small pool of realistic, stable desktop browser profiles (P2-1). The goal
# is asset-isolation *consistency*, not anti-detection: each account binds a
# stable fingerprint, and new accounts spread across the pool instead of all
# sharing one row. Every profile uses ordinary, current browser values and a
# zh-CN / Asia-Shanghai locale consistent with the supported platforms — nothing
# fabricated or anomalous. Each entry has a distinct user_agent because the
# fingerprints table is unique on (platform, user_agent).
DEFAULT_FINGERPRINT_POOL = [
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36", "vw": 1920, "vh": 1080},
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36", "vw": 1536, "vh": 864},
    {"ua": "Mozilla/5.0 (Windows NT 11.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36", "vw": 1366, "vh": 768},
    {"ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36", "vw": 1440, "vh": 900},
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36", "vw": 1280, "vh": 720},
]


async def ensure_default_fingerprint(platform: str) -> int:
    """Return a fingerprint id for a new account, balanced across the pool.

    Find-or-creates each pool profile for the platform, then picks the one
    currently bound to the fewest accounts, so accounts are distributed across
    distinct fingerprints rather than funnelled onto a single shared one.
    """
    if not get_platform(platform):
        raise HTTPException(400, detail=f"Unsupported platform: {platform}")

    profile_ids: list[int] = []
    for profile in DEFAULT_FINGERPRINT_POOL:
        row = await database.fetch_one(
            "SELECT id FROM fingerprints WHERE platform = :platform AND user_agent = :ua",
            {"platform": platform, "ua": profile["ua"]},
        )
        if row:
            profile_ids.append(row["id"])
            continue
        fingerprint_id = await database.execute(
            """INSERT INTO fingerprints (platform, user_agent, viewport_width, viewport_height, timezone, locale)
               VALUES (:platform, :ua, :vw, :vh, 'Asia/Shanghai', 'zh-CN')""",
            {"platform": platform, "ua": profile["ua"], "vw": profile["vw"], "vh": profile["vh"]},
        )
        profile_ids.append(fingerprint_id)

    placeholders = ", ".join(f":id{index}" for index in range(len(profile_ids)))
    usage_values = {f"id{index}": fid for index, fid in enumerate(profile_ids)}
    usage_rows = await database.fetch_all(
        f"SELECT fingerprint_id, COUNT(*) AS cnt FROM accounts WHERE fingerprint_id IN ({placeholders}) GROUP BY fingerprint_id",
        usage_values,
    )
    usage = {row["fingerprint_id"]: row["cnt"] for row in usage_rows}
    # Least-used first; ties keep pool order (deterministic).
    return min(profile_ids, key=lambda fid: usage.get(fid, 0))


async def validate_proxy_assignment(proxy_id: int, account_id: int | None = None):
    row = await database.fetch_one(
        """SELECT p.id, p.status,
                  (p.cooldown_until IS NOT NULL AND p.cooldown_until > NOW()) AS cooling_active,
                  a.id AS assigned_account_id
           FROM proxies p
           LEFT JOIN accounts a ON a.proxy_id = p.id
           WHERE p.id = :id""",
        {"id": proxy_id},
    )
    if not row:
        raise HTTPException(404, detail="Proxy not found")
    if row["assigned_account_id"] and row["assigned_account_id"] != account_id:
        raise HTTPException(409, detail=f"Proxy is already assigned to account {row['assigned_account_id']}")
    if row["status"] == "dead":
        raise HTTPException(400, detail="Cannot assign a dead proxy")
    if row["cooling_active"]:
        raise HTTPException(400, detail="Cannot assign a proxy that is in cooldown")


def platform_selectors_complete(selector_config: dict, platform: str) -> bool:
    configured = selector_config.get(platform, {})
    return selector_config_complete(platform, configured)
