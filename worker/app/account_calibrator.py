import asyncio
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import uuid
from urllib.parse import urlparse, urlunparse

from app.account_calibration_streams import (
    ACCOUNT_CALIBRATION_OUTBOX_DEDUP_PREFIX,
    LEGACY_ACCOUNT_CALIBRATION_FANOUT_CONSUMER_NAME,
    LEGACY_ACCOUNT_CALIBRATION_FANOUT_GROUP_NAME,
    LEGACY_ACCOUNT_CALIBRATION_GROUP_NAME,
    LEGACY_ACCOUNT_CALIBRATION_STREAM_BINDING,
    LEGACY_ACCOUNT_CALIBRATION_STREAM_KEY,
    SAFE_ACCOUNT_FALLBACK_STATUSES,
    AccountCalibrationStreamBinding,
    account_calibration_stream_binding_for_platform,
    account_calibration_stream_bindings,
    validate_account_calibration_stream_message,
)
from app.config import settings
from app.db import database, execute_affected_rows, redis
from app.event_store.service import record_event
from app.platform_modules.registry import registered_platforms
from app.platforms import get_platform
from app.runtime_lane_health import (
    RUNTIME_LANE_PROGRESS_INTERVAL_SECONDS,
    record_runtime_lane_failure,
    record_runtime_lane_progress,
    record_runtime_lane_success,
)
from app.safety import detect_page_risk
from app.task_streams import SAFE_TERMINAL_STREAM_ACK_DELETE_LUA
from app.utils.cookies import inject_account_cookies
from app.utils.crypto import CREDENTIAL_AAD, cookie_vault
from app.utils.log import structured_log
from app.worker_identity import WORKER_ID
from shared.platform_scope import normalize_platform_scope
from shared.platform_ids import PLATFORM_IDS
from shared.douyin_device_contract import (
    DOUYIN_DEVICE_ACTION_ORDER,
    DOUYIN_DEVICE_CALIBRATION_CHECK_URL,
    parse_douyin_device_credential,
)
from shared.redis_consumer_groups import verify_redis_consumer_group
# Compatibility aliases for older tests and operators. Production consumers
# use the binding passed to each isolated platform lane.
STREAM_KEY = LEGACY_ACCOUNT_CALIBRATION_STREAM_KEY
GROUP_NAME = LEGACY_ACCOUNT_CALIBRATION_GROUP_NAME
# Keep the Redis consumer identity exactly aligned with the worker heartbeat
# identity. Streams/groups already isolate platforms, so a platform suffix is
# unnecessary and would make pending ownership impossible to join precisely
# with worker_heartbeats.
CONSUMER_NAME = WORKER_ID
PROFILES_ROOT = Path("/profiles")
CALIBRATION_SCREENSHOT_DIR_NAME = "account-calibrations"
WEIBO_OAUTH_IDENTITY_URL = "https://api.weibo.com/2/account/get_uid.json"
MAX_CALIBRATION_URL_LENGTH = 1024
CALIBRATION_IDLE_THRESHOLD_MS = 300_000
CALIBRATION_RECOVERY_INTERVAL_SECONDS = 60
CALIBRATION_EXECUTION_TIMEOUT_SECONDS = 180
CALIBRATION_RECOVERY_BATCH = 20
CALIBRATION_REQUEUE_DEDUP_SECONDS = 600
# Keep delivery bounded while allowing one serial execution lane per platform.
# Entries waiting for their lane remain in the consumer PEL and are refreshed
# below so the stale-owner recovery path cannot race a live local waiter.
CALIBRATION_DISPATCH_MAX_INFLIGHT = 32
CALIBRATION_STREAM_READ_COUNT = 8
CALIBRATION_PENDING_REFRESH_SECONDS = 30
CALIBRATION_LANE_RESOLUTION_TIMEOUT_SECONDS = 5
LEGACY_CALIBRATION_FANOUT_READ_COUNT = 50


def calibration_screenshot_path(
    platform: str,
    calibration_id: str,
    *,
    profiles_root: Path | None = None,
) -> Path:
    normalized_platform = str(platform or "").strip().casefold()
    if normalized_platform not in PLATFORM_IDS:
        raise ValueError("account_calibration_platform_invalid")
    raw_calibration_id = str(calibration_id or "").strip()
    try:
        parsed_id = uuid.UUID(raw_calibration_id)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("account_calibration_id_invalid") from exc
    if str(parsed_id) != raw_calibration_id:
        raise ValueError("account_calibration_id_invalid")
    root = Path(profiles_root or PROFILES_ROOT)
    if not root.is_absolute():
        raise ValueError("account_calibration_profiles_root_not_absolute")
    return (
        root
        / normalized_platform
        / CALIBRATION_SCREENSHOT_DIR_NAME
        / f"{parsed_id}.png"
    )
LEGACY_CALIBRATION_FANOUT_BLOCK_MS = 1000
LEGACY_CALIBRATION_FANOUT_MARKER_SECONDS = 7 * 24 * 60 * 60
CALIBRATION_ID_PATTERN = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z",
    re.ASCII,
)


class CalibrationClaimRejected(RuntimeError):
    """A stale, duplicate, or forged calibration message lost the CAS."""


def _parsed_safe_https_url(value: str):
    target = str(value or "").strip()
    if (
        not target
        or len(target) > MAX_CALIBRATION_URL_LENGTH
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in target)
    ):
        raise ValueError("account_calibration_navigation_target_not_allowed")
    parsed = urlparse(target)
    if (
        parsed.scheme.lower() != "https"
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("account_calibration_navigation_target_not_allowed")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("account_calibration_navigation_target_not_allowed") from exc
    host = (parsed.hostname or "").rstrip(".").lower()
    if not host or port not in (None, 443):
        raise ValueError("account_calibration_navigation_target_not_allowed")
    return target, parsed, host


def _normalized_exact_https_url(value: str) -> str:
    _target, parsed, host = _parsed_safe_https_url(value)
    if parsed.params or parsed.query or parsed.fragment:
        raise ValueError("account_calibration_navigation_target_not_allowed")
    path = parsed.path or "/"
    return urlunparse(("https", host, path, "", "", ""))


def calibration_browser_urls(platform: str) -> tuple[frozenset[str], frozenset[str]]:
    """Return the manifest-owned browser entry URLs and redirect hosts."""

    cfg = get_platform(str(platform or "").strip().lower())
    if not cfg:
        raise ValueError("account_calibration_navigation_target_not_allowed")
    entries = set()
    hosts = set()
    for key in ("account_check_url", "login_url"):
        value = cfg.get(key)
        if not value:
            continue
        entries.add(_normalized_exact_https_url(value))
        _target, _parsed, host = _parsed_safe_https_url(value)
        hosts.add(host)
    if not entries or not hosts:
        raise ValueError("account_calibration_navigation_target_not_allowed")
    return frozenset(entries), frozenset(hosts)


def validated_calibration_browser_entry_url(platform: str, value: str) -> str:
    """Accept only an exact browser entry URL declared by the manifest."""

    entries, _hosts = calibration_browser_urls(platform)
    normalized = _normalized_exact_https_url(value)
    if normalized not in entries:
        raise ValueError("account_calibration_navigation_target_not_allowed")
    return str(value).strip()


def validated_calibration_browser_navigation_url(platform: str, value: str) -> str:
    """Allow redirects only to manifest-owned hosts for this platform."""

    target, _parsed, host = _parsed_safe_https_url(value)
    _entries, hosts = calibration_browser_urls(platform)
    if host not in hosts:
        raise ValueError("account_calibration_navigation_target_not_allowed")
    return target


async def install_calibration_navigation_guard(page, platform: str) -> None:
    """Abort disallowed main-frame redirects before credentials reach them."""

    main_frame = page.main_frame

    async def guard(route):
        request = route.request
        if request.is_navigation_request() and request.frame == main_frame:
            try:
                validated_calibration_browser_navigation_url(
                    platform, request.url
                )
            except ValueError:
                await route.abort()
                return
        await route.continue_()

    await page.route("**/*", guard)


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
    queued_platform: str | None = None,
    queued_check_url=None,
) -> dict | None:
    """Atomically bind and claim one exact queued calibration message.

    Redis is only a delivery hint.  The calibration row (including its target
    URL) and the joined account row are the authority.  A message that supplies
    a different URL is rejected before any browser or credential is touched;
    old messages that omitted ``check_url`` remain compatible.
    """

    try:
        async with database.transaction():
            row = await database.fetch_one(
                """SELECT c.calibration_id, c.account_id, c.platform,
                          c.check_url, c.result AS staged_result,
                          c.status AS calibration_status,
                          a.platform AS account_platform,
                          a.execution_revision AS account_execution_revision
                     FROM account_calibrations c
                     JOIN accounts a ON a.id = c.account_id
                    WHERE c.calibration_id = :calibration_id
                      AND a.deleted_at IS NULL
                    FOR UPDATE""",
                {"calibration_id": calibration_id},
            )
            authoritative_platform = (
                str(row["platform"] or "").strip().lower() if row else ""
            )
            authoritative_check_url = (
                str(row["check_url"] or "").strip() if row else ""
            )
            account_platform = (
                str(row["account_platform"] or "").strip().lower()
                if row
                else ""
            )
            try:
                execution_revision = (
                    int(row["account_execution_revision"] or 0) if row else 0
                )
            except (TypeError, ValueError):
                execution_revision = 0
            normalized_queued_platform = (
                str(queued_platform).strip().lower()
                if queued_platform is not None
                else None
            )
            if (
                not row
                or str(row["calibration_id"] or "").strip() != calibration_id
                or int(row["account_id"] or 0) != account_id
                or account_platform != authoritative_platform
                or execution_revision <= 0
                or (
                    normalized_queued_platform is not None
                    and normalized_queued_platform != authoritative_platform
                )
                or str(row["calibration_status"] or "").strip().lower()
                != "queued"
                or not authoritative_check_url
                or (
                    queued_check_url is not None
                    and str(queued_check_url).strip() != authoritative_check_url
                )
            ):
                raise CalibrationClaimRejected(
                    "account_calibration_authoritative_binding_mismatch"
                )
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
                    "platform": authoritative_platform,
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
            return {
                "calibration_id": calibration_id,
                "account_id": account_id,
                "platform": authoritative_platform,
                "check_url": authoritative_check_url,
                "staged_result": row["staged_result"],
                "execution_revision": execution_revision,
            }
    except CalibrationClaimRejected:
        return None


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


def _calibration_message_binding(task: dict) -> dict | None:
    """Parse only identifiers needed to bind a Redis delivery to its DB row."""

    calibration_id = str(task.get("calibration_id") or "").strip()
    if not CALIBRATION_ID_PATTERN.fullmatch(calibration_id):
        return None
    try:
        account_id = int(task.get("account_id"))
    except (TypeError, ValueError):
        return None
    if account_id <= 0:
        return None
    queued_platform = (
        str(task.get("platform") or "").strip().lower()
        if "platform" in task
        else None
    )
    queued_check_url = (
        str(task.get("check_url") or "").strip()
        if "check_url" in task
        else None
    )
    return {
        "calibration_id": calibration_id,
        "account_id": account_id,
        "queued_platform": queued_platform,
        "queued_check_url": queued_check_url,
    }


def _calibration_task_from_row(row) -> dict:
    return {
        "calibration_id": str(row["calibration_id"] or "").strip(),
        "account_id": str(int(row["account_id"] or 0)),
        "platform": str(row["platform"] or "").strip().lower(),
        "check_url": str(row["check_url"] or "").strip(),
    }


async def reconcile_calibration_message_state(
    task: dict,
    *,
    stale_running_only: bool,
    failure_reason: str,
) -> str:
    """Return the authoritative state, terminally closing abandoned work.

    A reclaimed ``queued`` message is safe to replay because the DB claim CAS
    happens before browser/API access. A ``running`` message is never replayed:
    it is failed only after its bounded execution window has expired (or during
    this process' own graceful cancellation). This avoids concurrent credential
    use even when a former Redis consumer resumes after XCLAIM.
    """

    binding = _calibration_message_binding(task)
    if binding is None:
        return "invalid"

    failed_binding = None
    async with database.transaction():
        row = await database.fetch_one(
            """SELECT c.calibration_id, c.account_id, c.platform, c.check_url,
                      c.status AS calibration_status,
                      a.platform AS account_platform,
                      a.deleted_at AS account_deleted_at,
                      a.execution_revision AS account_execution_revision,
                      CASE WHEN c.started_at IS NULL
                                  OR c.started_at < (NOW() - INTERVAL 5 MINUTE)
                           THEN 1 ELSE 0 END AS stale_running
                 FROM account_calibrations c
                 LEFT JOIN accounts a ON a.id = c.account_id
                WHERE c.calibration_id = :calibration_id
                FOR UPDATE""",
            {"calibration_id": binding["calibration_id"]},
        )
        if not row:
            return "missing"

        authoritative_platform = str(row["platform"] or "").strip().lower()
        authoritative_check_url = str(row["check_url"] or "").strip()
        if (
            int(row["account_id"] or 0) != binding["account_id"]
            or (
                binding["queued_platform"] is not None
                and binding["queued_platform"] != authoritative_platform
            )
            or (
                binding["queued_check_url"] is not None
                and binding["queued_check_url"] != authoritative_check_url
            )
        ):
            # The claimed stream entry is forged or obsolete. ACKing that entry
            # is safe, but it must not mutate the row whose binding it failed.
            return "binding_mismatch"

        status = str(row["calibration_status"] or "").strip().lower()
        if status in {"succeeded", "failed"}:
            return "terminal"
        if status not in {"queued", "running"}:
            return "active"

        try:
            account_execution_revision = int(
                row["account_execution_revision"] or 0
            )
        except (TypeError, ValueError):
            account_execution_revision = 0
        account_authority_invalid = (
            not str(row["account_platform"] or "").strip()
            or row["account_deleted_at"] is not None
            or str(row["account_platform"] or "").strip().lower()
            != authoritative_platform
            or account_execution_revision <= 0
            or not authoritative_check_url
        )
        if status == "queued" and not account_authority_invalid:
            return "queued"
        if (
            status == "running"
            and stale_running_only
            and int(row["stale_running"] or 0) != 1
            and not account_authority_invalid
        ):
            return "active"

        effective_reason = (
            "account calibration authority became invalid"
            if account_authority_invalid
            else failure_reason
        )
        updated = await execute_affected_rows(
            """UPDATE account_calibrations
                  SET status = 'failed', error_message = :error,
                      finished_at = NOW()
                WHERE calibration_id = :calibration_id
                  AND account_id = :account_id
                  AND platform = :platform
                  AND status = :status""",
            {
                "calibration_id": binding["calibration_id"],
                "account_id": binding["account_id"],
                "platform": authoritative_platform,
                "status": status,
                "error": effective_reason,
            },
            db=database,
        )
        if updated != 1:
            return "active"
        failed_binding = {
            "calibration_id": binding["calibration_id"],
            "account_id": binding["account_id"],
            "platform": authoritative_platform,
            "error": effective_reason,
        }

    await emit_calibration_terminal_observability(
        account_id=failed_binding["account_id"],
        calibration_id=failed_binding["calibration_id"],
        platform=failed_binding["platform"],
        status="failed",
        content=(
            f"Account A{failed_binding['account_id']} calibration for "
            f"{failed_binding['platform']} was closed by worker recovery. "
            "Queue a fresh calibration before using this account."
        ),
        severity="warning",
        event_type="AccountCalibrationFailed",
        event_payload={
            "platform": failed_binding["platform"],
            "calibration_id": failed_binding["calibration_id"],
            "error": failed_binding["error"],
            "recovery": True,
        },
    )
    return "failed"


RECOVERY_ACKABLE_STATES = frozenset(
    {"invalid", "missing", "binding_mismatch", "terminal", "failed"}
)


async def _ack_terminal_calibration_message(
    binding: AccountCalibrationStreamBinding,
    message_id: str,
) -> None:
    """ACK and retire a terminal live-lane entry after every group advances."""

    if binding.legacy:
        await redis.xack(binding.stream_key, binding.group_name, message_id)
        return
    await redis.eval(
        SAFE_TERMINAL_STREAM_ACK_DELETE_LUA,
        1,
        binding.stream_key,
        binding.group_name,
        str(message_id),
    )


def calibration_consumer_name(
    binding: AccountCalibrationStreamBinding,
) -> str:
    return CONSUMER_NAME


async def process_calibration_message(
    pool,
    msg_id: str,
    data: dict,
    *,
    binding: AccountCalibrationStreamBinding = (
        LEGACY_ACCOUNT_CALIBRATION_STREAM_BINDING
    ),
) -> None:
    """Execute one delivery and ACK only after a terminal/irrelevant outcome."""

    task = {key: value for key, value in data.items()}
    try:
        await asyncio.wait_for(
            handle_calibration(pool, task),
            timeout=CALIBRATION_EXECUTION_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        state = await reconcile_calibration_message_state(
            task,
            stale_running_only=False,
            failure_reason="account calibration execution deadline exceeded",
        )
        if state in RECOVERY_ACKABLE_STATES:
            await _ack_terminal_calibration_message(binding, msg_id)
        else:
            structured_log(
                "error",
                "account_calibration_timeout_not_terminal",
                task_id=task.get("calibration_id"),
                state=state,
            )
        return
    except asyncio.CancelledError:
        try:
            state = await asyncio.shield(
                reconcile_calibration_message_state(
                    task,
                    stale_running_only=False,
                    failure_reason="account calibration cancelled during worker shutdown",
                )
            )
            if state in RECOVERY_ACKABLE_STATES:
                await asyncio.shield(
                    _ack_terminal_calibration_message(binding, msg_id)
                )
        except Exception as exc:
            structured_log(
                "error",
                "account_calibration_shutdown_reconciliation_failed",
                task_id=task.get("calibration_id"),
                exception=exc,
            )
        raise
    # ``handle_calibration`` returns normally both after terminal settlement
    # and when its queued->running claim loses a race. Re-read the locked DB
    # authority before ACKing so a fresh queued/running row remains in the PEL,
    # while a deleted/mismatched account is failed atomically and can be ACKed.
    state = await reconcile_calibration_message_state(
        task,
        stale_running_only=True,
        failure_reason=(
            "account calibration handler returned without terminal settlement"
        ),
    )
    if state in RECOVERY_ACKABLE_STATES:
        await _ack_terminal_calibration_message(binding, msg_id)
    else:
        structured_log(
            "warning",
            "account_calibration_delivery_remains_pending",
            task_id=task.get("calibration_id"),
            message_id=msg_id,
            state=state,
        )


async def reclaim_stale_calibration_messages(
    pool,
    platform_locks: dict[str, asyncio.Lock] | None = None,
    *,
    binding: AccountCalibrationStreamBinding = (
        LEGACY_ACCOUNT_CALIBRATION_STREAM_BINDING
    ),
) -> int:
    """Claim stale PEL entries and recover them from authoritative DB state."""

    pending = await redis.xpending_range(
        binding.stream_key,
        binding.group_name,
        min="-",
        max="+",
        count=CALIBRATION_RECOVERY_BATCH,
        idle=CALIBRATION_IDLE_THRESHOLD_MS,
    )
    acknowledged = 0
    for entry in pending or []:
        message_id = entry.get("message_id")
        if not message_id:
            continue
        claimed = await redis.xclaim(
            binding.stream_key,
            binding.group_name,
            calibration_consumer_name(binding),
            min_idle_time=CALIBRATION_IDLE_THRESHOLD_MS,
            message_ids=[message_id],
        )
        if not claimed:
            continue
        claimed_id, fields = claimed[0]
        task = {key: value for key, value in (fields or {}).items()}
        try:
            validate_account_calibration_stream_message(binding, task)
        except ValueError as exc:
            await _ack_terminal_calibration_message(binding, claimed_id)
            acknowledged += 1
            structured_log(
                "error",
                "account_calibration_recovery_envelope_rejected",
                message_id=claimed_id,
                stream=binding.stream_key,
                platform=binding.platform or "legacy",
                error=str(exc),
            )
            continue
        state = await reconcile_calibration_message_state(
            task,
            stale_running_only=True,
            failure_reason=(
                "stale account calibration owner lost; explicit retry required"
            ),
        )
        if state == "queued":
            try:
                if platform_locks is None:
                    await asyncio.wait_for(
                        handle_calibration(pool, task),
                        timeout=CALIBRATION_EXECUTION_TIMEOUT_SECONDS,
                    )
                else:
                    lane = await asyncio.wait_for(
                        _resolve_calibration_platform_for_dispatch(task),
                        timeout=CALIBRATION_LANE_RESOLUTION_TIMEOUT_SECONDS,
                    )
                    async with platform_locks[lane]:
                        await asyncio.wait_for(
                            handle_calibration(pool, task),
                            timeout=CALIBRATION_EXECUTION_TIMEOUT_SECONDS,
                        )
            except asyncio.TimeoutError:
                state = await reconcile_calibration_message_state(
                    task,
                    stale_running_only=False,
                    failure_reason=(
                        "reclaimed account calibration execution deadline exceeded"
                    ),
                )
            else:
                # A competing former consumer may have won the queued->running
                # CAS immediately before our replay. In that case keep the PEL
                # entry until its DB state becomes terminal or stale.
                state = await reconcile_calibration_message_state(
                    task,
                    stale_running_only=True,
                    failure_reason=(
                        "stale account calibration owner lost; explicit retry required"
                    ),
                )
        if state not in RECOVERY_ACKABLE_STATES:
            structured_log(
                "warning",
                "account_calibration_reclaimed_but_active",
                task_id=task.get("calibration_id"),
                message_id=claimed_id,
                state=state,
            )
            continue
        await _ack_terminal_calibration_message(binding, claimed_id)
        acknowledged += 1
        structured_log(
            "warning",
            "account_calibration_stale_message_settled",
            task_id=task.get("calibration_id"),
            message_id=claimed_id,
            state=state,
        )
    return acknowledged


async def requeue_stale_queued_calibrations(
    platform: str | None = None,
) -> int:
    """Repair DB queued rows whose non-durable Redis delivery disappeared."""

    normalized_platform = (
        str(platform or "").strip().casefold() if platform is not None else None
    )
    platform_scope = (
        " AND c.platform = :platform" if normalized_platform is not None else ""
    )
    index_hint = (
        " FORCE INDEX (idx_account_calibration_platform_queued)"
        if normalized_platform is not None
        else ""
    )
    values = {"limit": CALIBRATION_RECOVERY_BATCH}
    if normalized_platform is not None:
        account_calibration_stream_binding_for_platform(normalized_platform)
        values["platform"] = normalized_platform
    rows = await database.fetch_all(
        f"""SELECT c.calibration_id, c.account_id, c.platform, c.check_url,
                   a.platform AS account_platform,
                   a.deleted_at AS account_deleted_at,
                   a.execution_revision AS account_execution_revision
              FROM account_calibrations c{index_hint}
              LEFT JOIN accounts a ON a.id = c.account_id
             WHERE c.status = 'queued'
               AND c.created_at < (NOW() - INTERVAL 5 MINUTE)
               {platform_scope}
               AND NOT EXISTS (
                 SELECT 1
                   FROM outbox_events delivery
                  WHERE delivery.dedup_key =
                        CONCAT(:dedup_prefix, c.calibration_id)
               )
             ORDER BY c.created_at, c.id
             LIMIT :limit""",
        {
            **values,
            "dedup_prefix": ACCOUNT_CALIBRATION_OUTBOX_DEDUP_PREFIX,
        },
    )
    requeued = 0
    for row in rows or []:
        task = _calibration_task_from_row(row)
        state = await reconcile_calibration_message_state(
            task,
            stale_running_only=True,
            failure_reason="account calibration authority became invalid",
        )
        if state != "queued":
            continue
        marker = f"account_calibration_requeue:{task['calibration_id']}"
        reserved = await redis.set(
            marker,
            "1",
            nx=True,
            ex=CALIBRATION_REQUEUE_DEDUP_SECONDS,
        )
        if not reserved:
            continue
        binding = account_calibration_stream_binding_for_platform(
            task["platform"]
        )
        validate_account_calibration_stream_message(binding, task)
        try:
            await redis.xadd(binding.stream_key, task)
        except Exception:
            try:
                await redis.delete(marker)
            except Exception as cleanup_exc:
                structured_log(
                    "warning",
                    "account_calibration_requeue_marker_cleanup_failed",
                    task_id=task["calibration_id"],
                    exception=cleanup_exc,
                )
            raise
        requeued += 1
        structured_log(
            "warning",
            "account_calibration_orphan_requeued",
            task_id=task["calibration_id"],
            platform=task["platform"],
        )
    return requeued


async def expire_stale_running_calibrations(
    platform: str | None = None,
) -> int:
    """Fail bounded executions that outlived both owner and execution window."""

    normalized_platform = (
        str(platform or "").strip().casefold() if platform is not None else None
    )
    platform_scope = (
        " AND c.platform = :platform" if normalized_platform is not None else ""
    )
    index_hint = (
        " FORCE INDEX (idx_account_calibration_platform_running)"
        if normalized_platform is not None
        else ""
    )
    values = {"limit": CALIBRATION_RECOVERY_BATCH}
    if normalized_platform is not None:
        account_calibration_stream_binding_for_platform(normalized_platform)
        values["platform"] = normalized_platform
    rows = await database.fetch_all(
        f"""SELECT calibration_id, account_id, platform, check_url
              FROM account_calibrations c{index_hint}
             WHERE c.status = 'running'
               AND (c.started_at IS NULL
                    OR c.started_at < (NOW() - INTERVAL 5 MINUTE))
               {platform_scope}
             ORDER BY c.started_at, c.created_at, c.id
             LIMIT :limit""",
        values,
    )
    expired = 0
    for row in rows or []:
        state = await reconcile_calibration_message_state(
            _calibration_task_from_row(row),
            stale_running_only=True,
            failure_reason=(
                "stale account calibration owner lost; explicit retry required"
            ),
        )
        if state == "failed":
            expired += 1
    return expired


async def recover_calibration_state(
    pool,
    platform_locks: dict[str, asyncio.Lock] | None = None,
    *,
    binding: AccountCalibrationStreamBinding = (
        LEGACY_ACCOUNT_CALIBRATION_STREAM_BINDING
    ),
) -> None:
    """Run independent recovery phases so one backend failure cannot mask all."""

    phases = (
        (
            "pel",
            lambda: reclaim_stale_calibration_messages(
                pool,
                platform_locks,
                binding=binding,
            ),
        ),
        (
            "queued",
            lambda: requeue_stale_queued_calibrations(binding.platform),
        ),
        (
            "running",
            lambda: expire_stale_running_calibrations(binding.platform),
        ),
    )
    for phase, recover in phases:
        try:
            await recover()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            structured_log(
                "error",
                "account_calibration_recovery_phase_failed",
                phase=phase,
                exception=exc,
            )


async def _calibration_recovery_loop(
    pool,
    shutdown_event: asyncio.Event,
    platform_locks: dict[str, asyncio.Lock] | None = None,
    *,
    binding: AccountCalibrationStreamBinding = (
        LEGACY_ACCOUNT_CALIBRATION_STREAM_BINDING
    ),
) -> None:
    """Run stale-state repair independently from live platform ingestion."""

    while not shutdown_event.is_set():
        try:
            await recover_calibration_state(
                pool,
                platform_locks,
                binding=binding,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            structured_log(
                "error",
                "account_calibration_recovery_loop_failed",
                stream=binding.stream_key,
                platform=binding.platform or "legacy",
                exception=exc,
            )
        try:
            await asyncio.wait_for(
                shutdown_event.wait(),
                timeout=CALIBRATION_RECOVERY_INTERVAL_SECONDS,
            )
        except asyncio.TimeoutError:
            pass


@dataclass
class _DispatchedCalibrationMessage:
    message_id: str
    data: dict
    waiting_for_platform: bool = True


def _calibration_platform_for_dispatch(data: dict) -> str:
    """Choose only a local scheduling lane; DB claim remains authoritative."""

    platform = str(data.get("platform") or "").strip().lower()
    if platform in registered_platforms():
        return platform
    # Legacy messages omitted platform, and malformed messages can supply an
    # unknown value. Isolate both from the four normal platform lanes; claim
    # validation still decides whether they are actionable or ACK-safe.
    return "__legacy_or_invalid__"


async def _resolve_calibration_platform_for_dispatch(data: dict) -> str:
    """Resolve a legacy platform omission before choosing an execution lock.

    Legacy messages are compatible only after their calibration/account binding
    identifies one registered platform. The resolved platform is copied into
    the in-memory delivery so the later authoritative claim must match it. This
    prevents a legacy lane from running concurrently with the same platform's
    normal lane.
    """

    lane = _calibration_platform_for_dispatch(data)
    if lane != "__legacy_or_invalid__":
        return lane
    if str(data.get("platform") or "").strip():
        return lane
    calibration_id = str(data.get("calibration_id") or "").strip()
    try:
        account_id = int(data.get("account_id"))
    except (TypeError, ValueError):
        return lane
    if not CALIBRATION_ID_PATTERN.fullmatch(calibration_id) or account_id <= 0:
        return lane
    row = await database.fetch_one(
        """SELECT c.calibration_id, c.account_id, c.platform,
                  a.platform AS account_platform
             FROM account_calibrations c
             JOIN accounts a ON a.id = c.account_id
            WHERE c.calibration_id = :calibration_id
              AND c.account_id = :account_id
              AND a.deleted_at IS NULL""",
        {"calibration_id": calibration_id, "account_id": account_id},
    )
    if not row:
        return lane
    authoritative_platform = str(row["platform"] or "").strip().lower()
    account_platform = str(row["account_platform"] or "").strip().lower()
    if (
        str(row["calibration_id"] or "").strip() != calibration_id
        or int(row["account_id"] or 0) != account_id
        or authoritative_platform != account_platform
        or authoritative_platform not in registered_platforms()
    ):
        return lane
    data["platform"] = authoritative_platform
    return authoritative_platform


async def _execute_dispatched_calibration(
    dispatched: _DispatchedCalibrationMessage,
    binding: AccountCalibrationStreamBinding,
    platform_lock: asyncio.Lock,
    pool,
) -> None:
    try:
        async with platform_lock:
            dispatched.waiting_for_platform = False
            await process_calibration_message(
                pool,
                dispatched.message_id,
                dispatched.data,
                binding=binding,
            )
    except asyncio.CancelledError:
        # ``process_calibration_message`` terminally reconciles a calibration
        # that had started. A task cancelled while waiting for the lock never
        # reaches it and deliberately leaves its queued entry in the PEL.
        structured_log(
            "info",
            "account_calibration_cancelled",
            task_id=dispatched.data.get("calibration_id"),
            message_id=dispatched.message_id,
            lane=binding.platform,
        )
        raise
    except Exception as exc:
        # Settlement/ACK failures stay pending and are handled by the bounded
        # stale-owner recovery path without stopping sibling platform lanes.
        structured_log(
            "error",
            "account_calibration_dispatch_error",
            task_id=dispatched.data.get("calibration_id"),
            message_id=dispatched.message_id,
            lane=binding.platform,
            stream=binding.stream_key,
            exception=exc,
        )


async def _refresh_waiting_calibration_entries(
    inflight: dict[asyncio.Task, _DispatchedCalibrationMessage],
    shutdown_event: asyncio.Event,
    binding: AccountCalibrationStreamBinding = (
        LEGACY_ACCOUNT_CALIBRATION_STREAM_BINDING
    ),
) -> None:
    while not shutdown_event.is_set():
        try:
            await asyncio.wait_for(
                shutdown_event.wait(),
                timeout=CALIBRATION_PENDING_REFRESH_SECONDS,
            )
            return
        except asyncio.TimeoutError:
            pass

        message_ids = [
            dispatched.message_id
            for task, dispatched in tuple(inflight.items())
            if not task.done() and dispatched.waiting_for_platform
        ]
        if not message_ids:
            continue
        try:
            await redis.xclaim(
                binding.stream_key,
                binding.group_name,
                calibration_consumer_name(binding),
                min_idle_time=0,
                message_ids=message_ids,
                justid=True,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            structured_log(
                "warning",
                "waiting_account_calibration_pending_refresh_failed",
                message_count=len(message_ids),
                stream=binding.stream_key,
                platform=binding.platform or "legacy",
                exception=exc,
            )


async def _wait_for_calibration_capacity(
    binding: AccountCalibrationStreamBinding,
    inflight: dict[asyncio.Task, _DispatchedCalibrationMessage],
    shutdown_event: asyncio.Event,
) -> bool:
    while len(inflight) >= CALIBRATION_DISPATCH_MAX_INFLIGHT:
        record_runtime_lane_progress(
            "calibration",
            binding.platform,
            saturated=True,
        )
        shutdown_waiter = asyncio.create_task(shutdown_event.wait())
        try:
            done, _ = await asyncio.wait(
                (*tuple(inflight), shutdown_waiter),
                return_when=asyncio.FIRST_COMPLETED,
                timeout=RUNTIME_LANE_PROGRESS_INTERVAL_SECONDS,
            )
        finally:
            if not shutdown_waiter.done():
                shutdown_waiter.cancel()
                await asyncio.gather(shutdown_waiter, return_exceptions=True)
        for completed in done:
            if completed is not shutdown_waiter:
                inflight.pop(completed, None)
        if shutdown_event.is_set():
            return False
    record_runtime_lane_progress(
        "calibration",
        binding.platform,
        saturated=False,
    )
    return not shutdown_event.is_set()


async def _ensure_calibration_stream_group(
    binding: AccountCalibrationStreamBinding,
) -> None:
    await verify_redis_consumer_group(
        redis,
        stream_key=binding.stream_key,
        group_name=binding.group_name,
    )


async def _calibration_platform_loop(
    pool,
    shutdown_event: asyncio.Event,
    binding: AccountCalibrationStreamBinding,
    platform_lock: asyncio.Lock,
) -> None:
    inflight: dict[asyncio.Task, _DispatchedCalibrationMessage] = {}
    pending_refresh_task = asyncio.create_task(
        _refresh_waiting_calibration_entries(
            inflight,
            shutdown_event,
            binding,
        )
    )
    group_ready = False
    try:
        while not shutdown_event.is_set():
            try:
                if not group_ready:
                    await _ensure_calibration_stream_group(binding)
                    group_ready = True
                if not await _wait_for_calibration_capacity(
                    binding,
                    inflight,
                    shutdown_event,
                ):
                    break
                available = CALIBRATION_DISPATCH_MAX_INFLIGHT - len(inflight)
                messages = await asyncio.wait_for(
                    redis.xreadgroup(
                        binding.group_name,
                        calibration_consumer_name(binding),
                        {binding.stream_key: ">"},
                        count=min(CALIBRATION_STREAM_READ_COUNT, available),
                        block=5000,
                    ),
                    timeout=6,
                )
                record_runtime_lane_success(
                    "calibration",
                    binding.platform,
                )
                if not messages:
                    continue
                for stream_name, entries in messages:
                    if str(stream_name) != binding.stream_key:
                        structured_log(
                            "error",
                            "account_calibration_stream_response_mismatch",
                            expected_stream=binding.stream_key,
                            actual_stream=str(stream_name),
                            platform=binding.platform,
                        )
                        continue
                    for msg_id, data in entries:
                        task_data = {key: value for key, value in data.items()}
                        try:
                            validate_account_calibration_stream_message(
                                binding,
                                task_data,
                            )
                        except ValueError as exc:
                            # The malformed delivery is not allowed to touch
                            # credentials. A durable legitimate row remains
                            # recoverable from its Outbox/DB authority.
                            await _ack_terminal_calibration_message(
                                binding,
                                str(msg_id),
                            )
                            structured_log(
                                "error",
                                "account_calibration_envelope_rejected",
                                message_id=str(msg_id),
                                stream=binding.stream_key,
                                platform=binding.platform,
                                error=str(exc),
                            )
                            continue
                        dispatched = _DispatchedCalibrationMessage(
                            str(msg_id), task_data
                        )
                        execution_task = asyncio.create_task(
                            _execute_dispatched_calibration(
                                dispatched,
                                binding,
                                platform_lock,
                                pool,
                            )
                        )
                        inflight[execution_task] = dispatched

                        def discard_completed(completed: asyncio.Task) -> None:
                            inflight.pop(completed, None)

                        execution_task.add_done_callback(discard_completed)
            except asyncio.TimeoutError as exc:
                record_runtime_lane_failure(
                    "calibration",
                    binding.platform,
                    exc,
                )
                continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                record_runtime_lane_failure(
                    "calibration",
                    binding.platform,
                    exc,
                )
                group_ready = False
                structured_log(
                    "error",
                    "account_calibration_loop_error",
                    stream=binding.stream_key,
                    platform=binding.platform,
                    exception=exc,
                )
                try:
                    await asyncio.wait_for(shutdown_event.wait(), timeout=3)
                except asyncio.TimeoutError:
                    pass
    except asyncio.CancelledError:
        raise
    finally:
        pending_refresh_task.cancel()
        execution_tasks = tuple(inflight)
        for execution_task in execution_tasks:
            execution_task.cancel()
        await asyncio.gather(
            pending_refresh_task,
            *execution_tasks,
            return_exceptions=True,
        )


LEGACY_CALIBRATION_FANOUT_LUA = """
local existing = redis.call('GET', KEYS[3])
if existing then
  redis.call('XACK', KEYS[1], ARGV[1], ARGV[2])
  return existing
end
local fields = {}
for index = 4, #ARGV do
  fields[#fields + 1] = ARGV[index]
end
local target_id = redis.call('XADD', KEYS[2], '*', unpack(fields))
redis.call('SET', KEYS[3], target_id, 'EX', ARGV[3])
redis.call('XACK', KEYS[1], ARGV[1], ARGV[2])
return target_id
"""


def _legacy_calibration_fanout_marker_key(message_id: str) -> str:
    digest = hashlib.sha256(
        (
            f"{LEGACY_ACCOUNT_CALIBRATION_STREAM_KEY}:"
            f"{str(message_id or '').strip()}"
        ).encode("utf-8")
    ).hexdigest()
    return f"account_calibration_legacy_fanout:{digest}"


async def _ack_legacy_calibration_fanout_entry(message_id: str) -> None:
    """ACK this compatibility group while retaining migration provenance."""

    await redis.xack(
        LEGACY_ACCOUNT_CALIBRATION_STREAM_KEY,
        LEGACY_ACCOUNT_CALIBRATION_FANOUT_GROUP_NAME,
        str(message_id),
    )


async def _fanout_legacy_calibration_entry(
    message_id: str,
    fields: dict,
) -> bool:
    """Transfer one legacy entry only after verifying current DB authority."""

    calibration_id = str(fields.get("calibration_id") or "").strip()
    try:
        account_id = int(fields.get("account_id"))
    except (TypeError, ValueError):
        account_id = 0
    if not CALIBRATION_ID_PATTERN.fullmatch(calibration_id) or account_id <= 0:
        await _ack_legacy_calibration_fanout_entry(message_id)
        return False
    row = await database.fetch_one(
        """SELECT c.calibration_id, c.account_id, c.platform, c.check_url,
                  c.status, a.platform AS account_platform,
                  a.deleted_at AS account_deleted_at,
                  a.execution_revision AS account_execution_revision
             FROM account_calibrations c
             LEFT JOIN accounts a ON a.id = c.account_id
            WHERE c.calibration_id = :calibration_id""",
        {"calibration_id": calibration_id},
    )
    platform = str(row["platform"] or "").strip().casefold() if row else ""
    account_platform = (
        str(row["account_platform"] or "").strip().casefold() if row else ""
    )
    status = str(row["status"] or "").strip().casefold() if row else ""
    if (
        not row
        or int(row["account_id"] or 0) != account_id
        or status != "queued"
        or row["account_deleted_at"] is not None
        or int(row["account_execution_revision"] or 0) <= 0
        or not platform
        or platform != account_platform
    ):
        # Missing/terminal/running/forged entries cannot be replayed. ACK only
        # this compatibility group; the historical consumer group and DB row
        # retain their independent authority.
        await _ack_legacy_calibration_fanout_entry(message_id)
        return False

    binding = account_calibration_stream_binding_for_platform(platform)
    fallback = str(
        fields.get("fallback_account_status") or "login_required"
    ).strip().casefold()
    if fallback not in SAFE_ACCOUNT_FALLBACK_STATUSES:
        fallback = "login_required"
    forwarded = {
        "calibration_id": calibration_id,
        "account_id": str(account_id),
        "platform": platform,
        "check_url": str(row["check_url"] or "").strip(),
        "calibration_kind": str(
            fields.get("calibration_kind") or ""
        ).strip().casefold(),
        "fallback_account_status": fallback,
    }
    validate_account_calibration_stream_message(binding, forwarded)
    argv = [
        LEGACY_ACCOUNT_CALIBRATION_FANOUT_GROUP_NAME,
        message_id,
        str(LEGACY_CALIBRATION_FANOUT_MARKER_SECONDS),
    ]
    for key, value in forwarded.items():
        argv.extend((str(key), str(value)))
    await redis.eval(
        LEGACY_CALIBRATION_FANOUT_LUA,
        3,
        LEGACY_ACCOUNT_CALIBRATION_STREAM_KEY,
        binding.stream_key,
        _legacy_calibration_fanout_marker_key(message_id),
        *argv,
    )
    structured_log(
        "info",
        "legacy_account_calibration_fanned_out",
        message_id=message_id,
        task_id=calibration_id,
        platform=platform,
        target_stream=binding.stream_key,
    )
    return True


async def _reclaim_legacy_calibration_fanout() -> int:
    pending = await redis.xpending_range(
        LEGACY_ACCOUNT_CALIBRATION_STREAM_KEY,
        LEGACY_ACCOUNT_CALIBRATION_FANOUT_GROUP_NAME,
        min="-",
        max="+",
        count=CALIBRATION_RECOVERY_BATCH,
        idle=CALIBRATION_IDLE_THRESHOLD_MS,
    )
    recovered = 0
    for entry in pending or []:
        message_id = str(entry.get("message_id") or "").strip()
        if not message_id:
            continue
        claimed = await redis.xclaim(
            LEGACY_ACCOUNT_CALIBRATION_STREAM_KEY,
            LEGACY_ACCOUNT_CALIBRATION_FANOUT_GROUP_NAME,
            LEGACY_ACCOUNT_CALIBRATION_FANOUT_CONSUMER_NAME,
            min_idle_time=CALIBRATION_IDLE_THRESHOLD_MS,
            message_ids=[message_id],
        )
        if not claimed:
            continue
        claimed_id, fields = claimed[0]
        if await _fanout_legacy_calibration_entry(
            str(claimed_id),
            dict(fields or {}),
        ):
            recovered += 1
    return recovered


async def _legacy_calibration_fanout_loop(
    shutdown_event: asyncio.Event,
) -> None:
    binding = AccountCalibrationStreamBinding(
        stream_key=LEGACY_ACCOUNT_CALIBRATION_STREAM_KEY,
        group_name=LEGACY_ACCOUNT_CALIBRATION_FANOUT_GROUP_NAME,
        platform=None,
        legacy=True,
    )
    group_ready = False
    while not shutdown_event.is_set():
        try:
            if not group_ready:
                await _ensure_calibration_stream_group(binding)
                group_ready = True
            await _reclaim_legacy_calibration_fanout()
            messages = await redis.xreadgroup(
                LEGACY_ACCOUNT_CALIBRATION_FANOUT_GROUP_NAME,
                LEGACY_ACCOUNT_CALIBRATION_FANOUT_CONSUMER_NAME,
                {LEGACY_ACCOUNT_CALIBRATION_STREAM_KEY: ">"},
                count=LEGACY_CALIBRATION_FANOUT_READ_COUNT,
                block=LEGACY_CALIBRATION_FANOUT_BLOCK_MS,
            )
            await asyncio.gather(
                *(
                    _fanout_legacy_calibration_entry(
                        str(message_id),
                        dict(fields or {}),
                    )
                    for stream_name, entries in messages or ()
                    if str(stream_name)
                    == LEGACY_ACCOUNT_CALIBRATION_STREAM_KEY
                    for message_id, fields in entries
                )
            )
            record_runtime_lane_success(
                "legacy_calibration_fanout"
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            group_ready = False
            record_runtime_lane_failure(
                "legacy_calibration_fanout",
                None,
                exc,
            )
            structured_log(
                "error",
                "legacy_account_calibration_fanout_loop_error",
                exception=exc,
            )
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=3)
            except asyncio.TimeoutError:
                pass


async def legacy_calibration_fanout_loop(
    shutdown_event: asyncio.Event,
) -> None:
    """Public control-worker entrypoint for the legacy shared stream."""

    await _legacy_calibration_fanout_loop(shutdown_event)


async def calibration_loop(
    pool,
    shutdown_event: asyncio.Event,
    *,
    platforms=None,
    include_legacy_fanout: bool | None = None,
):
    selected_platforms = normalize_platform_scope(
        "all" if platforms is None else platforms
    )
    for platform in selected_platforms:
        (
            PROFILES_ROOT
            / platform
            / CALIBRATION_SCREENSHOT_DIR_NAME
        ).mkdir(parents=True, exist_ok=True)
    bindings = tuple(
        binding
        for binding in account_calibration_stream_bindings(
            include_legacy=False
        )
        if binding.platform in selected_platforms
    )
    platform_locks = {
        binding.platform: asyncio.Lock() for binding in bindings
    }
    tasks = []
    for binding in bindings:
        lock = platform_locks[binding.platform]
        tasks.append(
            asyncio.create_task(
                _calibration_platform_loop(
                    pool,
                    shutdown_event,
                    binding,
                    lock,
                ),
                name=f"account-calibration:{binding.platform}",
            )
        )
        tasks.append(
            asyncio.create_task(
                _calibration_recovery_loop(
                    pool,
                    shutdown_event,
                    {binding.platform: lock},
                    binding=binding,
                ),
                name=f"account-calibration-recovery:{binding.platform}",
            )
        )
    legacy_fanout_enabled = (
        settings.legacy_control_stream_drain_enabled
        if include_legacy_fanout is None
        else bool(include_legacy_fanout)
    )
    if legacy_fanout_enabled:
        tasks.append(
            asyncio.create_task(
                _legacy_calibration_fanout_loop(shutdown_event),
                name="account-calibration:legacy-fanout",
            )
        )
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def handle_calibration(pool, task: dict):
    calibration_id = str(task["calibration_id"]).strip()
    account_id = int(task["account_id"])
    queued_platform = task.get("platform") if "platform" in task else None
    platform = (
        str(queued_platform).strip().lower()
        if queued_platform is not None
        else ""
    )
    screenshot_path = None

    claim = await claim_calibration_message(
        calibration_id,
        account_id,
        queued_platform,
        task.get("check_url") if "check_url" in task else None,
    )
    if not claim:
        structured_log(
            "warning",
            "account_calibration_claim_rejected",
            account_id=account_id,
            task_id=calibration_id,
            platform=platform,
        )
        return
    # From this point forward, use only the transactionally bound database
    # values.  Do not let the queue choose the platform or navigation target.
    platform = claim["platform"]
    check_url = claim["check_url"]
    screenshot_path = str(
        calibration_screenshot_path(
            platform,
            calibration_id,
        )
    )
    execution_revision = int(claim["execution_revision"])
    task = {
        **task,
        "calibration_id": calibration_id,
        "account_id": account_id,
        "platform": platform,
        "check_url": check_url,
        "staged_result": claim["staged_result"],
        # The locked database row is authoritative. This explicit assignment
        # also overwrites an execution_revision supplied by an old or forged
        # queue message.
        "execution_revision": execution_revision,
    }
    cfg = get_platform(platform)
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
        if platform == "douyin":
            calibration_kind = await resolve_douyin_calibration_kind(task)
            if calibration_kind == "device_agent":
                screenshot_path = None
                await handle_douyin_device_agent_calibration(task)
                return
        # Browser targets use a calibration-specific contract: the entry must
        # be an exact manifest URL, while redirects may use only the manifest's
        # explicitly approved hosts for this platform. OAuth never reaches this
        # browser branch.
        check_url = validated_calibration_browser_entry_url(platform, check_url)
        ctx = await pool.get_account_context(
            account_id,
            f"/profiles/{platform}/account_{account_id}",
            platform=platform,
        )
        page = await ctx.new_page()
        await install_calibration_navigation_guard(page, platform)
        await inject_calibration_cookies(
            ctx,
            account_id,
            platform,
            expected_execution_revision=execution_revision,
        )
        await page.goto(check_url, wait_until="domcontentloaded", timeout=45000)
        # The route guard blocks disallowed top-level requests before they are
        # sent; this second check binds the document that actually committed.
        validated_calibration_browser_navigation_url(platform, page.url)
        await page.wait_for_timeout(2000)
        await detect_page_risk(
            page,
            account_id,
            platform,
            expected_execution_revision=execution_revision,
        )

        cookies = await ctx.cookies()
        cookie_names = {cookie.get("name") for cookie in cookies}
        required = set(cfg.get("required_cookies", []))
        required_present = sorted(required.intersection(cookie_names))
        missing = sorted(required.difference(cookie_names))
        if missing:
            await mark_account_login_required(
                account_id,
                f"missing cookies after calibration: {', '.join(missing)}",
                expected_execution_revision=execution_revision,
            )
            raise ValueError(f"Missing required cookies after calibration: {', '.join(missing)}")

        identity = await verify_platform_identity(ctx, platform)
        identity_verified = identity.get("verified") is True
        final_url = page.url
        title = await safe_title(page)
        await page.screenshot(path=screenshot_path, full_page=True)
        async with database.transaction():
            current = await database.fetch_one(
                """SELECT c.status AS calibration_status,
                          a.status AS account_status, a.execution_revision
                     FROM account_calibrations c
                     JOIN accounts a ON a.id = c.account_id
                    WHERE c.calibration_id = :calibration_id
                      AND c.account_id = :account_id
                      AND c.platform = :platform
                      AND a.platform = :platform
                      AND a.deleted_at IS NULL
                    FOR UPDATE""",
                {
                    "calibration_id": calibration_id,
                    "account_id": account_id,
                    "platform": platform,
                },
            )
            if (
                not current
                or str(current["calibration_status"] or "").strip().lower()
                != "running"
                or int(current["execution_revision"] or 0)
                != execution_revision
            ):
                raise ValueError(
                    "account_calibration_execution_revision_mismatch"
                )
            current_account_status = str(
                current["account_status"] or ""
            ).strip().lower()
            target_account_status = calibrated_account_status(
                current_account_status, identity
            )
            result = {
                "check_url": check_url,
                "final_url": final_url,
                "title": title,
                "required_present": required_present,
                "identity": identity,
                "calibration_scope": (
                    "identity_and_session" if identity_verified else "session_only"
                ),
                "requires_manual_identity_review": not identity_verified,
                "account_status_target": target_account_status,
            }
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
                      AND deleted_at IS NULL
                      AND execution_revision = :execution_revision
                      AND status = :current_status""",
                {
                    "account_id": account_id,
                    "platform": platform,
                    "execution_revision": execution_revision,
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


async def inject_calibration_cookies(
    ctx,
    account_id: int,
    platform: str,
    *,
    expected_execution_revision: int,
):
    row = await database.fetch_one(
        """SELECT encrypted_credential, execution_revision FROM accounts
           WHERE id = :id AND platform = :platform AND deleted_at IS NULL""",
        {"id": account_id, "platform": platform},
    )
    if (
        not row
        or int(row["execution_revision"] or 0) != expected_execution_revision
    ):
        raise ValueError("account_calibration_execution_revision_mismatch")
    if not row or not row["encrypted_credential"]:
        raise ValueError(f"Account {account_id} has no imported login Cookie")
    credential_blob = row["encrypted_credential"]
    if isinstance(credential_blob, memoryview):
        credential_blob = credential_blob.tobytes()
    try:
        credential = cookie_vault.decrypt(credential_blob, aad=CREDENTIAL_AAD)
    except Exception as exc:
        # ``decrypt`` already supports legacy AES-GCM credentials without AAD.
        # A value that still cannot decrypt is corrupted or bypassed the
        # credential ingress and must never be reinterpreted as a Cookie.
        raise ValueError("account_credential_decryption_failed") from exc
    await inject_account_cookies(ctx, platform, credential)


async def resolve_weibo_calibration_kind(task: dict) -> str:
    """Derive the calibration route from persisted state, never queue kind."""

    from app.weibo.credentials import is_weibo_oauth_credential_envelope

    account_id = int(task.get("account_id"))
    check_url = str(task.get("check_url") or "").strip()
    staged_result = task.get("staged_result")
    row = await database.fetch_one(
        """SELECT platform, encrypted_credential, execution_revision
             FROM accounts WHERE id = :id AND deleted_at IS NULL""",
        {"id": account_id},
    )
    if not row or str(row["platform"] or "").strip().lower() != "weibo":
        raise ValueError("weibo_calibration_account_binding_invalid")
    expected_execution_revision = int(task.get("execution_revision") or 0)
    if (
        expected_execution_revision > 0
        and int(row["execution_revision"] or 0) != expected_execution_revision
    ):
        raise ValueError("account_calibration_execution_revision_mismatch")
    blob = row["encrypted_credential"]
    if not blob:
        raise ValueError("weibo_calibration_credential_required")
    purpose_bound = False
    try:
        candidate = cookie_vault.decrypt_strict(blob, aad=CREDENTIAL_AAD)
        purpose_bound = True
    except Exception:
        # Only the browser-session branch may read legacy unbound cookie
        # credentials. An OAuth envelope discovered through this fallback is
        # rejected below instead of being promoted to an OAuth calibration.
        try:
            candidate = cookie_vault.decrypt(blob, aad=CREDENTIAL_AAD)
        except Exception:
            candidate = (
                blob.decode("utf-8", errors="strict")
                if isinstance(blob, bytes)
                else str(blob)
            )
    is_oauth = is_weibo_oauth_credential_envelope(candidate)
    if is_oauth and not purpose_bound:
        raise ValueError("weibo_oauth_credential_decryption_failed")

    if is_oauth:
        if check_url != WEIBO_OAUTH_IDENTITY_URL:
            raise ValueError("weibo_oauth_calibration_binding_invalid")
        return (
            "weibo_oauth_capability"
            if staged_result is not None
            else "weibo_oauth_identity"
        )
    if check_url == WEIBO_OAUTH_IDENTITY_URL or staged_result is not None:
        raise ValueError("weibo_browser_calibration_credential_kind_mismatch")
    return "browser_session"


async def resolve_douyin_calibration_kind(task: dict) -> str:
    """Derive device routing from the purpose-bound account credential."""

    account_id = int(task.get("account_id"))
    check_url = str(task.get("check_url") or "").strip()
    row = await database.fetch_one(
        """SELECT encrypted_credential FROM accounts
             WHERE id = :account_id AND platform = 'douyin'
               AND deleted_at IS NULL LIMIT 1""",
        {"account_id": account_id},
    )
    blob = row["encrypted_credential"] if row else None
    if isinstance(blob, memoryview):
        blob = blob.tobytes()
    credential = None
    if blob:
        try:
            credential = parse_douyin_device_credential(
                cookie_vault.decrypt_strict(blob, aad=CREDENTIAL_AAD)
            )
        except Exception:
            credential = None
    if credential is not None:
        if check_url != DOUYIN_DEVICE_CALIBRATION_CHECK_URL:
            raise ValueError("douyin_device_calibration_binding_invalid")
        return "device_agent"
    if check_url == DOUYIN_DEVICE_CALIBRATION_CHECK_URL:
        raise ValueError("douyin_device_credential_invalid")
    return "browser_session"


async def handle_douyin_device_agent_calibration(task: dict) -> dict:
    """Read-only host-agent health calibration; never invokes an action."""

    from app.douyin_device_client import DouyinDeviceClient

    calibration_id = str(task.get("calibration_id") or "").strip()
    account_id = int(task.get("account_id"))
    execution_revision = int(task.get("execution_revision") or 0)
    if (
        not CALIBRATION_ID_PATTERN.fullmatch(calibration_id)
        or execution_revision <= 0
        or str(task.get("check_url") or "").strip()
        != DOUYIN_DEVICE_CALIBRATION_CHECK_URL
    ):
        raise ValueError("douyin_device_calibration_binding_invalid")
    row = await database.fetch_one(
        """SELECT c.status AS calibration_status,
                  a.encrypted_credential, a.execution_revision
             FROM account_calibrations c
             JOIN accounts a ON a.id = c.account_id
            WHERE c.calibration_id = :calibration_id
              AND c.account_id = :account_id
              AND c.platform = 'douyin' AND a.platform = 'douyin'
              AND a.deleted_at IS NULL LIMIT 1""",
        {"calibration_id": calibration_id, "account_id": account_id},
    )
    blob = row["encrypted_credential"] if row else None
    if isinstance(blob, memoryview):
        blob = blob.tobytes()
    if (
        not row
        or str(row["calibration_status"] or "").casefold() != "running"
        or int(row["execution_revision"] or 0) != execution_revision
        or not blob
    ):
        raise ValueError("douyin_device_calibration_binding_invalid")
    try:
        credential = parse_douyin_device_credential(
            cookie_vault.decrypt_strict(blob, aad=CREDENTIAL_AAD)
        )
    except Exception as exc:
        raise ValueError("douyin_device_credential_invalid") from exc

    client = DouyinDeviceClient.from_environment()
    try:
        health = await client.health(
            expected_identity=credential["device_agent"],
            required_actions=DOUYIN_DEVICE_ACTION_ORDER,
        )
    finally:
        await client.aclose()

    result = {
        "contract_version": 1,
        "credential_kind": "device_agent",
        "identity_verified": True,
        "calibration_scope": "device_agent_health",
        "account_id": account_id,
        "execution_revision": execution_revision,
        "device_agent": credential["device_agent"],
        "package": health["package"],
        "supported_actions": list(health["supported_actions"]),
        "observed_at": health["observed_at"],
    }
    async with database.transaction():
        current = await database.fetch_one(
            """SELECT c.status AS calibration_status,
                      a.status AS account_status, a.execution_revision,
                      a.encrypted_credential
                 FROM account_calibrations c
                 JOIN accounts a ON a.id = c.account_id
                WHERE c.calibration_id = :calibration_id
                  AND c.account_id = :account_id
                  AND c.platform = 'douyin' AND a.platform = 'douyin'
                  AND a.deleted_at IS NULL FOR UPDATE""",
            {"calibration_id": calibration_id, "account_id": account_id},
        )
        current_blob = current["encrypted_credential"] if current else None
        if isinstance(current_blob, memoryview):
            current_blob = current_blob.tobytes()
        try:
            current_credential = parse_douyin_device_credential(
                cookie_vault.decrypt_strict(current_blob, aad=CREDENTIAL_AAD)
            )
        except Exception as exc:
            raise ValueError("douyin_device_credential_invalid") from exc
        current_status = str(current["account_status"] or "").casefold()
        if (
            not current
            or str(current["calibration_status"] or "").casefold() != "running"
            or int(current["execution_revision"] or 0) != execution_revision
            or current_credential != credential
            or current_status not in {"warming", "login_required", "ready", "cooling"}
        ):
            raise ValueError("douyin_device_calibration_binding_invalid")
        target_status = "cooling" if current_status == "cooling" else "ready"
        calibration_updated = await execute_affected_rows(
            """UPDATE account_calibrations
                  SET status = 'succeeded', result = :result,
                      screenshot_path = NULL, error_message = NULL,
                      finished_at = NOW()
                WHERE calibration_id = :calibration_id
                  AND account_id = :account_id AND platform = 'douyin'
                  AND status = 'running'""",
            {
                "calibration_id": calibration_id,
                "account_id": account_id,
                "result": json.dumps(result, ensure_ascii=False),
            },
            db=database,
        )
        account_updated = await execute_affected_rows(
            """UPDATE accounts SET status = :target_status,
                      updated_at = NOW(), version = version + 1
                WHERE id = :account_id AND platform = 'douyin'
                  AND deleted_at IS NULL
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
        if calibration_updated != 1 or account_updated != 1:
            raise ValueError("douyin_device_calibration_settlement_lost")
    await emit_calibration_terminal_observability(
        account_id=account_id,
        calibration_id=calibration_id,
        platform="douyin",
        status="succeeded",
        content=f"Account A{account_id} device-agent health calibration succeeded.",
        severity="info",
        event_type="AccountCalibrated",
        event_payload={
            "platform": "douyin",
            "calibration_id": calibration_id,
            "result": result,
            "screenshot_path": None,
        },
    )
    return result


async def mark_account_login_required(
    account_id: int,
    reason: str,
    *,
    expected_execution_revision: int | None = None,
):
    async with database.transaction():
        if expected_execution_revision is None:
            await database.execute(
                """UPDATE accounts
                   SET status = 'login_required', updated_at = NOW(),
                       version = version + 1
                   WHERE id = :account_id""",
                {"account_id": account_id},
            )
        else:
            affected = await execute_affected_rows(
                """UPDATE accounts
                   SET status = 'login_required', updated_at = NOW(),
                       version = version + 1
                   WHERE id = :account_id
                     AND deleted_at IS NULL
                     AND execution_revision = :execution_revision""",
                {
                    "account_id": account_id,
                    "execution_revision": expected_execution_revision,
                },
                db=database,
            )
            if affected != 1:
                raise ValueError(
                    "account_calibration_execution_revision_mismatch"
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


def public_identity_text(value, *, max_length: int = 160) -> str | None:
    """Return a bounded public profile label without coercing opaque objects.

    Calibration evidence is durable and rendered by the operations UI.  Only
    direct scalar values from an authenticated identity response may enter the
    public profile; cookies, credential hashes, URLs, and nested unknown data
    never pass through this helper.
    """

    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    normalized = str(value).strip()
    if (
        not normalized
        or len(normalized) > max_length
        or any(ord(char) < 32 or ord(char) == 127 for char in normalized)
    ):
        return None
    return normalized


def bilibili_public_identity_metadata(data: dict) -> dict:
    """Extract only authenticated public labels from Bilibili nav data."""

    metadata = {}
    nickname = public_identity_text(data.get("uname"))
    if nickname:
        metadata["nickname"] = nickname

    level_info = data.get("level_info")
    if isinstance(level_info, dict):
        level = level_info.get("current_level")
        if isinstance(level, int) and not isinstance(level, bool) and 0 <= level <= 99:
            metadata["level"] = level

    official = data.get("official")
    official_title = (
        public_identity_text(official.get("title"))
        if isinstance(official, dict)
        else None
    )
    vip = data.get("vip")
    vip_label = vip.get("label") if isinstance(vip, dict) else None
    membership_title = (
        public_identity_text(vip_label.get("text"))
        if isinstance(vip_label, dict)
        else None
    )
    title = official_title or membership_title
    if title:
        metadata["title"] = title
    return metadata


async def verify_bilibili_identity(ctx) -> dict:
    cookies = await ctx.cookies("https://api.bilibili.com/")
    declared_mid_values = {
        str(cookie.get("value") or "").strip()
        for cookie in cookies
        if cookie.get("name") == "DedeUserID"
    }
    if len(declared_mid_values) != 1:
        raise ValueError("Bilibili credential identity is ambiguous")
    declared_mid = next(iter(declared_mid_values))
    if (
        not declared_mid.isascii()
        or not declared_mid.isdecimal()
        or int(declared_mid) <= 0
    ):
        raise ValueError("Bilibili credential identity is ambiguous")
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
    if str(data["mid"]) != declared_mid:
        raise ValueError("bilibili_authenticated_identity_mismatch")
    mid = str(data["mid"])
    return {
        "verified": True,
        "method": "bilibili_nav",
        "mid": mid,
        "uid": mid,
        **bilibili_public_identity_metadata(data),
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

    from app.weibo.capabilities import (
        build_weibo_oauth_capability_attestation,
    )

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
    identity_client_factory=None,
) -> dict:
    """Run official identity verification without browser/cookie fallback."""

    from app.weibo.capabilities import validate_weibo_operator_attestation
    from app.weibo.client import WeiboOAuthIdentityClient
    from app.weibo.credentials import parse_weibo_oauth_credential

    if identity_client_factory is None:
        identity_client_factory = WeiboOAuthIdentityClient

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
    claimed_execution_revision = int(task.get("execution_revision") or 0)
    if (
        claimed_execution_revision > 0
        and execution_revision != claimed_execution_revision
    ):
        raise ValueError("weibo_oauth_execution_revision_mismatch")
    credential_blob = row["encrypted_credential"]
    if not credential_blob:
        raise ValueError("weibo_oauth_credential_required")
    try:
        decrypted = cookie_vault.decrypt_strict(
            credential_blob, aad=CREDENTIAL_AAD
        )
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
    # Cookie presence proves only that a session-shaped credential exists.  It
    # must never be promoted to verified identity when this endpoint is
    # unavailable, rejects the request, or returns an unknown payload shape.
    try:
        response = await ctx.request.get(
            "https://edith.xiaohongshu.com/api/sns/web/v2/user/me",
            headers={"Referer": "https://www.xiaohongshu.com/"},
            timeout=15000,
        )
        if not response.ok:
            return {
                "verified": False,
                "method": "xiaohongshu_me",
                "note": f"identity_api_http_{response.status}",
            }
        payload = await response.json()
    except Exception:
        return {
            "verified": False,
            "method": "xiaohongshu_me",
            "note": "identity_api_unavailable",
        }
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        return {
            "verified": False,
            "method": "xiaohongshu_me",
            "note": "identity_api_payload_invalid",
        }
    data = payload["data"]
    if data.get("guest") is True:
        raise ValueError("Xiaohongshu credential is not authenticated")
    user_id = data.get("user_id") or data.get("userId")
    if (
        payload.get("success") is True
        and isinstance(user_id, (str, int))
        and not isinstance(user_id, bool)
        and str(user_id).strip()
    ):
        identity = {
            "verified": True,
            "method": "xiaohongshu_me",
            "user_id": str(user_id).strip(),
        }
        nickname = public_identity_text(data.get("nickname"))
        title = public_identity_text(data.get("official_title")) or public_identity_text(
            data.get("certification_title")
        )
        if nickname:
            identity["nickname"] = nickname
        if title:
            identity["title"] = title
        return identity
    return {
        "verified": False,
        "method": "xiaohongshu_me",
        "note": "identity_api_unverified",
    }


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
