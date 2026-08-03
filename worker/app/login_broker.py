import asyncio
import uuid
from pathlib import Path

from app.db import database, redis
from app.event_store.service import record_event
from app.login_profile_cleanup import (
    enqueue_login_profile_cleanup,
    ensure_login_profile_cleanup_completed,
)
from app.platforms import get_platform
from app.services.account_calibration_outbox import (
    build_account_calibration_message,
    enqueue_account_calibration_outbox,
)
from app.utils.cookies import serialize_cookies
from app.utils.crypto import CREDENTIAL_AAD, cookie_vault
from app.utils.log import structured_log
from app.worker_identity import WORKER_ID
from shared.login_streams import (
    LOGIN_REQUEST_GROUP_NAME,
    LOGIN_REQUEST_STREAM_KEY,
    validate_login_request_stream_message,
)
from shared.redis_consumer_groups import (
    verify_redis_consumer_group,
)
from shared.task_streams import SAFE_TERMINAL_STREAM_ACK_DELETE_LUA


STREAM_KEY = LOGIN_REQUEST_STREAM_KEY
GROUP_NAME = LOGIN_REQUEST_GROUP_NAME
CONSUMER_NAME = WORKER_ID
PROFILE_ROOT = Path("/profiles/login-sessions")
MAX_ACTIVE_LOGIN_SESSIONS = 2
LOGIN_RECOVERY_BATCH = 20
LOGIN_RECOVERY_INTERVAL_SECONDS = 60
# A browser login has a five-minute product deadline.  Reclaim only after that
# entire window plus a safety margin, then settle from the locked DB session
# instead of replaying an ambiguous browser/account-creation operation.
LOGIN_IDLE_THRESHOLD_MS = 6 * 60 * 1000
LOGIN_PAGE_TEXT_MAX_CHARS = 32 * 1024
XIAOHONGSHU_QR_SELECTOR = ".login-container .qrcode-img"
XIAOHONGSHU_AUTHENTICATED_SELECTOR = (
    ".main-container .user .link-wrapper .channel"
)
XIAOHONGSHU_QR_WAIT_MILLISECONDS = 15_000
XIAOHONGSHU_QR_POLL_MILLISECONDS = 1_000


class LoginSessionExpired(RuntimeError):
    pass


def classify_login_page_blocker(
    platform: str,
    *,
    title: str,
    visible_text: str,
) -> dict[str, str] | None:
    """Map known provider denial pages to fixed, non-sensitive failures."""

    normalized_platform = str(platform or "").strip().casefold()
    normalized_title = str(title or "").strip()
    text = str(visible_text or "")[:LOGIN_PAGE_TEXT_MAX_CHARS]
    combined_text = f"{normalized_title}\n{text}"
    if (
        normalized_platform == "xiaohongshu"
        and "300012" in combined_text
    ):
        return {
            "error_message": (
                "Xiaohongshu blocked this login request (risk code 300012); "
                "stop automated retries and complete any required "
                "verification in the official app or site before retrying"
            ),
            "error_code": "xiaohongshu_login_network_risk_300012",
        }
    if normalized_platform == "douyin" and (
        "非法应用" in text and "error_code" in text and "22" in text
    ):
        return {
            "error_message": (
                "Douyin rejected the legacy browser QR endpoint "
                "(error 22); use the configured device-agent login path"
            ),
            "error_code": "douyin_legacy_qr_endpoint_rejected",
        }
    if (
        normalized_platform == "douyin"
        and normalized_title == "验证码中间页"
    ):
        return {
            "error_message": (
                "Douyin requires interactive verification before Web login; "
                "use the configured device-agent login path"
            ),
            "error_code": "douyin_web_login_verification_required",
        }
    return None


async def detect_login_page_blocker(page, platform: str) -> dict[str, str] | None:
    title = ""
    visible_text = ""
    try:
        title = await page.title()
    except Exception:
        pass
    try:
        visible_text = await page.locator("body").inner_text(timeout=3000)
    except Exception:
        pass
    if not title and not visible_text:
        return None
    return classify_login_page_blocker(
        platform,
        title=title,
        visible_text=visible_text,
    )


async def capture_login_qr_image(
    page,
    platform: str,
    image_path: Path,
    *,
    wait_milliseconds: int = 0,
) -> bool:
    """Capture a provider QR element when its DOM contract is known."""

    if str(platform or "").strip().casefold() != "xiaohongshu":
        await page.screenshot(path=str(image_path), full_page=True)
        return True

    qr = page.locator(XIAOHONGSHU_QR_SELECTOR)
    try:
        await qr.wait_for(
            state="visible",
            timeout=max(int(wait_milliseconds), 1),
        )
        await qr.screenshot(path=str(image_path))
        return True
    except Exception:
        return False


async def login_page_authenticated(page, platform: str) -> bool:
    """Require provider page evidence in addition to credential cookies."""

    if str(platform or "").strip().casefold() != "xiaohongshu":
        return True
    try:
        return await page.locator(
            XIAOHONGSHU_AUTHENTICATED_SELECTOR
        ).is_visible(timeout=3_000)
    except Exception:
        return False


async def authenticated_login_cookies(page, ctx, platform: str, required: set):
    cookies = await ctx.cookies()
    names = {cookie.get("name") for cookie in cookies}
    if not required or not required.issubset(names):
        return None
    if not await login_page_authenticated(page, platform):
        return None
    return cookies


async def complete_authenticated_login_if_ready(
    page,
    ctx,
    platform: str,
    required: set,
    session_id: str,
) -> bool:
    """Bind a login immediately once both cookies and page identity agree."""

    cookies = await authenticated_login_cookies(
        page,
        ctx,
        platform,
        required,
    )
    if cookies is None:
        return False
    created = await create_account_from_cookies(
        platform,
        cookies,
        session_id=session_id,
    )
    if created.get("status") == "expired":
        return True
    account_id = created["account_id"]
    if not created.get("created", True):
        structured_log(
            "info",
            "qr_login_already_confirmed",
            account_id=account_id,
        )
        return True
    structured_log("info", "qr_login_confirmed", account_id=account_id)
    return True


async def login_session_owner_state(
    session_id: str,
    *,
    expected_status: str,
) -> str:
    """Return owned, expired, or released for one browser login owner."""

    if expected_status not in {"opening", "waiting_scan"}:
        raise ValueError("login_owner_expected_status_invalid")
    row = await database.fetch_one(
        """SELECT status,
                  (
                    expires_at IS NOT NULL
                    AND expires_at > NOW()
                  ) AS not_expired
           FROM login_sessions
           WHERE session_id = :session_id""",
        {"session_id": session_id},
    )
    if not row:
        return "released"
    current = str(row["status"] or "").strip().casefold()
    if current != expected_status:
        return "released"
    if int(row["not_expired"] or 0) != 1:
        return "expired"
    return "owned"


async def login_session_is_waiting(session_id: str) -> bool:
    """Stop a superseded browser owner before it occupies a login slot."""

    return (
        await login_session_owner_state(
            session_id,
            expected_status="waiting_scan",
        )
        == "owned"
    )


async def login_loop(pool, shutdown_event: asyncio.Event):
    active_tasks: set[asyncio.Task] = set()
    semaphore = asyncio.Semaphore(MAX_ACTIVE_LOGIN_SESSIONS)
    last_reclaim_at = 0.0
    try:
        await verify_redis_consumer_group(
            redis,
            stream_key=STREAM_KEY,
            group_name=GROUP_NAME,
        )

        PROFILE_ROOT.mkdir(parents=True, exist_ok=True)
        while not shutdown_event.is_set():
            completed = {
                task for task in active_tasks if task.done()
            }
            if completed:
                await asyncio.gather(
                    *completed,
                    return_exceptions=True,
                )
                active_tasks.difference_update(completed)
            if len(active_tasks) >= MAX_ACTIVE_LOGIN_SESSIONS:
                # Keep backlog in Redis.  Reading ahead would move an
                # unbounded queue into Python tasks and start PEL idle time
                # before a browser slot is actually available.
                await asyncio.wait(
                    active_tasks,
                    timeout=1,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                continue
            try:
                now = asyncio.get_running_loop().time()
                if (
                    now - last_reclaim_at
                    >= LOGIN_RECOVERY_INTERVAL_SECONDS
                ):
                    await reclaim_stale_login_messages(pool)
                    last_reclaim_at = now
                msgs = await asyncio.wait_for(
                    redis.xreadgroup(
                        GROUP_NAME,
                        CONSUMER_NAME,
                        {STREAM_KEY: ">"},
                        count=1,
                        block=5000,
                    ),
                    timeout=1,
                )
                if not msgs:
                    continue
                for msg_id, data in msgs[0][1]:
                    session = {k: v for k, v in data.items()}
                    active_tasks.add(
                        asyncio.create_task(
                            handle_and_ack(
                                pool,
                                msg_id,
                                session,
                                semaphore,
                            )
                        )
                    )
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                structured_log("error", "login_loop_error", exception=e)
                await asyncio.sleep(3)
    finally:
        for task in active_tasks:
            task.cancel()
        if active_tasks:
            await asyncio.gather(
                *active_tasks,
                return_exceptions=True,
            )


async def handle_and_ack(pool, msg_id: str, session: dict, semaphore: asyncio.Semaphore):
    try:
        validate_login_request_stream_message(session)
    except (TypeError, ValueError) as exc:
        # A malformed durable envelope is a poison message, not retryable
        # browser work. The producer validates this exact contract before
        # inserting its Outbox row; never log the untrusted envelope itself.
        structured_log(
            "error",
            "login_request_message_rejected",
            error_code=str(exc)[:96],
        )
        await acknowledge_terminal_login_message(msg_id)
        return
    async with semaphore:
        await handle_login_session(pool, session)
    if not await ensure_login_profile_cleanup_completed(
        pool,
        session["session_id"],
    ):
        raise RuntimeError("login_profile_cleanup_not_completed")
    # Cancellation and unknown exceptions intentionally leave the delivery in
    # the PEL.  The bounded recovery pass settles it from database authority;
    # acknowledging from ``finally`` would silently discard interrupted work.
    await acknowledge_terminal_login_message(msg_id)


async def acknowledge_terminal_login_message(msg_id: str) -> dict:
    """Atomically ACK and delete only after every attached group confirms."""

    result = list(
        await redis.eval(
            SAFE_TERMINAL_STREAM_ACK_DELETE_LUA,
            1,
            STREAM_KEY,
            GROUP_NAME,
            msg_id,
        )
        or ()
    )
    if len(result) != 2:
        raise RuntimeError("login_terminal_ack_result_invalid")
    try:
        acknowledged = int(result[0])
        deleted = int(result[1])
    except (TypeError, ValueError) as exc:
        raise RuntimeError("login_terminal_ack_result_invalid") from exc
    if acknowledged not in {0, 1} or deleted not in {0, 1}:
        raise RuntimeError("login_terminal_ack_result_invalid")
    # [0, 1] is a legitimate recovery result: a prior script invocation may
    # have durably ACKed before an ACL-denied XDEL, or another group may have
    # released the last pending reference after this group already ACKed.
    if acknowledged == 0 and deleted == 0:
        raise RuntimeError("login_terminal_ack_lost_ownership")
    return {
        "acknowledged": acknowledged,
        "deleted": deleted,
    }


async def reclaim_stale_login_messages(pool) -> int:
    """Fail closed stale browser-login deliveries without ambiguous replay."""

    pending = await redis.xpending_range(
        STREAM_KEY,
        GROUP_NAME,
        min="-",
        max="+",
        count=LOGIN_RECOVERY_BATCH,
        idle=LOGIN_IDLE_THRESHOLD_MS,
    )
    settled = 0
    for entry in pending or ():
        message_id = entry.get("message_id")
        if not message_id:
            continue
        claimed = await redis.xclaim(
            STREAM_KEY,
            GROUP_NAME,
            CONSUMER_NAME,
            min_idle_time=LOGIN_IDLE_THRESHOLD_MS,
            message_ids=[message_id],
        )
        if not claimed:
            continue
        claimed_id, fields = claimed[0]
        try:
            validate_login_request_stream_message(fields)
        except (TypeError, ValueError) as exc:
            # A stale poison envelope has no trustworthy database binding.
            # Match the live-consumption path: discard the envelope without
            # logging attacker-controlled fields or manufacturing a session.
            structured_log(
                "error",
                "login_request_message_rejected",
                error_code=str(exc)[:96],
            )
            await acknowledge_terminal_login_message(claimed_id)
            settled += 1
            continue
        session_id = fields["session_id"]
        try:
            terminal = await settle_stale_login_session(session_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            structured_log(
                "error",
                "login_recovery_settlement_failed",
                message_id=claimed_id,
                exception=exc,
            )
            continue
        if terminal:
            if not await ensure_login_profile_cleanup_completed(
                pool,
                session_id,
            ):
                continue
            await acknowledge_terminal_login_message(claimed_id)
            settled += 1
    return settled


async def settle_stale_login_session(session_id: str) -> bool:
    """Settle one abandoned session under the same DB lock as account bind."""

    if not session_id:
        return True
    await settle_login_session_without_account(
        session_id,
        status="failed",
        error_message=(
            "Login worker ownership expired; start a new QR login"
        ),
        event_type="QrLoginFailed",
        error_code="login_worker_ownership_expired",
    )
    return True


async def settle_login_session_without_account(
    session_id: str,
    *,
    status: str,
    error_message: str,
    event_type: str,
    error_code: str,
) -> str:
    """Atomically persist a non-success terminal state and its audit event."""

    if status not in {"failed", "expired"}:
        raise ValueError("login_terminal_status_invalid")
    async with database.transaction():
        row = await database.fetch_one(
            """SELECT status, account_id, platform
               FROM login_sessions
               WHERE session_id = :session_id
               FOR UPDATE""",
            {"session_id": session_id},
        )
        if not row:
            return "missing"
        current = str(row["status"] or "").strip().casefold()
        if current in {"confirmed", "failed", "expired"}:
            await enqueue_login_profile_cleanup(session_id)
            return current
        platform = str(row["platform"] or "").strip().casefold()
        await database.execute(
            """UPDATE login_sessions
               SET status = :status,
                   error_message = :error_message,
                   completed_at = NOW(),
                   updated_at = NOW()
               WHERE session_id = :session_id
                 AND status IN ('queued', 'opening', 'waiting_scan')""",
            {
                "session_id": session_id,
                "status": status,
                "error_message": error_message,
            },
        )
        await enqueue_login_profile_cleanup(session_id)
        await record_event(
            aggregate="browser",
            aggregate_id=session_id,
            event_type=event_type,
            payload={
                "platform": platform,
                "error": error_code,
            },
            correlation_id=session_id,
        )
    return status


async def expire_locked_login_session(
    session_id: str,
    *,
    platform: str,
    expected_status: str,
) -> None:
    """Converge the exact locked pre-terminal state at a wall-clock boundary."""

    if expected_status not in {"queued", "opening"}:
        raise ValueError("login_expiry_expected_status_invalid")
    await database.execute(
        """UPDATE login_sessions
           SET status = 'expired',
               error_message = :error_message,
               completed_at = NOW(),
               updated_at = NOW()
           WHERE session_id = :session_id
             AND status = :expected_status
             AND (
               expires_at IS NULL
               OR expires_at <= NOW()
             )""",
        {
            "session_id": session_id,
            "expected_status": expected_status,
            "error_message": "QR login session expired",
        },
    )
    affected = await database.fetch_one(
        "SELECT ROW_COUNT() AS affected"
    )
    if affected is None:
        raise RuntimeError("login_expiry_row_count_unavailable")
    if int(affected["affected"] or 0) != 1:
        # The caller owns the row lock. A zero here means the DB contract no
        # longer matches the state just observed; leave the Redis message in
        # PEL rather than ACKing a queued/opening orphan.
        raise RuntimeError("login_expiry_state_transition_lost")
    await enqueue_login_profile_cleanup(session_id)
    await record_event(
        aggregate="browser",
        aggregate_id=session_id,
        event_type="QrLoginExpired",
        payload={
            "platform": platform,
            "error": "qr_login_session_expired",
        },
        correlation_id=session_id,
    )


async def claim_login_session_for_processing(
    session: dict,
    *,
    qr_image_path: str,
) -> dict:
    """Claim exactly one queued, unexpired DB session for at-least-once input."""

    validate_login_request_stream_message(session)
    session_id = session["session_id"]
    platform = session["platform"]
    login_url = session["login_url"]
    async with database.transaction():
        row = await database.fetch_one(
            """SELECT status, account_id, platform, login_url, expires_at,
                      (
                        expires_at IS NOT NULL
                        AND expires_at > NOW()
                      ) AS not_expired,
                      TIMESTAMPDIFF(
                        MICROSECOND,
                        NOW(),
                        expires_at
                      ) AS remaining_microseconds
               FROM login_sessions
               WHERE session_id = :session_id
               FOR UPDATE""",
            {"session_id": session_id},
        )
        if not row:
            return {"state": "missing"}

        current = str(row["status"] or "").strip().casefold()
        exact_binding = (
            str(row["platform"] or "").strip().casefold() == platform
            and str(row["login_url"] or "").strip() == login_url
        )
        if not exact_binding:
            if current == "queued":
                await database.execute(
                    """UPDATE login_sessions
                       SET status = 'failed',
                           error_message = :error_message,
                           completed_at = NOW(),
                           updated_at = NOW()
                       WHERE session_id = :session_id
                         AND status = 'queued'""",
                    {
                        "session_id": session_id,
                        "error_message": (
                            "Login request no longer matches its session"
                        ),
                    },
                )
                affected = await database.fetch_one(
                    "SELECT ROW_COUNT() AS affected"
                )
                if affected is None:
                    raise RuntimeError(
                        "login_binding_failure_row_count_unavailable"
                    )
                if int(affected["affected"] or 0) != 1:
                    raise RuntimeError(
                        "login_binding_failure_transition_lost"
                    )
                await enqueue_login_profile_cleanup(session_id)
                await record_event(
                    aggregate="browser",
                    aggregate_id=session_id,
                    event_type="QrLoginFailed",
                    payload={
                        "platform": str(
                            row["platform"] or ""
                        ).strip().casefold(),
                        "error": "login_request_binding_mismatch",
                    },
                    correlation_id=session_id,
                )
            elif current in {"confirmed", "failed", "expired"}:
                # A mismatched duplicate must still repair the durable
                # terminal-cleanup invariant before its stream delivery can
                # be acknowledged.
                await enqueue_login_profile_cleanup(session_id)
            return {"state": "binding_mismatch"}

        if current == "confirmed":
            await enqueue_login_profile_cleanup(session_id)
            return {
                "state": "confirmed",
                "account_id": row["account_id"],
            }
        if current in {"failed", "expired"}:
            await enqueue_login_profile_cleanup(session_id)
            return {"state": current}
        if current != "queued":
            # A distinct duplicate stream entry can arrive while the first
            # browser owner is already opening/waiting. ACK the duplicate and
            # leave the original PEL delivery as the sole recovery authority.
            return {"state": "in_progress"}

        if int(row["not_expired"] or 0) != 1:
            await expire_locked_login_session(
                session_id,
                platform=platform,
                expected_status="queued",
            )
            return {"state": "expired"}

        await database.execute(
            """UPDATE login_sessions
               SET status = 'opening',
                   qr_image_path = :qr_image_path,
                   error_message = NULL,
                   updated_at = NOW()
               WHERE session_id = :session_id
                 AND status = 'queued'
                 AND expires_at IS NOT NULL
                 AND expires_at > NOW()""",
            {
                "session_id": session_id,
                "qr_image_path": qr_image_path,
            },
        )
        affected = await database.fetch_one(
            "SELECT ROW_COUNT() AS affected"
        )
        if affected is None:
            raise RuntimeError("login_claim_row_count_unavailable")
        if int(affected["affected"] or 0) != 1:
            await expire_locked_login_session(
                session_id,
                platform=platform,
                expected_status="queued",
            )
            return {"state": "expired"}
        await record_event(
            aggregate="browser",
            aggregate_id=session_id,
            event_type="QrLoginOpening",
            payload={
                "platform": platform,
                "login_url": login_url,
                "qr_image_path": qr_image_path,
            },
            correlation_id=session_id,
        )
        remaining_microseconds = row["remaining_microseconds"]
        return {
            "state": "claimed",
            "remaining_seconds": min(
                max(float(remaining_microseconds or 0) / 1_000_000, 0.0),
                300.0,
            ),
        }


async def transition_login_session_to_waiting_scan(
    session_id: str,
    *,
    platform: str,
    qr_image_path: str,
) -> bool:
    """Advance only the current owner without resurrecting a terminal row."""

    async with database.transaction():
        row = await database.fetch_one(
            """SELECT status,
                      (
                        expires_at IS NOT NULL
                        AND expires_at > NOW()
                      ) AS not_expired
               FROM login_sessions
               WHERE session_id = :session_id
               FOR UPDATE""",
            {"session_id": session_id},
        )
        if not row:
            return False
        current = str(row["status"] or "").strip().casefold()
        if current != "opening":
            return False
        if int(row["not_expired"] or 0) != 1:
            await expire_locked_login_session(
                session_id,
                platform=platform,
                expected_status="opening",
            )
            return False
        await database.execute(
            """UPDATE login_sessions
               SET status = 'waiting_scan',
                   qr_image_path = :qr_image_path,
                   updated_at = NOW()
               WHERE session_id = :session_id
                 AND status = 'opening'
                 AND expires_at IS NOT NULL
                 AND expires_at > NOW()""",
            {
                "session_id": session_id,
                "qr_image_path": qr_image_path,
            },
        )
        affected = await database.fetch_one(
            "SELECT ROW_COUNT() AS affected"
        )
        if affected is None:
            raise RuntimeError(
                "login_waiting_scan_row_count_unavailable"
            )
        if int(affected["affected"] or 0) != 1:
            await expire_locked_login_session(
                session_id,
                platform=platform,
                expected_status="opening",
            )
            return False
        await record_event(
            aggregate="browser",
            aggregate_id=session_id,
            event_type="QrLoginWaitingScan",
            payload={
                "platform": platform,
                "qr_image_path": qr_image_path,
            },
            correlation_id=session_id,
        )
        return True


async def handle_login_session(pool, session: dict):
    session_id = session["session_id"]
    platform = session["platform"]
    login_url = session["login_url"]
    profile_dir = PROFILE_ROOT / session_id / "profile"
    image_path = PROFILE_ROOT / f"{session_id}.png"
    claim = await claim_login_session_for_processing(
        session,
        qr_image_path=str(image_path),
    )
    if claim["state"] != "claimed":
        structured_log(
            "info",
            "login_request_idempotent_skip",
            session_id=session_id,
            state=claim["state"],
        )
        return

    cfg = get_platform(platform)
    if not cfg or str(cfg.get("login_url") or "").strip() != login_url:
        await settle_login_session_without_account(
            session_id,
            status="failed",
            error_message="Unsupported or changed login platform",
            event_type="QrLoginFailed",
            error_code="login_platform_contract_mismatch",
        )
        return

    ctx = None
    page = None
    try:
        ctx = await pool.get_transient_context(str(profile_dir))
        page = await ctx.new_page()
        await page.goto(login_url, wait_until="domcontentloaded", timeout=45000)
        required = set(cfg.get("required_cookies", []))
        blocker = await detect_login_page_blocker(page, platform)
        if blocker:
            await settle_login_session_without_account(
                session_id,
                status="failed",
                error_message=blocker["error_message"],
                event_type="QrLoginFailed",
                error_code=blocker["error_code"],
            )
            return
        qr_ready = False
        if platform == "xiaohongshu":
            loop = asyncio.get_running_loop()
            qr_wait_deadline = loop.time() + min(
                XIAOHONGSHU_QR_WAIT_MILLISECONDS / 1000,
                max(float(claim["remaining_seconds"]), 0.0),
            )
            while loop.time() < qr_wait_deadline:
                owner_state = await login_session_owner_state(
                    session_id,
                    expected_status="opening",
                )
                if owner_state != "owned":
                    if owner_state == "expired":
                        await settle_login_session_without_account(
                            session_id,
                            status="expired",
                            error_message="QR login session expired",
                            event_type="QrLoginExpired",
                            error_code="qr_login_session_expired",
                        )
                    structured_log(
                        "info",
                        "qr_login_owner_released",
                        session_id=session_id,
                        platform=platform,
                        state=owner_state,
                    )
                    return
                if await complete_authenticated_login_if_ready(
                    page,
                    ctx,
                    platform,
                    required,
                    session_id,
                ):
                    return
                remaining_milliseconds = max(
                    1,
                    min(
                        XIAOHONGSHU_QR_POLL_MILLISECONDS,
                        int((qr_wait_deadline - loop.time()) * 1000),
                    ),
                )
                qr_ready = await capture_login_qr_image(
                    page,
                    platform,
                    image_path,
                    wait_milliseconds=remaining_milliseconds,
                )
                if qr_ready:
                    break
                blocker = await detect_login_page_blocker(page, platform)
                if blocker:
                    await settle_login_session_without_account(
                        session_id,
                        status="failed",
                        error_message=blocker["error_message"],
                        event_type="QrLoginFailed",
                        error_code=blocker["error_code"],
                    )
                    return
            if not qr_ready:
                if await complete_authenticated_login_if_ready(
                    page,
                    ctx,
                    platform,
                    required,
                    session_id,
                ):
                    return
                blocker = await detect_login_page_blocker(page, platform)
                if blocker:
                    await settle_login_session_without_account(
                        session_id,
                        status="failed",
                        error_message=blocker["error_message"],
                        event_type="QrLoginFailed",
                        error_code=blocker["error_code"],
                    )
                    return
                owner_state = await login_session_owner_state(
                    session_id,
                    expected_status="opening",
                )
                if owner_state == "expired":
                    await settle_login_session_without_account(
                        session_id,
                        status="expired",
                        error_message="QR login session expired",
                        event_type="QrLoginExpired",
                        error_code="qr_login_session_expired",
                    )
                    return
                if owner_state == "released":
                    structured_log(
                        "info",
                        "qr_login_owner_released",
                        session_id=session_id,
                        platform=platform,
                        state=owner_state,
                    )
                    return
                await settle_login_session_without_account(
                    session_id,
                    status="failed",
                    error_message=(
                        "Xiaohongshu did not expose a login QR code; "
                        "complete any required verification in the official "
                        "app or site, then retry"
                    ),
                    event_type="QrLoginFailed",
                    error_code="xiaohongshu_login_qr_not_found",
                )
                return
        else:
            qr_ready = await capture_login_qr_image(
                page,
                platform,
                image_path,
                wait_milliseconds=1,
            )
        if not await transition_login_session_to_waiting_scan(
            session_id,
            platform=platform,
            qr_image_path=str(image_path),
        ):
            return

        deadline = (
            asyncio.get_running_loop().time()
            + float(claim["remaining_seconds"])
        )
        while asyncio.get_running_loop().time() < deadline:
            owner_state = await login_session_owner_state(
                session_id,
                expected_status="waiting_scan",
            )
            if owner_state != "owned":
                if owner_state == "expired":
                    await settle_login_session_without_account(
                        session_id,
                        status="expired",
                        error_message="QR login session expired",
                        event_type="QrLoginExpired",
                        error_code="qr_login_session_expired",
                    )
                structured_log(
                    "info",
                    "qr_login_owner_released",
                    session_id=session_id,
                    platform=platform,
                    state=owner_state,
                )
                return
            blocker = await detect_login_page_blocker(page, platform)
            if blocker:
                await settle_login_session_without_account(
                    session_id,
                    status="failed",
                    error_message=blocker["error_message"],
                    event_type="QrLoginFailed",
                    error_code=blocker["error_code"],
                )
                return
            if await complete_authenticated_login_if_ready(
                page,
                ctx,
                platform,
                required,
                session_id,
            ):
                return
            await capture_login_qr_image(
                page,
                platform,
                image_path,
                wait_milliseconds=(
                    XIAOHONGSHU_QR_POLL_MILLISECONDS
                    if platform == "xiaohongshu"
                    else 1
                ),
            )
            await asyncio.sleep(3)

        await settle_login_session_without_account(
            session_id,
            status="expired",
            error_message="QR login session expired",
            event_type="QrLoginExpired",
            error_code="qr_login_session_expired",
        )
    except asyncio.CancelledError:
        raise
    except Exception as e:
        error_type = str(type(e).__name__ or "Exception")[:64]
        await settle_login_session_without_account(
            session_id,
            status="failed",
            error_message=f"QR login failed ({error_type})",
            event_type="QrLoginFailed",
            error_code=f"qr_login_failed:{error_type}",
        )
        structured_log("error", "qr_login_failed", exception=e)
    finally:
        if page is not None:
            try:
                await page.close()
            except Exception as exc:
                structured_log(
                    "error",
                    "qr_login_page_close_failed",
                    exception=exc,
                )
        if ctx is not None:
            try:
                close_tracked = getattr(
                    pool,
                    "close_transient_context",
                    None,
                )
                if close_tracked is not None:
                    closed = await close_tracked(
                        ctx,
                        reason="qr_login_finished",
                    )
                    if closed is not True:
                        raise RuntimeError(
                            "qr_login_context_close_unconfirmed"
                        )
                else:
                    await ctx.close()
            except Exception as exc:
                structured_log(
                    "error",
                    "qr_login_context_close_failed",
                    exception=exc,
                )
                raise


async def create_account_from_cookies(
    platform: str,
    cookies: list[dict],
    *,
    session_id: str | None = None,
) -> dict:
    credential = cookie_vault.encrypt(serialize_cookies(platform, cookies), aad=CREDENTIAL_AAD)
    try:
        async with database.transaction():
            if session_id is not None:
                session = await database.fetch_one(
                    """SELECT status, account_id, platform, expires_at,
                              (
                                expires_at IS NOT NULL
                                AND expires_at > NOW()
                              ) AS not_expired
                       FROM login_sessions
                       WHERE session_id = :session_id
                       FOR UPDATE""",
                    {"session_id": session_id},
                )
                if not session:
                    raise RuntimeError("login_session_missing")
                if str(session["platform"] or "") != str(platform):
                    raise RuntimeError("login_session_platform_mismatch")
                session_status = str(
                    session["status"] or ""
                ).strip().casefold()
                if session_status == "confirmed" and session["account_id"]:
                    await enqueue_login_profile_cleanup(session_id)
                    return {
                        "account_id": int(session["account_id"]),
                        "calibration": None,
                        "created": False,
                        "status": "confirmed",
                    }
                if session_status not in {
                    "queued",
                    "opening",
                    "waiting_scan",
                }:
                    raise RuntimeError("login_session_not_bindable")
                if int(session["not_expired"] or 0) != 1:
                    raise LoginSessionExpired(
                        "login_session_expired"
                    )
            fingerprint_id = await ensure_default_fingerprint(platform)
            account_id = await database.execute(
                """INSERT INTO accounts
                     (platform, fingerprint_id, encrypted_credential, status)
                   VALUES
                     (:platform, :fingerprint_id, :credential, 'warming')""",
                {
                    "platform": platform,
                    "fingerprint_id": fingerprint_id,
                    "credential": credential,
                },
            )
            calibration = await queue_account_calibration(
                account_id,
                platform,
            )
            if session_id is not None:
                await database.execute(
                    """UPDATE login_sessions
                       SET status = 'confirmed',
                           account_id = :account_id,
                           error_message = NULL,
                           completed_at = NOW(),
                           updated_at = NOW()
                       WHERE session_id = :session_id
                         AND status IN (
                           'queued',
                           'opening',
                           'waiting_scan'
                         )
                         AND expires_at IS NOT NULL
                         AND expires_at > NOW()""",
                    {
                        "session_id": session_id,
                        "account_id": account_id,
                    },
                )
                affected = await database.fetch_one(
                    "SELECT ROW_COUNT() AS affected"
                )
                if (
                    affected is None
                    or int(affected["affected"] or 0) != 1
                ):
                    raise LoginSessionExpired(
                        "login_session_expired_before_bind"
                    )
                await enqueue_login_profile_cleanup(session_id)
                await record_event(
                    aggregate="browser",
                    aggregate_id=session_id,
                    event_type="QrLoginCompleted",
                    payload={
                        "platform": platform,
                        "account_id": account_id,
                        "calibration": calibration,
                    },
                    correlation_id=session_id,
                )
                await record_event(
                    aggregate="account",
                    aggregate_id=account_id,
                    event_type="AccountCreated",
                    payload={
                        "platform": platform,
                        "credential_imported": True,
                        "source": "qr_login",
                        "calibration": calibration,
                    },
                    correlation_id=session_id,
                )
    except LoginSessionExpired:
        await settle_login_session_without_account(
            str(session_id or ""),
            status="expired",
            error_message="QR login session expired",
            event_type="QrLoginExpired",
            error_code="qr_login_session_expired",
        )
        return {
            "account_id": None,
            "calibration": None,
            "created": False,
            "status": "expired",
        }
    return {
        "account_id": account_id,
        "calibration": calibration,
        "created": True,
        "status": "confirmed" if session_id is not None else "created",
    }


async def queue_account_calibration(account_id: int, platform: str):
    cfg = get_platform(platform)
    calibration_id = str(uuid.uuid4())
    check_url = cfg.get("account_check_url") or cfg["login_url"]
    message = build_account_calibration_message(
        calibration_id=calibration_id,
        platform=platform,
        account_id=account_id,
        check_url=check_url,
        calibration_kind="browser_session",
        fallback_account_status="login_required",
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
