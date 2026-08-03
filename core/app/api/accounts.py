import uuid
import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.db import database, execute_affected_rows, redis
from app.event_store.service import record_event
from app.action_plan import WEIBO_ACTION_ORDER
from app.adapter_config import (
    load_runtime_selector_config,
    platform_has_runtime_real_adapter,
    platform_real_adapter_kind,
)
from app.models.schemas import AccountCalibrationRequest, AccountCreate, AccountCredentialUpdate, AccountHealthRecheckRequest, AccountProxyUpdate, AccountUpdateStatus, QRLoginStart, WeiboOAuthCapabilityAttestationRequest
from app.platforms import get_platform, get_platforms
from app.services.risk_engine import check_all_accounts_health
from app.services.account_calibration_outbox import (
    build_account_calibration_message,
    enqueue_account_calibration_outbox,
)
from app.services.account_profile_cleanup import (
    enqueue_account_profile_cleanup,
    enqueue_login_profile_cleanup,
)
from app.services.login_request_outbox import (
    build_login_request_message,
    enqueue_login_request_outbox,
)
from app.security import audit_event, require_confirmation, require_min_role
from app.services.state_machine import transition_account
from app.utils.cookies import (
    parse_cookie_payload,
    validate_api_cookie_name_uniqueness,
    validate_required_cookies,
)
from app.utils.crypto import CREDENTIAL_AAD, cookie_vault
from app.utils.credential_kind import (
    account_credential_kind,
    account_remote_subject,
)
from app.utils.weibo_oauth_credential import (
    normalize_weibo_oauth_credential,
    parse_weibo_oauth_credential,
)
from app.utils.secure_files import (
    SecureFileError,
    open_bounded_regular_file_beneath_root,
)
from shared.platform_ids import PLATFORM_IDS
from shared.douyin_device_contract import (
    DOUYIN_DEVICE_CALIBRATION_CHECK_URL,
    normalize_douyin_device_credential,
)


PROFILES_DIR = Path("/profiles")
LEGACY_CALIBRATION_DIR = PROFILES_DIR / "account-calibrations"
ACCOUNT_DELETE_INTENT_CHECK_LIMIT = 256
PROFILE_IMAGE_MAX_BYTES = 32 * 1024 * 1024
MAX_SUPERSEDED_LOGIN_SESSIONS = 32
router = APIRouter()


def _profile_png_response(path_value, *, allowed_root: Path) -> StreamingResponse:
    try:
        snapshot = open_bounded_regular_file_beneath_root(
            allowed_root,
            Path(str(path_value or "")),
            max_bytes=PROFILE_IMAGE_MAX_BYTES,
        )
    except (OSError, SecureFileError, TypeError, ValueError) as exc:
        raise HTTPException(
            404,
            detail="Profile image not ready",
        ) from exc
    return StreamingResponse(
        snapshot.iter_chunks(),
        media_type="image/png",
        headers={
            "Cache-Control": "no-store",
            "Content-Length": str(snapshot.size),
            "X-Content-Type-Options": "nosniff",
        },
    )


async def supersede_active_login_sessions(
    *,
    replacement_session_id: str,
    actor_id: str,
) -> int:
    """Retire login work hidden by the UI's new single active session.

    The caller holds the outer transaction. Locking and retiring every prior
    non-terminal session before inserting its replacement keeps the database,
    Redis delivery, browser ownership, and the one-session UI model aligned.
    """

    rows = await database.fetch_all(
        f"""SELECT session_id, platform, status
            FROM login_sessions
            WHERE status IN ('queued', 'opening', 'waiting_scan')
            ORDER BY created_at ASC, id ASC
            LIMIT {MAX_SUPERSEDED_LOGIN_SESSIONS + 1}
            FOR UPDATE"""
    )
    if len(rows) > MAX_SUPERSEDED_LOGIN_SESSIONS:
        raise HTTPException(
            503,
            detail="Too many active login sessions; retry after cleanup",
        )

    for row in rows:
        await database.execute(
            """UPDATE login_sessions
               SET status = 'expired',
                   error_message = 'Superseded by a newer login request',
                   completed_at = NOW(),
                   updated_at = NOW()
               WHERE session_id = :session_id
                 AND status = :expected_status""",
            {
                "session_id": row["session_id"],
                "expected_status": row["status"],
            },
        )
        await enqueue_login_profile_cleanup(row["session_id"])
        await record_event(
            aggregate="browser",
            aggregate_id=row["session_id"],
            event_type="QrLoginSuperseded",
            payload={
                "platform": row["platform"],
                "replacement_session_id": replacement_session_id,
            },
            correlation_id=replacement_session_id,
            actor_type="operator",
            actor_id=actor_id,
        )
    return len(rows)


async def lock_account_for_execution_contract_mutation(account_id: int):
    """Fence credential/proxy identity changes against every active operation.

    Operation acquisition locks the same account row before inserting its
    append-only lease.  Taking that lock first here makes the active-lease
    check and the subsequent revision bump one serializable decision.
    """

    row = await database.fetch_one(
        """SELECT id, platform, status, proxy_id, encrypted_credential,
                  deleted_at
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


async def account_has_unsettled_real_action_state(account_id: int) -> bool:
    """Return whether an account still has in-flight or ambiguous real work."""

    row = await database.fetch_one(
        """SELECT EXISTS (
             SELECT 1
             FROM task_runs tr
             WHERE tr.account_id = :account_id
               AND tr.task_mode = 'real_run'
               AND (
                 tr.status IN ('queued', 'running')
                 OR tr.reconciliation_required = 1
                 OR EXISTS (
                   SELECT 1
                   FROM external_action_intents eai
                   WHERE eai.task_id = tr.task_id
                     AND eai.account_id = tr.account_id
                     AND eai.lottery_id = tr.lottery_id
                     AND (
                       eai.status IN ('started', 'unknown')
                       OR eai.effect_certainty = 'unknown'
                     )
                 )
               )
             LIMIT 1
           ) AS has_unsettled_state""",
        {"account_id": int(account_id)},
    )
    return bool(row and int(row["has_unsettled_state"] or 0) == 1)


async def account_has_frozen_real_action_state(account_id: int) -> bool:
    """Freeze identity while remote effects are confirmed or unresolved.

    Confirmed effects remain bound to the current durable lottery intent so a
    missing-action Repair cannot silently switch remote accounts. Unknown
    effects and explicit reconciliation requirements freeze independently of
    the current intent head: superseding an intent cannot make an ambiguous
    remote mutation safe to forget.
    """

    if await account_has_unsettled_real_action_state(account_id):
        return True
    row = await database.fetch_one(
        """SELECT EXISTS (
             SELECT 1
             FROM task_runs tr
             WHERE tr.account_id = :account_id
               AND tr.task_mode = 'real_run'
               AND (
                 EXISTS (
                   SELECT 1
                   FROM lottery_execution_intent_heads head
                   JOIN lottery_execution_intents root
                     ON root.lottery_id = head.lottery_id
                    AND root.intent_id = head.current_intent_id
                    AND root.source_account_id = tr.account_id
                   WHERE head.lottery_id = tr.lottery_id
                 )
                 AND (
                   EXISTS (
                     SELECT 1
                     FROM external_action_intents eai
                     WHERE eai.task_id = tr.task_id
                       AND eai.account_id = tr.account_id
                       AND eai.lottery_id = tr.lottery_id
                       AND eai.status = 'succeeded'
                       AND eai.effect_certainty = 'confirmed_effect'
                       AND eai.outcome = 'ok'
                   )
                   OR EXISTS (
                     SELECT 1
                     FROM bilibili_action_ledger bal
                     WHERE bal.task_id = tr.task_id
                       AND bal.account_id = tr.account_id
                       AND bal.lottery_id = tr.lottery_id
                       AND bal.task_mode = 'real_run'
                       AND bal.ok = 1
                       AND bal.phase IS NOT NULL
                   )
                   OR EXISTS (
                     SELECT 1
                     FROM task_phases tp
                     WHERE tp.task_id = tr.task_id
                       AND tp.account_id = tr.account_id
                       AND tp.lottery_id = tr.lottery_id
                   )
                   OR EXISTS (
                     SELECT 1
                     FROM events event_row
                     WHERE event_row.correlation_id = tr.task_id
                       AND event_row.event_type = 'TaskPhaseCompleted'
                   )
                 )
               )
             LIMIT 1
           ) AS has_frozen_state""",
        {"account_id": int(account_id)},
    )
    return bool(row and int(row["has_frozen_state"] or 0) == 1)


async def account_has_deletion_blocking_real_action_state(
    account_id: int,
) -> bool:
    """Protect credentials needed to settle ambiguity or finish exact Repair."""

    if await account_has_unsettled_real_action_state(account_id):
        return True
    intent_rows = await database.fetch_all(
        """SELECT root.contract_version, root.intent_id, root.intent_hash,
                  root.lottery_id, root.source_task_id,
                  root.source_account_id, root.platform, root.raw_url,
                  root.canonical_url, root.full_action_plan,
                  root.full_action_plan_hash, root.full_required_actions,
                  root.full_required_actions_hash, root.rule_snapshot_id,
                  root.rule_hash, root.execution_path_id, root.target_hash
           FROM lottery_execution_intent_heads head
           JOIN lottery_execution_intents root
             ON root.lottery_id = head.lottery_id
            AND root.intent_id = head.current_intent_id
           WHERE root.source_account_id = :account_id
           ORDER BY root.lottery_id
           LIMIT :limit""",
        {
            "account_id": int(account_id),
            "limit": ACCOUNT_DELETE_INTENT_CHECK_LIMIT + 1,
        },
    )
    if not intent_rows:
        return False
    if len(intent_rows) > ACCOUNT_DELETE_INTENT_CHECK_LIMIT:
        # Deletion is rare and security-sensitive; never hold the account row
        # while scanning an unbounded execution history.  A future explicit
        # archival/abandon workflow can settle oversized histories.
        return True

    # The caller already owns the account-row fence used by every dispatch and
    # operation-lease acquisition.  These must remain consistent reads: taking
    # lottery/intent locks after the account lock would invert dispatch's
    # lottery -> account order and create an AB-BA deadlock.  A dispatch that
    # has not acquired the account fence cannot commit usable work after this
    # account is deleted.
    #
    # Local import avoids coupling router initialization while reusing the
    # same cross-platform completion authority as Repair planning.
    from app.api.lotteries import (
        load_real_run_completion_authorities_for_lotteries,
    )
    from app.services.execution_intents import (
        ExecutionIntentError,
        coerce_frozen_execution_intent,
    )
    from app.platform_modules import PlatformModuleUnavailableError

    frozen_intents = []
    try:
        for row in intent_rows:
            frozen = coerce_frozen_execution_intent(dict(row))
            if frozen.source_account_id != int(account_id):
                return True
            frozen_intents.append(frozen)
    except (
        ExecutionIntentError,
        ImportError,
        KeyError,
        PlatformModuleUnavailableError,
        TypeError,
        ValueError,
    ):
        # A corrupt or currently unloadable platform contract cannot prove
        # that all externally visible effects have been settled.  Account
        # deletion must remain fail closed rather than trusting one JSON field.
        return True

    intent_platforms = {
        frozen.lottery_id: frozen.platform for frozen in frozen_intents
    }
    authorities = (
        await load_real_run_completion_authorities_for_lotteries(
            intent_platforms,
            execution_intents={
                frozen.lottery_id: frozen
                for frozen in frozen_intents
            },
        )
    )
    for frozen in frozen_intents:
        authority = authorities.get(frozen.lottery_id)
        if authority is None:
            return True
        if authority.blockers:
            return True
        completed = set(authority.completed_actions)
        if completed and any(
            action not in completed
            for action in frozen.full_required_actions
        ):
            return True
    return False


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
            "qr_login_blocker": value.get("qr_login_blocker"),
            "cookie_login": value.get("cookie_login", True),
            "action_adapter": value.get("action_adapter", False) or platform_has_runtime_real_adapter(selector_config, key),
            "adapter_status": platform_adapter_status(selector_config, key, value),
            "cookie_domain": value.get("cookie_domain"),
            "account_check_url": value.get("account_check_url"),
        }
        for key, value in get_platforms().items()
    ]


def platform_adapter_status(selector_config: dict, platform: str, metadata: dict) -> str:
    kind = platform_real_adapter_kind(selector_config, platform)
    if kind in {"api", "selector"}:
        return "configured"
    return metadata.get("adapter_status", "planned")


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
        item["credential_kind"] = account_credential_kind(
            str(item.get("platform") or ""),
            credential,
        )
        if item.get("latest_risk_event"):
            item["latest_risk_event"] = parse_json_field(item["latest_risk_event"])
        if item.get("latest_calibration"):
            item["latest_calibration"] = parse_json_field(item["latest_calibration"])
            if isinstance(item["latest_calibration"], dict):
                item["latest_calibration"]["result"] = parse_json_field(
                    item["latest_calibration"].get("result")
                )
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

    calibration = None
    async with database.transaction():
        account_id = await database.execute(
            """INSERT INTO accounts
                 (platform, fingerprint_id, proxy_id, encrypted_credential, status)
               VALUES (:platform, :fid, :pid, :cred, :status)""",
            {
                "platform": data.platform,
                "fid": fingerprint_id,
                "pid": data.proxy_id,
                "cred": credential,
                "status": status,
            },
        )
        if credential:
            calibration = await queue_account_calibration(
                account_id,
                data.platform,
                fallback_account_status="login_required",
            )
        await audit_event(
            request,
            action="account.create",
            resource_type="account",
            resource_id=account_id,
            result="created",
            risk_level="high" if credential else "medium",
            detail={
                "platform": data.platform,
                "credential_imported": bool(credential),
                "calibration": calibration,
            },
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
            # Keep the Bilibili provider outside Core's import-time fault
            # domain. A broken optional provider must fail only this endpoint.
            from app.services.bilibili_qr import generate_bilibili_qr

            qr_content, provider_key = await generate_bilibili_qr()
        except Exception as exc:
            raise HTTPException(502, detail="Bilibili QR service is unavailable") from exc
        async with database.transaction():
            superseded_sessions = await supersede_active_login_sessions(
                replacement_session_id=session_id,
                actor_id=actor["actor_id"],
            )
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
                detail={
                    "platform": data.platform,
                    "login_mode": "official_qr",
                    "superseded_sessions": superseded_sessions,
                },
            )
            await record_event(
                aggregate="browser",
                aggregate_id=session_id,
                event_type="QrLoginWaitingScan",
                payload={
                    "platform": data.platform,
                    "login_mode": "official_qr",
                    "superseded_sessions": superseded_sessions,
                },
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

    login_message = build_login_request_message(
        session_id=session_id,
        platform=data.platform,
        login_url=platform_cfg["login_url"],
    )
    async with database.transaction():
        superseded_sessions = await supersede_active_login_sessions(
            replacement_session_id=session_id,
            actor_id=actor["actor_id"],
        )
        await database.execute(
            """INSERT INTO login_sessions
                 (session_id, platform, status, login_url, qr_image_path,
                  expires_at)
               VALUES
                 (:session_id, :platform, 'queued', :login_url, :image_path,
                  DATE_ADD(NOW(), INTERVAL 5 MINUTE))""",
            {
                "session_id": session_id,
                "platform": data.platform,
                "login_url": platform_cfg["login_url"],
                "image_path": image_path,
            },
        )
        await enqueue_login_request_outbox(login_message)
        await audit_event(
            request,
            action="account.login_qr.start",
            resource_type="login_session",
            resource_id=session_id,
            result="queued",
            risk_level="medium",
            detail={
                "platform": data.platform,
                "superseded_sessions": superseded_sessions,
            },
        )
        await record_event(
            aggregate="browser",
            aggregate_id=session_id,
            event_type="QrLoginRequested",
            payload={
                "platform": data.platform,
                "login_url": platform_cfg["login_url"],
                "superseded_sessions": superseded_sessions,
            },
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
    async with database.transaction():
        row = await database.fetch_one(
            """SELECT login_sessions.*,
                      (
                        expires_at IS NOT NULL
                        AND expires_at <= NOW()
                      ) AS is_expired
               FROM login_sessions
               WHERE session_id = :sid
               FOR UPDATE""",
            {"sid": session_id},
        )
        from app.services.bilibili_qr import provider_qr_controls_expiry

        provider_authoritative_qr = bool(
            row
            and provider_qr_controls_expiry(
                row["platform"],
                row["provider_key"],
            )
        )
        if row and (
            int(row["is_expired"] or 0) == 1
            and row["status"] not in {"confirmed", "expired", "failed"}
            # The provider result is authoritative for official Bilibili QR
            # sessions. Poll it before applying our local timeout so a mobile
            # confirmation at the boundary cannot be discarded as expired.
            and not provider_authoritative_qr
        ):
            await database.execute(
                """UPDATE login_sessions
                   SET status = 'expired',
                       error_message = 'QR login session expired',
                       completed_at = NOW(),
                       updated_at = NOW()
                   WHERE session_id = :sid
                     AND status = :expected_status""",
                {
                    "sid": row["session_id"],
                    "expected_status": row["status"],
                },
            )
            await enqueue_login_profile_cleanup(row["session_id"])
            row = await database.fetch_one(
                "SELECT * FROM login_sessions WHERE session_id = :sid",
                {"sid": row["session_id"]},
            )
        elif row and row["status"] in {
            "confirmed",
            "expired",
            "failed",
        }:
            await enqueue_login_profile_cleanup(row["session_id"])
    if not row:
        raise HTTPException(404, detail="Login session not found")
    # Do not leak the computed lock-time expiry helper through the public
    # response contract.
    row = await database.fetch_one(
        "SELECT * FROM login_sessions WHERE session_id = :sid",
        {"sid": row["session_id"]},
    )
    if row["platform"] != "bilibili" or not row["provider_key"]:
        return serialize_login_session(row)
    if row["status"] in {"confirmed", "expired", "failed"}:
        return serialize_login_session(row)

    try:
        # Import on the platform-specific request path for the same isolation
        # reason as QR creation above.
        from app.services.bilibili_qr import poll_bilibili_qr

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
        async with database.transaction():
            current = await database.fetch_one(
                """SELECT session_id, status
                   FROM login_sessions
                   WHERE session_id = :session_id
                   FOR UPDATE""",
                {"session_id": session_id},
            )
            if not current:
                raise HTTPException(
                    404,
                    detail="Login session not found",
                )
            if current["status"] not in {
                "confirmed",
                "expired",
                "failed",
            }:
                await database.execute(
                    """UPDATE login_sessions
                       SET status = :status,
                           error_message = NULL,
                           completed_at = CASE
                             WHEN :status IN ('expired', 'failed')
                             THEN NOW()
                             ELSE completed_at
                           END,
                           updated_at = NOW()
                       WHERE session_id = :session_id
                         AND status = :expected_status""",
                    {
                        "session_id": current["session_id"],
                        "status": result.status,
                        "expected_status": current["status"],
                    },
                )
            if result.status in {"expired", "failed"}:
                await enqueue_login_profile_cleanup(
                    current["session_id"]
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

    return _profile_png_response(
        row["qr_image_path"],
        allowed_root=PROFILES_DIR / "login-sessions",
    )


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
        current_plaintext = None
        current_credential = locked["encrypted_credential"]
        if isinstance(current_credential, memoryview):
            current_credential = current_credential.tobytes()
        if current_credential:
            try:
                if str(locked["platform"]) == "weibo":
                    # OAuth identity continuity is security-sensitive.  Old
                    # executable envelopes must retain their strict AAD
                    # binding even when their token has expired.
                    current_plaintext = cookie_vault.decrypt_strict(
                        current_credential,
                        aad=CREDENTIAL_AAD,
                    )
                else:
                    current_plaintext = cookie_vault.decrypt(
                        current_credential,
                        aad=CREDENTIAL_AAD,
                    )
            except Exception:
                current_plaintext = None
        current_subject = account_remote_subject(
            str(locked["platform"]),
            current_plaintext or "",
        )
        replacement_subject = account_remote_subject(
            str(locked["platform"]),
            normalized,
        )
        if (
            (
                current_subject is None
                or replacement_subject is None
                or current_subject != replacement_subject
            )
            and await account_has_frozen_real_action_state(account_id)
        ):
            raise HTTPException(
                409,
                detail={
                    "message": (
                        "Cannot change the remote account identity after "
                        "confirmed real actions; finish or reconcile the "
                        "frozen Repair intent first"
                    ),
                    "code": "confirmed_real_actions_freeze_account_subject",
                    "account_id": account_id,
                },
            )
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
            raise HTTPException(
                409,
                detail="Account changed; credential update was not applied",
            )
        calibration = await queue_account_calibration(
            account_id,
            row["platform"],
            # The credential changed. Relay exhaustion must never restore a
            # formerly-ready account with an uncalibrated new credential.
            fallback_account_status="login_required",
        )
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
        if await account_has_deletion_blocking_real_action_state(account_id):
            raise HTTPException(
                409,
                detail={
                    "message": (
                        "Cannot delete credentials while real actions are "
                        "unsettled or an exact Repair still has missing actions"
                    ),
                    "code": "real_action_state_requires_account_credential",
                    "account_id": account_id,
                },
            )
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
        await enqueue_account_profile_cleanup(
            account_id,
            str(row["platform"]),
        )
        await audit_event(
            request,
            action="account.delete",
            resource_type="account",
            resource_id=account_id,
            result="soft_deleted",
            risk_level="high",
            detail={
                "previous_status": row["status"],
                "database_credential_removed": True,
                "browser_profile_cleanup": "queued",
            },
        )
        await record_event(
            aggregate="account",
            aggregate_id=account_id,
            event_type="AccountSoftDeleted",
            payload={
                "previous_status": row["status"],
                "database_credential_removed": True,
                "browser_profile_cleanup": "queued",
            },
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
    async with database.transaction():
        row = await database.fetch_one(
            """SELECT platform, status,
                      OCTET_LENGTH(encrypted_credential) AS credential_size
                 FROM accounts
                WHERE id = :id
                FOR UPDATE""",
            {"id": account_id},
        )
        if not row:
            raise HTTPException(404, detail="Account not found")
        if row["credential_size"] == 0:
            raise HTTPException(
                400,
                detail=(
                    "Cannot calibrate an account before importing a credential"
                ),
            )
        if row["status"] == "executing":
            raise HTTPException(
                409,
                detail="Cannot calibrate an account while it is executing",
            )
        if row["status"] == "banned":
            if not data.force:
                raise HTTPException(
                    409,
                    detail=(
                        "Cannot calibrate a banned account without an "
                        "admin override"
                    ),
                )
            require_min_role(request, "admin")
            require_confirmation(request)
        updated = await execute_affected_rows(
            """UPDATE accounts
                  SET status = 'warming', updated_at = NOW(),
                      version = version + 1
                WHERE id = :id AND status <> 'executing'""",
            {"id": account_id},
            db=database,
        )
        if int(updated or 0) != 1:
            raise HTTPException(
                409,
                detail=(
                    "Account started executing; calibration was not queued"
                ),
            )
        calibration = await queue_account_calibration(
            account_id,
            row["platform"],
            fallback_account_status=str(
                row["status"] or "login_required"
            ),
        )
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


@router.post("/{account_id}/weibo-oauth-capability-attestation")
async def attest_weibo_oauth_capabilities(
    account_id: int,
    data: WeiboOAuthCapabilityAttestationRequest,
    request: Request,
):
    """Queue identity verification for an independently admin-attested grant set."""

    actor = require_min_role(request, "admin")
    require_confirmation(request)
    if data.confirm is not True:
        raise HTTPException(409, detail="Weibo OAuth capability attestation body confirmation required")
    review_status = data.app_review_status.strip().lower()
    client_type = data.client_type.strip().lower()
    if review_status not in {"approved", "test_only", "unknown"}:
        raise HTTPException(
            400,
            detail={"code": "weibo_oauth_app_review_status_invalid"},
        )
    if client_type not in {"weibo", "other"}:
        raise HTTPException(
            400,
            detail={"code": "weibo_oauth_client_type_invalid"},
        )
    grants = data.granted_actions
    if (
        not isinstance(grants, dict)
        or set(grants) != set(WEIBO_ACTION_ORDER)
        or any(type(value) is not bool for value in grants.values())
    ):
        raise HTTPException(
            400,
            detail={"code": "weibo_oauth_capability_contract_mismatch"},
        )
    normalized_grants = {
        action: grants[action] for action in WEIBO_ACTION_ORDER
    }
    previous_account_status = None
    calibration_id = str(uuid.uuid4())
    attested_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    operator_attestation = {
        "version": 1,
        "attested_by": actor["actor_id"],
        "attested_at": attested_at,
        "app_review_status": review_status,
        "client_type": client_type,
        "granted_actions": normalized_grants,
    }
    check_url = "https://api.weibo.com/2/account/get_uid.json"
    async with database.transaction():
        locked = await lock_account_for_execution_contract_mutation(account_id)
        previous_account_status = str(locked["status"] or "login_required")
        if locked["deleted_at"]:
            raise HTTPException(409, detail="Cannot attest a deleted account")
        if str(locked["platform"]) != "weibo":
            raise HTTPException(400, detail="OAuth capability attestation is Weibo-only")
        if str(locked["status"]) in {"executing", "banned"}:
            raise HTTPException(
                409,
                detail=f"Cannot attest capabilities while account is {locked['status']}",
            )
        credential_row = await database.fetch_one(
            "SELECT encrypted_credential FROM accounts WHERE id = :id",
            {"id": account_id},
        )
        if not credential_row or not credential_row["encrypted_credential"]:
            raise HTTPException(400, detail="Weibo OAuth credential is required")
        try:
            decrypted = cookie_vault.decrypt_strict(
                credential_row["encrypted_credential"],
                aad=CREDENTIAL_AAD,
            )
            parse_weibo_oauth_credential(decrypted)
        except Exception as exc:
            raise HTTPException(
                400,
                detail={"code": "weibo_oauth_credential_invalid"},
            ) from exc
        await database.execute(
            """INSERT INTO account_calibrations
                 (calibration_id, platform, account_id, check_url, status, result)
               VALUES
                 (:calibration_id, 'weibo', :account_id, :check_url, 'queued', :result)""",
            {
                "calibration_id": calibration_id,
                "account_id": account_id,
                "check_url": check_url,
                "result": json.dumps(
                    {"operator_attestation": operator_attestation},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            },
        )
        await database.execute(
            """UPDATE accounts
               SET status = 'warming', updated_at = NOW(), version = version + 1
               WHERE id = :id""",
            {"id": account_id},
        )
        calibration_message = build_account_calibration_message(
            calibration_id=calibration_id,
            calibration_kind="weibo_oauth_capability",
            platform="weibo",
            account_id=account_id,
            check_url=check_url,
            fallback_account_status=(
                previous_account_status
                if previous_account_status
                in {"cold", "login_required", "ready", "cooling", "frozen"}
                else "login_required"
            ),
        )
        await enqueue_account_calibration_outbox(calibration_message)
        await audit_event(
            request,
            action="account.weibo_oauth_capabilities.attest",
            resource_type="account",
            resource_id=account_id,
            result="queued",
            risk_level="critical",
            detail={
                "calibration_id": calibration_id,
                "app_review_status": review_status,
                "client_type": client_type,
                "granted_actions": normalized_grants,
            },
        )
    await record_event(
        aggregate="account",
        aggregate_id=account_id,
        event_type="WeiboOAuthCapabilitiesAttested",
        payload={
            "calibration_id": calibration_id,
            "app_review_status": review_status,
            "client_type": client_type,
            "granted_actions": normalized_grants,
            "attested_at": attested_at,
        },
        correlation_id=calibration_id,
        actor_type="operator",
        actor_id=actor["actor_id"],
    )
    return {
        "status": "queued",
        "calibration": {
            "calibration_id": calibration_id,
            "status": "queued",
            "calibration_kind": "weibo_oauth_capability",
        },
        "attestation": operator_attestation,
    }


@router.get("/calibrations/{calibration_id}/screenshot")
async def get_account_calibration_screenshot(calibration_id: str):
    row = await database.fetch_one(
        """SELECT calibration_id, platform, screenshot_path
           FROM account_calibrations
           WHERE calibration_id = :calibration_id""",
        {"calibration_id": calibration_id},
    )
    if not row or not row["screenshot_path"]:
        raise HTTPException(404, detail="Calibration screenshot not ready")
    platform = str(row["platform"] or "").strip().casefold()
    if platform not in PLATFORM_IDS:
        raise HTTPException(404, detail="Calibration screenshot not ready")
    try:
        stored_calibration_id = str(
            uuid.UUID(str(row["calibration_id"] or "").strip())
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise HTTPException(
            404,
            detail="Calibration screenshot not ready",
        ) from exc
    expected_current_path = (
        PROFILES_DIR
        / platform
        / "account-calibrations"
        / f"{stored_calibration_id}.png"
    )
    expected_legacy_path = (
        LEGACY_CALIBRATION_DIR
        / f"{stored_calibration_id}.png"
    )
    stored_path = Path(str(row["screenshot_path"]))
    if stored_path == expected_current_path:
        allowed_root = expected_current_path.parent
    elif stored_path == expected_legacy_path:
        # Explicit compatibility for screenshots produced before 0025. No
        # other path under the former shared directory is accepted.
        allowed_root = LEGACY_CALIBRATION_DIR
    else:
        raise HTTPException(
            404,
            detail="Calibration screenshot not ready",
        )
    return _profile_png_response(
        stored_path,
        allowed_root=allowed_root,
    )


async def queue_account_calibration(
    account_id: int,
    platform: str,
    *,
    fallback_account_status: str = "login_required",
):
    platform_cfg = get_platform(platform)
    if not platform_cfg:
        raise HTTPException(400, detail=f"Unsupported platform: {platform}")
    calibration_kind = "browser_session"
    if platform in {"weibo", "douyin"}:
        credential_row = await database.fetch_one(
            "SELECT encrypted_credential FROM accounts WHERE id = :id",
            {"id": account_id},
        )
        credential_kind = account_credential_kind(
            platform,
            credential_row["encrypted_credential"] if credential_row else None,
        )
        if platform == "weibo" and credential_kind == "weibo_oauth":
            calibration_kind = "weibo_oauth_identity"
        elif platform == "douyin" and credential_kind == "device_agent":
            calibration_kind = "device_agent"
        elif credential_kind != "browser_session":
            raise ValueError("account_credential_invalid")
    calibration_id = str(uuid.uuid4())
    if calibration_kind == "weibo_oauth_identity":
        check_url = "https://api.weibo.com/2/account/get_uid.json"
    elif calibration_kind == "device_agent":
        check_url = DOUYIN_DEVICE_CALIBRATION_CHECK_URL
    else:
        check_url = platform_cfg.get("account_check_url") or platform_cfg["login_url"]
    message = build_account_calibration_message(
        calibration_id=calibration_id,
        calibration_kind=calibration_kind,
        platform=platform,
        account_id=account_id,
        check_url=check_url,
        fallback_account_status=fallback_account_status,
    )
    async with database.transaction():
        await database.execute(
            """INSERT INTO account_calibrations
                 (calibration_id, platform, account_id, check_url, status)
               VALUES
                 (:calibration_id, :platform, :account_id, :check_url, 'queued')""",
            {
                "calibration_id": calibration_id,
                "platform": platform,
                "account_id": account_id,
                "check_url": check_url,
            },
        )
        await enqueue_account_calibration_outbox(message)
    return {
        "calibration_id": calibration_id,
        "status": "queued",
        "calibration_kind": calibration_kind,
        "check_url": check_url,
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


def normalize_and_validate_credential(platform: str, payload: str) -> str:
    platform_cfg = get_platform(platform)
    if not platform_cfg:
        raise ValueError(f"Unsupported platform: {platform}")
    if platform == "weibo":
        try:
            parsed = json.loads(payload)
        except (TypeError, json.JSONDecodeError):
            parsed = None
        if isinstance(parsed, dict) and (
            "credential_kind" in parsed or "access_token" in parsed
        ):
            return normalize_weibo_oauth_credential(payload)
    if platform == "douyin":
        try:
            parsed = json.loads(payload)
        except (TypeError, json.JSONDecodeError):
            parsed = None
        if isinstance(parsed, dict) and (
            "credential_kind" in parsed or "device_agent" in parsed
        ):
            return normalize_douyin_device_credential(payload)
    cookies = parse_cookie_payload(platform, payload)
    validate_required_cookies(cookies, platform_cfg.get("required_cookies", []))
    validate_api_cookie_name_uniqueness(platform, cookies)
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
    validate_api_cookie_name_uniqueness("bilibili", cookies)
    normalized = json.dumps(cookies, ensure_ascii=False, separators=(",", ":"))
    async with database.transaction():
        row = await database.fetch_one(
            "SELECT status, account_id FROM login_sessions WHERE session_id = :sid FOR UPDATE",
            {"sid": session_id},
        )
        if not row:
            raise HTTPException(404, detail="Login session not found")
        if row["status"] == "confirmed" and row["account_id"]:
            await enqueue_login_profile_cleanup(session_id)
            return int(row["account_id"])
        if row["status"] in {"expired", "failed"}:
            raise HTTPException(409, detail=f"Login session is {row['status']}")
        fingerprint_id = await ensure_default_fingerprint("bilibili")
        account_id = await database.execute(
            """INSERT INTO accounts (platform, fingerprint_id, encrypted_credential, status)
               VALUES ('bilibili', :fingerprint_id, :credential, 'warming')""",
            {"fingerprint_id": fingerprint_id, "credential": cookie_vault.encrypt(normalized, aad=CREDENTIAL_AAD)},
        )
        calibration = await queue_account_calibration(
            account_id,
            "bilibili",
            fallback_account_status="login_required",
        )
        await database.execute(
            """UPDATE login_sessions
               SET status = 'confirmed', account_id = :account_id, completed_at = NOW(),
                   error_message = NULL, updated_at = NOW()
               WHERE session_id = :session_id""",
            {"session_id": session_id, "account_id": account_id},
        )
        await enqueue_login_profile_cleanup(session_id)
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
