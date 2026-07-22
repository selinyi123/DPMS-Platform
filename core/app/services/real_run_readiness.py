import asyncio
import hashlib
import hmac
import json
import os
import stat
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.action_plan import (
    ACTION_ORDER,
    BILIBILI_API_EXECUTION_PATH,
    DOUYIN_MANUAL_EXECUTION_PATH,
    DOUYIN_NO_OFFICIAL_API_BLOCKER,
    WEIBO_MANUAL_EXECUTION_BLOCKER,
    WEIBO_MANUAL_EXECUTION_PATH,
    WEIBO_ACTION_CAPABILITY_REQUIREMENTS,
    WEIBO_ACTION_ORDER,
    WEIBO_OAUTH_CAPABILITY_CONTRACT_VERSION,
    WEIBO_OAUTH_EXECUTION_PATH,
    XIAOHONGSHU_ACTION_ORDER,
    XIAOHONGSHU_MANUAL_EXECUTION_PATH,
    XIAOHONGSHU_NO_OFFICIAL_API_BLOCKER,
    ActionPlanV2Error,
    action_order_for_platform,
    bind_manual_friend_mentions,
    bind_manual_follow_target,
    compute_bilibili_api_config_hash,
    compute_config_hash,
    compute_rule_hash,
    compute_target_hash,
    semantic_requirement_status,
    validate_action_plan_v2,
    validate_friend_mention_requirements,
    weibo_runtime_capability_requirements,
)
from app.adapter_config import (
    STRUCTURED_SELECTOR_PLATFORMS,
    click_selectors,
    platform_has_api_real_adapter,
    platform_has_runtime_real_adapter,
    platform_probe_ready_for_real_actions,
    platform_real_adapter_kind,
    selector_config_complete,
    selector_phase_configured,
    selector_values,
)
from app.db import database, redis
from app.platforms import get_platform
from app.services.lottery_rules import parse_lottery_rule
from app.services.bilibili_preflight_evidence import (
    BilibiliPreflightEvidenceError,
    extract_bilibili_dynamic_id,
    validate_preflight_observation_binding,
)
from app.utils.log import structured_log
from app.utils.lottery_targets import validate_lottery_target
from app.utils.crypto import CREDENTIAL_AAD, cookie_vault
from app.utils.weibo_oauth_credential import (
    WeiboOAuthCredentialError,
    parse_weibo_oauth_credential,
)

ACCOUNT_RISK_COOLDOWN_HOURS = 24
MAX_ACCOUNT_RISK_COOLDOWN_HOURS = 24
SHADOW_PHASE_ORDER = list(ACTION_ORDER)
EVIDENCE_ROOT = Path(os.getenv("EVIDENCE_ROOT", "/profiles"))
SHADOW_SCREENSHOT_ROOT = EVIDENCE_ROOT / "shadow-runs"
EVIDENCE_HASH_CHUNK_SIZE = 1024 * 1024
MAX_SHADOW_SCREENSHOT_BYTES = 32 * 1024 * 1024
MAX_EVIDENCE_REQUEST_HASH_BYTES = 128 * 1024 * 1024
WEIBO_OAUTH_CAPABILITY_MAX_AGE = timedelta(hours=24)
WEIBO_OAUTH_ATTESTATION_KEYS = frozenset(
    {
        "contract_version",
        "calibration_id",
        "account_id",
        "execution_revision",
        "credential_kind",
        "identity_verified",
        "app_review_status",
        "client_type",
        "verified_at",
        "evidence_source",
        "attested_by",
        "attested_at",
        "actions",
    }
)
WEIBO_OAUTH_ACTION_ATTESTATION_KEYS = frozenset(
    {"endpoint", "permission", "granted"}
)
ACCOUNT_RISK_COOLDOWN_BY_REASON = {
    # A local action burst should pause the account, not lock it out for a day.
    "action_window": 4,
    "sliding_window_exceeded": 4,
    # Harder risk signals keep the conservative 24 hour hold.
    "daily_limit": 24,
    "page_risk_signal": 24,
    "redirected_to_login": 24,
    "execution_timeout": 24,
    "bilibili_follow_captcha": 24,
    "bilibili_like_captcha": 24,
    "bilibili_comment_captcha": 24,
    "bilibili_repost_captcha": 24,
    "bilibili_follow_limit": 24,
    "bilibili_like_limit": 24,
    "bilibili_comment_limit": 24,
    "bilibili_repost_limit": 24,
    "bilibili_follow_risk": 24,
    "bilibili_like_risk": 24,
    "bilibili_comment_risk": 24,
    "bilibili_repost_risk": 24,
}


@dataclass
class RealRunEvidenceBatch:
    """Request-scoped, read-only evidence used by the evidence list endpoint.

    Dispatch keeps using the non-batched path, so every authoritative decision
    still performs fresh reads.  The batch only replaces the list endpoint's
    per-lottery queries with equivalent latest-row maps.
    """

    account_id: int | None
    probes: dict[tuple[int, str], object] = field(default_factory=dict)
    shadows: dict[int, object] = field(default_factory=dict)
    observations: dict[str, object] = field(default_factory=dict)
    evidence_files: dict[tuple[str, str, int], object] = field(default_factory=dict)
    screenshot_integrity_cache: dict[tuple[str, str, str], tuple[tuple[int, int, int, int, int], bool]] = field(
        default_factory=dict
    )
    screenshot_hash_budget: dict[str, int] = field(
        default_factory=lambda: {"remaining": MAX_EVIDENCE_REQUEST_HASH_BYTES}
    )


def _sql_in_values(prefix: str, values) -> tuple[str, dict]:
    parameters = {}
    placeholders = []
    for index, value in enumerate(values):
        key = f"{prefix}_{index}"
        placeholders.append(f":{key}")
        parameters[key] = value
    return ", ".join(placeholders), parameters


def parse_json_field(value):
    if isinstance(value, (dict, list)) or value is None:
        return value
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    try:
        return json.loads(value)
    except Exception:
        return value


def platform_selectors_complete(selector_config: dict, platform: str) -> bool:
    configured = selector_config.get(platform, {})
    return selector_config_complete(platform, configured)


def phase_configured(platform: str, config: dict, phase: str) -> bool:
    return selector_phase_configured(platform, config, phase)


def qualified_shadow_observation(payload, required_actions: list[str]) -> bool:
    if not isinstance(payload, dict) or payload.get("qualified") is not True or payload.get("side_effects") is not False:
        return False
    expected = [phase for phase in SHADOW_PHASE_ORDER if phase in required_actions]
    if len(expected) != len(required_actions) or payload.get("required_phases") != expected:
        return False
    visible = payload.get("visible_phases")
    if not isinstance(visible, dict):
        return False
    for phase in expected:
        observation = visible.get(phase)
        if phase == "commented":
            if not isinstance(observation, dict) or not observation.get("input") or not observation.get("submit"):
                return False
        elif not observation:
            return False
    return bool(expected and payload.get("screenshot_path"))


def qualified_manual_shadow_observation(
    payload,
    *,
    required_actions: tuple[str, ...] | list[str],
    capability_blocker: str,
) -> bool:
    """Validate manual-only selector evidence without making it real-ready.

    The Worker deliberately reports ``qualified=false`` because no official
    participant interaction API exists. This independent contract records that
    every required selector was observed while preserving the manual-
    confirmation boundary; the generic real-run validator remains strict.
    """

    if not isinstance(payload, dict):
        return False
    if (
        payload.get("side_effects") is not False
        or payload.get("qualified") is not False
        or payload.get("selector_observation_complete") is not True
        or payload.get("manual_confirmation_required") is not True
        or payload.get("real_run_capable") is not False
        or payload.get("capability_block_reason") != capability_blocker
        or payload.get("required_phases") != list(required_actions)
    ):
        return False
    visible = payload.get("visible_phases")
    if not isinstance(visible, dict):
        return False
    for phase in required_actions:
        observation = visible.get(phase)
        if phase == "commented":
            if (
                not isinstance(observation, dict)
                or not observation.get("input")
                or not observation.get("submit")
            ):
                return False
        elif not observation:
            return False
    return bool(payload.get("screenshot_path"))


def qualified_xiaohongshu_manual_shadow_observation(payload) -> bool:
    """Backward-compatible XHS four-action observation validator."""

    return qualified_manual_shadow_observation(
        payload,
        required_actions=XIAOHONGSHU_ACTION_ORDER,
        capability_blocker=XIAOHONGSHU_NO_OFFICIAL_API_BLOCKER,
    )


def qualified_douyin_manual_shadow_observation(
    payload,
    required_actions: tuple[str, ...] | list[str],
) -> bool:
    """Validate a variable-action Douyin manual shadow observation."""

    return qualified_manual_shadow_observation(
        payload,
        required_actions=required_actions,
        capability_blocker=DOUYIN_NO_OFFICIAL_API_BLOCKER,
    )


def qualified_weibo_manual_shadow_observation(
    payload,
    required_actions: tuple[str, ...] | list[str],
) -> bool:
    """Validate the explicit Weibo manual-fallback observation contract."""

    return qualified_manual_shadow_observation(
        payload,
        required_actions=required_actions,
        capability_blocker=WEIBO_MANUAL_EXECUTION_BLOCKER,
    )


def shadow_screenshot_integrity_matches(
    file_path,
    expected_sha256,
    *,
    allowed_root: str | Path | None = None,
    integrity_cache: dict | None = None,
    hash_budget: dict[str, int] | None = None,
) -> bool:
    """Validate a shadow screenshot without reading files outside its evidence root."""
    expected_hash = str(expected_sha256 or "").strip().lower()
    if len(expected_hash) != 64 or any(character not in "0123456789abcdef" for character in expected_hash):
        return False

    try:
        candidate = Path(str(file_path or ""))
        if not candidate.is_absolute():
            return False
        root = Path(allowed_root) if allowed_root is not None else SHADOW_SCREENSHOT_ROOT
        resolved_root = root.resolve(strict=True)
        resolved_candidate = candidate.resolve(strict=True)
        if not resolved_candidate.is_relative_to(resolved_root):
            return False

        with _open_evidence_file_beneath_root(resolved_root, resolved_candidate) as screenshot:
            before = os.fstat(screenshot.fileno())
            if not stat.S_ISREG(before.st_mode):
                return False
            if before.st_size > MAX_SHADOW_SCREENSHOT_BYTES:
                return False
            signature = (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            )
            cache_key = (str(resolved_root), str(resolved_candidate), expected_hash)
            cached = integrity_cache.get(cache_key) if integrity_cache is not None else None
            if cached and cached[0] == signature:
                # Opening the resolved path and comparing its current identity
                # prevents a cached result from surviving replacement/tamper.
                path_now = resolved_candidate.stat()
                path_signature = (
                    path_now.st_dev,
                    path_now.st_ino,
                    path_now.st_size,
                    path_now.st_mtime_ns,
                    path_now.st_ctime_ns,
                )
                if path_signature == signature:
                    return bool(cached[1])
                return False

            if hash_budget is not None:
                remaining = max(int(hash_budget.get("remaining", 0)), 0)
                if before.st_size > remaining:
                    hash_budget["exhausted"] = 1
                    hash_budget["required_bytes"] = before.st_size
                    return False

            digest = hashlib.sha256()
            bytes_hashed = 0
            while bytes_hashed < before.st_size:
                read_size = min(
                    EVIDENCE_HASH_CHUNK_SIZE,
                    before.st_size - bytes_hashed,
                )
                if hash_budget is not None:
                    remaining = max(int(hash_budget.get("remaining", 0)), 0)
                    read_size = min(read_size, remaining)
                if read_size <= 0:
                    return False
                chunk = screenshot.read(read_size)
                if not chunk:
                    break
                bytes_hashed += len(chunk)
                if hash_budget is not None:
                    remaining = max(int(hash_budget.get("remaining", 0)), 0)
                    hash_budget["remaining"] = remaining - len(chunk)
                digest.update(chunk)
            after = os.fstat(screenshot.fileno())
            after_signature = (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
            if after_signature != signature:
                return False
        path_after = resolved_candidate.stat()
        path_signature = (
            path_after.st_dev,
            path_after.st_ino,
            path_after.st_size,
            path_after.st_mtime_ns,
            path_after.st_ctime_ns,
        )
        if path_signature != signature:
            return False
    except (OSError, RuntimeError, TypeError, ValueError):
        return False

    matches = hmac.compare_digest(digest.hexdigest(), expected_hash)
    if integrity_cache is not None:
        integrity_cache[cache_key] = (signature, matches)
    return matches


def _open_evidence_file_beneath_root(resolved_root: Path, resolved_candidate: Path):
    """Open an evidence file without following replaceable in-root symlinks.

    DPMS runs Core in Linux containers, where directory-fd traversal with
    ``O_NOFOLLOW`` binds every absolute root component and every in-root
    component to an already-opened parent.  Runtimes without these primitives
    fail closed instead of silently reintroducing a resolve/open race.
    """
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
        raise OSError("secure_evidence_open_unsupported")

    relative = resolved_candidate.relative_to(resolved_root)
    if not relative.parts:
        raise OSError("evidence_path_is_not_a_file")
    if not resolved_root.is_absolute():
        raise OSError("evidence_root_must_be_absolute")
    close_on_exec = getattr(os, "O_CLOEXEC", 0)
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | close_on_exec
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | close_on_exec
    directory_fds: list[int] = []
    file_fd: int | None = None
    try:
        current_fd = os.open(os.path.sep, directory_flags)
        directory_fds.append(current_fd)
        for part in resolved_root.parts[1:]:
            current_fd = os.open(part, directory_flags, dir_fd=current_fd)
            directory_fds.append(current_fd)
        for part in relative.parts[:-1]:
            current_fd = os.open(part, directory_flags, dir_fd=current_fd)
            directory_fds.append(current_fd)
        file_fd = os.open(relative.parts[-1], file_flags, dir_fd=current_fd)
        stream = os.fdopen(file_fd, "rb")
        file_fd = None
        return stream
    finally:
        if file_fd is not None:
            os.close(file_fd)
        for directory_fd in reversed(directory_fds):
            os.close(directory_fd)


def action_plan_missing_rule_actions(lottery_data: dict, action_plan: dict | None) -> list[str]:
    """Return every rule/action-plan set mismatch in deterministic order.

    The historical name is kept for compatibility, but extra saved actions are
    as unsafe as missing actions: a stale/manual plan must not authorize a
    remote mutation that the source rule never requested.
    """
    if not isinstance(action_plan, dict):
        return []
    rule_text = str(lottery_data.get("rule_text") or "").strip()
    if not rule_text:
        return []
    platform = str(lottery_data.get("platform") or "bilibili")
    suggested = parse_lottery_rule(rule_text, platform)
    suggested_actions = suggested.get("required_actions") or []
    saved_actions = action_plan.get("required_actions") or []
    if not isinstance(suggested_actions, list) or not isinstance(saved_actions, list):
        return []
    suggested_order = [str(action) for action in suggested_actions]
    saved_order = [str(action) for action in saved_actions]
    suggested = set(suggested_order)
    saved = set(saved_order)
    missing = [action for action in suggested_order if action not in saved]
    extra = [action for action in saved_order if action not in suggested]
    return [*missing, *extra]


def normalize_timestamp(value) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value).replace(" ", "T")


def normalize_datetime(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    if " " in text and "T" not in text:
        text = text.replace(" ", "T", 1)
    try:
        return datetime.fromisoformat(text).replace(tzinfo=None)
    except ValueError:
        return None


def row_value(row, key, default=None):
    if not row:
        return default
    try:
        return row[key]
    except Exception:
        if hasattr(row, "get"):
            return row.get(key, default)
    return default


def account_risk_reason(detail) -> str:
    parsed = parse_json_field(detail)
    if isinstance(parsed, dict):
        return str(parsed.get("reason") or "").strip()
    return ""


def account_risk_cooldown_hours(detail=None, event_type: str | None = None) -> int:
    reason = account_risk_reason(detail).lower()
    if reason in ACCOUNT_RISK_COOLDOWN_BY_REASON:
        return ACCOUNT_RISK_COOLDOWN_BY_REASON[reason]
    if str(event_type or "").lower() == "login_required":
        return 24
    return ACCOUNT_RISK_COOLDOWN_HOURS


def account_risk_cooldown_until(row) -> datetime | None:
    created_at = normalize_datetime(row_value(row, "created_at"))
    if not created_at:
        fallback = normalize_datetime(row_value(row, "cooldown_until"))
        return fallback
    hours = account_risk_cooldown_hours(row_value(row, "detail"), row_value(row, "event_type"))
    return created_at + timedelta(hours=hours)


def account_risk_is_active(row, now=None) -> bool:
    cooldown_until = account_risk_cooldown_until(row)
    if not cooldown_until:
        return True
    now_dt = normalize_datetime(now) or datetime.now()
    return cooldown_until > now_dt


async def current_db_time() -> datetime:
    row = await database.fetch_one("SELECT NOW() AS db_now")
    return normalize_datetime(row_value(row, "db_now")) or datetime.now()


def account_risk_payload(row) -> dict:
    if not row:
        return {"has_recent_risk": False, "cooldown_hours": ACCOUNT_RISK_COOLDOWN_HOURS}
    detail = parse_json_field(row_value(row, "detail"))
    event_type = row_value(row, "event_type")
    cooldown_hours = account_risk_cooldown_hours(detail, event_type)
    cooldown_until = account_risk_cooldown_until(row)
    return {
        "has_recent_risk": True,
        "cooldown_hours": cooldown_hours,
        "latest_event": {
            "id": row_value(row, "id"),
            "account_id": row_value(row, "account_id"),
            "event_type": event_type,
            "detail": detail if isinstance(detail, dict) else {},
            "created_at": normalize_timestamp(row_value(row, "created_at")),
        },
        "cooldown_until": normalize_timestamp(cooldown_until),
    }


async def recent_account_risk(account_id: int, *, now=None) -> dict:
    now_dt = normalize_datetime(now) or await current_db_time()
    # Cooldowns vary by event reason.  A row-count cap can let many newer,
    # already-expired short cooldowns hide an older 24-hour risk, so the
    # complete bounded time window is the safety boundary here.
    rows = await database.fetch_all(
        f"""SELECT id, account_id, event_type, detail, created_at
           FROM risk_events
           WHERE account_id = :account_id
             AND created_at >= DATE_SUB(NOW(), INTERVAL {MAX_ACCOUNT_RISK_COOLDOWN_HOURS} HOUR)
           ORDER BY created_at DESC, id DESC""",
        {"account_id": account_id},
    )
    for row in rows:
        if account_risk_is_active(row, now_dt):
            return account_risk_payload(row)
    return account_risk_payload(None)


async def real_run_account_risk_summaries(platforms) -> dict[str, dict]:
    """Load ready-account and active-risk summaries in a constant query count."""
    requested_platforms = list(dict.fromkeys(str(platform) for platform in platforms))
    if not requested_platforms:
        return {}

    platform_clause, values = _sql_in_values("platform", requested_platforms)
    ready_rows = await database.fetch_all(
        f"""SELECT a.id, a.platform
           FROM accounts a
           WHERE a.platform IN ({platform_clause})
             AND a.status = 'ready'
             AND OCTET_LENGTH(a.encrypted_credential) > 0
             AND (
               SELECT c.status FROM account_calibrations c
               WHERE c.account_id = a.id
               ORDER BY c.created_at DESC
               LIMIT 1
             ) = 'succeeded'""",
        values,
    )
    db_now = await current_db_time()

    account_ids = list(dict.fromkeys(int(row_value(account, "id")) for account in ready_rows))
    risk_rows = []
    if account_ids:
        account_clause, account_values = _sql_in_values("risk_account", account_ids)
        # Preserve every event in the maximum cooldown window; per-account
        # ranking/capping would recreate the same displacement bug as the
        # single-account path.
        risk_rows = await database.fetch_all(
            f"""SELECT id, account_id, event_type, detail, created_at
                FROM risk_events
                WHERE account_id IN ({account_clause})
                  AND created_at >= DATE_SUB(NOW(), INTERVAL {MAX_ACCOUNT_RISK_COOLDOWN_HOURS} HOUR)
                ORDER BY account_id, created_at DESC, id DESC""",
            account_values,
        )

    risks_by_account: dict[int, list] = {account_id: [] for account_id in account_ids}
    for row in risk_rows:
        risk_account_id = int(row_value(row, "account_id"))
        if risk_account_id not in risks_by_account:
            raise RuntimeError("risk query returned an account outside the requested evidence scope")
        risks_by_account[risk_account_id].append(row)

    ready_by_platform_key = {platform.casefold(): [] for platform in requested_platforms}
    for account in ready_rows:
        platform_key = str(row_value(account, "platform") or "").casefold()
        if platform_key in ready_by_platform_key:
            ready_by_platform_key[platform_key].append(account)

    summaries = {}
    for platform in requested_platforms:
        platform_accounts = ready_by_platform_key[platform.casefold()]
        runnable_count = 0
        latest_risk = None
        latest_risk_created_at = None
        for account in platform_accounts:
            account_id = int(row_value(account, "id"))
            risk = account_risk_payload(None)
            for row in risks_by_account.get(account_id, []):
                if account_risk_is_active(row, db_now):
                    risk = account_risk_payload(row)
                    break
            if not risk["has_recent_risk"]:
                runnable_count += 1
                continue
            created_at = normalize_datetime(risk["latest_event"].get("created_at"))
            if latest_risk is None or (
                created_at and (latest_risk_created_at is None or created_at > latest_risk_created_at)
            ):
                latest_risk = risk
                latest_risk_created_at = created_at
        summaries[platform] = {
            "ready_accounts": len(platform_accounts),
            "runnable_accounts": runnable_count,
            "latest_recent_risk": latest_risk or account_risk_payload(None),
        }
    return summaries


async def real_run_account_risk_summary(platform: str) -> dict:
    ready_rows = await database.fetch_all(
        """SELECT a.id
           FROM accounts a
           WHERE a.platform = :platform
             AND a.status = 'ready'
             AND OCTET_LENGTH(a.encrypted_credential) > 0
             AND (
               SELECT c.status FROM account_calibrations c
               WHERE c.account_id = a.id
               ORDER BY c.created_at DESC
               LIMIT 1
             ) = 'succeeded'""",
        {"platform": platform},
    )
    db_now = await current_db_time()
    runnable_count = 0
    latest_risk = None
    latest_risk_created_at = None
    for account in ready_rows:
        risk = await recent_account_risk(int(account["id"]), now=db_now)
        if not risk["has_recent_risk"]:
            runnable_count += 1
            continue
        created_at = normalize_datetime(risk["latest_event"].get("created_at"))
        if latest_risk is None or (
            created_at and (latest_risk_created_at is None or created_at > latest_risk_created_at)
        ):
            latest_risk = risk
            latest_risk_created_at = created_at
    return {
        "ready_accounts": len(ready_rows),
        "runnable_accounts": runnable_count,
        "latest_recent_risk": latest_risk or account_risk_payload(None),
    }


async def load_real_run_evidence_batch(lotteries, *, account_id: int | None = None) -> RealRunEvidenceBatch:
    """Preload latest Probe/Shadow rows for a bounded lottery list.

    The query predicates and latest-row ordering mirror
    :func:`validate_real_run_evidence`; absent or malformed rows remain absent
    in the maps and therefore block readiness.
    """
    batch = RealRunEvidenceBatch(account_id=account_id)
    lottery_rows = [dict(lottery) for lottery in lotteries]
    lottery_ids = list(dict.fromkeys(int(lottery["id"]) for lottery in lottery_rows))
    if not lottery_ids:
        return batch

    lottery_clause, lottery_values = _sql_in_values("evidence_lottery", lottery_ids)
    probe_account_predicate = ""
    shadow_account_predicate = ""
    scoped_values = dict(lottery_values)
    if account_id is not None:
        probe_account_predicate = "AND ac.account_id = :evidence_account_id"
        shadow_account_predicate = "AND tr.account_id = :evidence_account_id"
        scoped_values["evidence_account_id"] = account_id

    if any(not platform_has_api_real_adapter(lottery["platform"]) for lottery in lottery_rows):
        probe_rows = await database.fetch_all(
            f"""SELECT id, lottery_id, platform, account_id, result, status, created_at
                FROM (
                  SELECT ac.id, ac.lottery_id, ac.platform, ac.account_id,
                         ac.result, ac.status, ac.created_at,
                         ROW_NUMBER() OVER (
                           PARTITION BY ac.lottery_id, ac.platform
                           ORDER BY ac.id DESC
                         ) AS evidence_rank
                  FROM adapter_calibrations ac
                  WHERE ac.lottery_id IN ({lottery_clause})
                    AND ac.status = 'succeeded'
                    AND ac.created_at >= DATE_SUB(NOW(), INTERVAL 24 HOUR)
                    {probe_account_predicate}
                ) ranked_probes
                WHERE evidence_rank = 1
                ORDER BY lottery_id, platform""",
            scoped_values,
        )
        requested_pairs = {
            (int(lottery["id"]), str(lottery["platform"]).casefold())
            for lottery in lottery_rows
            if not platform_has_api_real_adapter(lottery["platform"])
        }
        for row in probe_rows:
            try:
                key = (int(row_value(row, "lottery_id")), str(row_value(row, "platform")).casefold())
            except (TypeError, ValueError):
                continue
            if key in requested_pairs:
                batch.probes.setdefault(key, row)

    shadow_rows = await database.fetch_all(
        f"""SELECT id, lottery_id, task_id, account_id, finished_at, screenshot_path
            FROM (
              SELECT tr.id, tr.lottery_id, tr.task_id, tr.account_id,
                     tr.finished_at, tr.screenshot_path,
                     ROW_NUMBER() OVER (
                       PARTITION BY tr.lottery_id
                       ORDER BY tr.id DESC
                     ) AS evidence_rank
              FROM task_runs tr
              WHERE tr.lottery_id IN ({lottery_clause})
                AND tr.task_mode = 'shadow_run'
                AND tr.status = 'succeeded'
                AND tr.finished_at >= DATE_SUB(NOW(), INTERVAL 24 HOUR)
                {shadow_account_predicate}
            ) ranked_shadows
            WHERE evidence_rank = 1
            ORDER BY lottery_id""",
        scoped_values,
    )
    for row in shadow_rows:
        try:
            lottery_id = int(row_value(row, "lottery_id"))
        except (TypeError, ValueError):
            continue
        if lottery_id in lottery_ids:
            batch.shadows.setdefault(lottery_id, row)

    selected_shadows = [row for row in batch.shadows.values() if row_value(row, "screenshot_path")]
    task_ids = list(
        dict.fromkeys(
            str(row_value(row, "task_id"))
            for row in selected_shadows
            if row_value(row, "task_id") is not None
        )
    )
    if not task_ids:
        return batch

    task_clause, task_values = _sql_in_values("shadow_task", task_ids)
    observation_rows = await database.fetch_all(
        f"""SELECT aggregate_id, payload, occurred_at
            FROM (
              SELECT e.aggregate_id, e.payload, e.occurred_at,
                     ROW_NUMBER() OVER (
                       PARTITION BY e.aggregate_id
                       ORDER BY e.occurred_at DESC
                     ) AS evidence_rank
              FROM events e
              WHERE e.aggregate = 'task'
                AND e.aggregate_id IN ({task_clause})
                AND e.correlation_id = e.aggregate_id
                AND e.event_type = 'TaskShadowRunObserved'
            ) ranked_observations
            WHERE evidence_rank = 1
            ORDER BY aggregate_id""",
        task_values,
    )
    task_ids_by_key = {task_id.casefold(): task_id for task_id in task_ids}
    for row in observation_rows:
        task_id_key = str(row_value(row, "aggregate_id") or "").casefold()
        selected_task_id = task_ids_by_key.get(task_id_key)
        if selected_task_id is not None:
            batch.observations.setdefault(selected_task_id.casefold(), row)

    evidence_values = dict(task_values)
    evidence_values.update(lottery_values)
    evidence_rows = await database.fetch_all(
        f"""SELECT id, task_id, account_id, lottery_id, file_path, sha256
            FROM (
              SELECT ef.id, ef.task_id, ef.account_id, ef.lottery_id,
                     ef.file_path, ef.sha256,
                     ROW_NUMBER() OVER (
                       PARTITION BY ef.task_id, ef.account_id, ef.lottery_id
                       ORDER BY ef.id DESC
                     ) AS evidence_rank
              FROM evidence_files ef
              WHERE ef.task_id IN ({task_clause})
                AND ef.lottery_id IN ({lottery_clause})
                AND ef.evidence_type = 'shadow_run_screenshot'
            ) ranked_evidence
            WHERE evidence_rank = 1
            ORDER BY task_id, account_id, lottery_id""",
        evidence_values,
    )
    for row in evidence_rows:
        try:
            key = (
                str(row_value(row, "task_id")).casefold(),
                str(row_value(row, "account_id")),
                int(row_value(row, "lottery_id")),
            )
        except (TypeError, ValueError):
            continue
        batch.evidence_files.setdefault(key, row)
    return batch


def _append_blocker(blockers: list[str], code: str) -> None:
    if code not in blockers:
        blockers.append(code)


def _exact_bilibili_evidence_observations_valid(
    row,
    *,
    dynamic_id: str,
    required_actions: tuple[str, ...],
    execution_revision: int,
    config_hash: str,
    follow_target_handle: str,
) -> bool:
    """Recompute both immutable observation hashes from their stored JSON."""

    if not row:
        return False
    try:
        validate_preflight_observation_binding(
            row_value(row, "probe_observation"),
            source_observation_kind=str(
                row_value(row, "probe_observation_kind") or ""
            ),
            source_observation_hash=str(
                row_value(row, "probe_observation_hash") or ""
            ),
            evidence_observation_kind=str(
                row_value(row, "evidence_probe_observation_kind") or ""
            ),
            evidence_observation_hash=str(
                row_value(row, "evidence_probe_observation_hash") or ""
            ),
            expected_dynamic_id=dynamic_id,
            expected_actions=required_actions,
            expected_execution_revision=execution_revision,
            expected_config_hash=config_hash,
            expected_follow_handle=follow_target_handle,
        )
        validate_preflight_observation_binding(
            row_value(row, "shadow_observation"),
            source_observation_kind=str(
                row_value(row, "shadow_observation_kind") or ""
            ),
            source_observation_hash=str(
                row_value(row, "shadow_observation_hash") or ""
            ),
            evidence_observation_kind=str(
                row_value(row, "evidence_shadow_observation_kind") or ""
            ),
            evidence_observation_hash=str(
                row_value(row, "evidence_shadow_observation_hash") or ""
            ),
            expected_dynamic_id=dynamic_id,
            expected_actions=required_actions,
            expected_execution_revision=execution_revision,
            expected_config_hash=config_hash,
            expected_follow_handle=follow_target_handle,
        )
    except (BilibiliPreflightEvidenceError, ActionPlanV2Error, TypeError, ValueError):
        return False
    return True


async def load_exact_bilibili_execution_evidence(
    *,
    lottery_id: int,
    account_id: int,
    rule_snapshot_id: int,
    execution_path_id: str,
    target_hash: str,
    rule_hash: str,
    action_plan_hash: str,
    config_hash: str,
    dynamic_id: str,
    required_actions: tuple[str, ...],
    execution_revision: int,
    follow_target_handle: str,
    evidence_id: str | None = None,
    for_update: bool = False,
):
    """Load one current, released, independently verifiable probe+shadow pair.

    The database predicates are part of the authorization boundary, but the
    observation JSON and hashes are still recomputed in Python.  This prevents
    a stale or manually edited source row from becoming executable merely
    because its foreign-key columns happen to match.
    """

    evidence_id_clause = "AND e.id = :evidence_id" if evidence_id else ""
    lock_clause = "FOR UPDATE" if for_update else ""
    values = {
        "lottery_id": lottery_id,
        "account_id": account_id,
        "rule_snapshot_id": rule_snapshot_id,
        "execution_path_id": execution_path_id,
        "target_hash": target_hash,
        "rule_hash": rule_hash,
        "action_plan_hash": action_plan_hash,
        "config_hash": config_hash,
    }
    if evidence_id:
        values["evidence_id"] = evidence_id
    row = await database.fetch_one(
        f"""SELECT e.id, e.probe_id, e.shadow_task_id, e.verified_at, e.expires_at,
                      e.probe_observation_kind AS evidence_probe_observation_kind,
                      e.probe_observation_hash AS evidence_probe_observation_hash,
                      e.shadow_observation_kind AS evidence_shadow_observation_kind,
                      e.shadow_observation_hash AS evidence_shadow_observation_hash,
                      ac.result AS probe_observation,
                      ac.observation_kind AS probe_observation_kind,
                      ac.observation_hash AS probe_observation_hash,
                      ac.finished_at AS probe_finished_at,
                      shadow.preflight_observation AS shadow_observation,
                      shadow.preflight_observation_kind AS shadow_observation_kind,
                      shadow.preflight_observation_hash AS shadow_observation_hash,
                      shadow.finished_at AS shadow_finished_at,
                      probe_lease.released_at AS probe_lease_released_at,
                      shadow_lease.released_at AS shadow_lease_released_at
               FROM execution_evidence_bindings e
               JOIN adapter_calibrations ac
                 ON ac.probe_id = e.probe_id
                AND ac.lottery_id = e.lottery_id
                AND ac.account_id = e.account_id
                AND ac.platform = e.platform
                AND ac.rule_snapshot_id = e.rule_snapshot_id
                AND ac.execution_path_id = e.execution_path_id
                AND ac.target_hash = e.target_hash
                AND ac.rule_hash = e.rule_hash
                AND ac.action_plan_hash = e.action_plan_hash
                AND ac.config_hash = e.config_hash
                AND ac.observation_kind = e.probe_observation_kind
                AND ac.observation_hash = e.probe_observation_hash
               JOIN task_runs shadow
                 ON shadow.task_id = e.shadow_task_id
                AND shadow.lottery_id = e.lottery_id
                AND shadow.account_id = e.account_id
                AND shadow.rule_snapshot_id = e.rule_snapshot_id
                AND shadow.execution_path_id = e.execution_path_id
                AND shadow.target_hash = e.target_hash
                AND shadow.rule_hash = e.rule_hash
                AND shadow.action_plan_hash = e.action_plan_hash
                AND shadow.config_hash = e.config_hash
                AND shadow.preflight_observation_kind = e.shadow_observation_kind
                AND shadow.preflight_observation_hash = e.shadow_observation_hash
               JOIN account_operation_leases probe_lease
                 ON probe_lease.lease_id = ac.account_lease_id
                AND probe_lease.account_id = ac.account_id
                AND probe_lease.generation = ac.account_lease_generation
                AND probe_lease.owner_id = ac.probe_id
                AND probe_lease.operation_kind = 'adapter_probe'
                AND probe_lease.task_id IS NULL
               JOIN account_operation_leases shadow_lease
                 ON shadow_lease.lease_id = shadow.account_lease_id
                AND shadow_lease.account_id = shadow.account_id
                AND shadow_lease.generation = shadow.account_lease_generation
                AND shadow_lease.owner_id = shadow.task_id
                AND shadow_lease.operation_kind = 'shadow_run'
                AND shadow_lease.task_id = shadow.task_id
               WHERE e.lottery_id = :lottery_id
                 AND e.account_id = :account_id
                 AND e.platform = 'bilibili'
                 AND e.rule_snapshot_id = :rule_snapshot_id
                 AND e.execution_path_id = :execution_path_id
                 AND e.target_hash = :target_hash
                 AND e.rule_hash = :rule_hash
                 AND e.action_plan_hash = :action_plan_hash
                 AND e.config_hash = :config_hash
                 {evidence_id_clause}
                 AND e.status = 'verified'
                 AND e.verified_at IS NOT NULL
                 AND e.verified_at <= NOW()
                 AND e.expires_at > NOW()
                 AND e.probe_id IS NOT NULL
                 AND e.shadow_task_id IS NOT NULL
                 AND ac.status = 'succeeded'
                 AND ac.finished_at IS NOT NULL
                 AND ac.finished_at >= DATE_SUB(NOW(), INTERVAL 24 HOUR)
                 AND ac.finished_at <= NOW()
                 AND shadow.task_mode = 'shadow_run'
                 AND shadow.status = 'succeeded'
                 AND shadow.finished_at IS NOT NULL
                 AND shadow.finished_at >= DATE_SUB(NOW(), INTERVAL 24 HOUR)
                 AND shadow.finished_at <= NOW()
                 AND probe_lease.released_at IS NOT NULL
                 AND probe_lease.acquired_at <= ac.finished_at
                 AND probe_lease.expires_at >= ac.finished_at
                 AND probe_lease.released_at >= ac.finished_at
                 AND probe_lease.released_at <= NOW()
                 AND shadow_lease.released_at IS NOT NULL
                 AND shadow_lease.acquired_at <= shadow.finished_at
                 AND shadow_lease.expires_at >= shadow.finished_at
                 AND shadow_lease.released_at >= shadow.finished_at
                 AND shadow_lease.released_at <= NOW()
                 AND e.verified_at >= GREATEST(ac.finished_at, shadow.finished_at)
                 AND e.expires_at <= LEAST(
                       DATE_ADD(ac.finished_at, INTERVAL 24 HOUR),
                       DATE_ADD(shadow.finished_at, INTERVAL 24 HOUR)
                     )
               ORDER BY e.verified_at DESC, e.id DESC
               LIMIT 1 {lock_clause}""",
        values,
    )
    if not _exact_bilibili_evidence_observations_valid(
        row,
        dynamic_id=dynamic_id,
        required_actions=required_actions,
        execution_revision=execution_revision,
        config_hash=config_hash,
        follow_target_handle=follow_target_handle,
    ):
        return None
    return row


async def validate_bilibili_v2_evidence(lottery, account_id: int | None) -> dict:
    """Validate the exact immutable Bilibili API-path execution contract."""

    blockers: list[str] = []
    lottery_data = dict(lottery)
    target = validate_lottery_target(lottery_data.get("platform"), lottery_data.get("raw_url"))
    if not target.valid:
        _append_blocker(blockers, "invalid_lottery_target")
    elif target.kind != "dynamic":
        _append_blocker(blockers, "bilibili_dynamic_target_required")

    raw_plan = parse_json_field(lottery_data.get("action_plan"))
    plan = None
    try:
        plan = validate_action_plan_v2(raw_plan, require_executable=False)
    except ActionPlanV2Error as exc:
        if exc.code == "action_plan_version_unsupported":
            _append_blocker(blockers, "lottery_action_plan_v2_required")
        elif exc.code == "action_plan_review_required":
            _append_blocker(blockers, "lottery_rule_review_required")
        else:
            _append_blocker(blockers, exc.code)

    rule_snapshot_ready = False
    semantic_ready = False
    if plan is not None:
        if plan.execution_path_id != BILIBILI_API_EXECUTION_PATH:
            _append_blocker(blockers, "bilibili_execution_path_not_supported")
        if plan.plan.get("platform") != "bilibili":
            _append_blocker(blockers, "action_plan_platform_mismatch")

        rule_text = str(lottery_data.get("rule_text") or "")
        if not rule_text.strip():
            _append_blocker(blockers, "lottery_rule_text_required")
        else:
            try:
                exact_rule_hash = compute_rule_hash(rule_text)
            except ActionPlanV2Error as exc:
                _append_blocker(blockers, exc.code)
                exact_rule_hash = ""
            try:
                authoritative_snapshot_id = int(
                    lottery_data.get("authoritative_rule_snapshot_id") or 0
                )
            except (TypeError, ValueError):
                authoritative_snapshot_id = 0
            if (
                not exact_rule_hash
                or plan.rule_hash != exact_rule_hash
                or str(lottery_data.get("rule_hash") or "") != exact_rule_hash
                or str(lottery_data.get("action_plan_hash") or "") != plan.plan_hash
                or authoritative_snapshot_id != plan.rule_snapshot_id
            ):
                _append_blocker(blockers, "action_plan_rule_binding_mismatch")
            else:
                snapshot = await database.fetch_one(
                    """SELECT id, platform, rule_hash, is_complete, attested_by, attested_at
                       FROM lottery_rule_snapshots
                       WHERE id = :snapshot_id
                         AND lottery_id = :lottery_id
                         AND platform = 'bilibili'
                         AND rule_hash = :rule_hash
                         AND BINARY rule_text = BINARY :rule_text
                         AND is_complete = 1
                         AND attested_by IS NOT NULL
                         AND attested_at IS NOT NULL
                       LIMIT 1""",
                    {
                        "snapshot_id": plan.rule_snapshot_id,
                        "lottery_id": lottery_data.get("id"),
                        "rule_hash": exact_rule_hash,
                        "rule_text": rule_text,
                    },
                )
                rule_snapshot_ready = bool(snapshot)
                if not rule_snapshot_ready:
                    _append_blocker(blockers, "authoritative_rule_snapshot_required")

            parsed_rule = parse_lottery_rule(rule_text, "bilibili")
            parsed_actions = [
                action
                for action in SHADOW_PHASE_ORDER
                if action in set(parsed_rule.get("required_actions") or [])
            ]
            represented, unresolved, capability = semantic_requirement_status(
                list(parsed_rule.get("unsupported_actions") or []),
                plan.action_payloads,
                parsed_rule.get("content_requirements") or {},
            )
            if not parsed_rule.get("is_lottery"):
                _append_blocker(blockers, "lottery_rule_not_recognized")
            if list(plan.required_actions) != parsed_actions:
                _append_blocker(blockers, "lottery_action_plan_stale")
            if parsed_rule.get("ambiguity_patterns"):
                _append_blocker(blockers, "lottery_rule_ambiguous")
            if unresolved:
                _append_blocker(blockers, "lottery_rule_requirements_unresolved")
            for code in capability:
                _append_blocker(blockers, code)
            semantic_ready = bool(
                parsed_rule.get("is_lottery")
                and list(plan.required_actions) == parsed_actions
                and not parsed_rule.get("ambiguity_patterns")
                and not unresolved
                and not capability
            )
            # Recomputed semantics, not the plan's own descriptive fields, are
            # authoritative.  A hand-edited plan cannot declare itself ready.
            if plan.plan.get("executable") is not True:
                _append_blocker(blockers, "lottery_action_plan_not_executable")
            if set(represented) != set(plan.plan.get("represented_requirements") or []):
                _append_blocker(blockers, "action_plan_requirement_binding_mismatch")
            if plan.content_requirements != dict(
                parsed_rule.get("content_requirements")
                or {
                    "follow_targets": [],
                    "commented": {"topic_tags": [], "mentions": []},
                    "reposted": {"topic_tags": [], "mentions": []},
                }
            ):
                _append_blocker(blockers, "action_plan_requirement_binding_mismatch")
            if set(unresolved) != set(plan.plan.get("unresolved_requirements") or []):
                _append_blocker(blockers, "action_plan_requirement_binding_mismatch")
            if set(capability) != set(plan.plan.get("capability_blockers") or []):
                _append_blocker(blockers, "action_plan_capability_binding_mismatch")

    action_plan_ready = bool(
        plan is not None
        and plan.plan.get("executable") is True
        and plan.execution_path_id == BILIBILI_API_EXECUTION_PATH
        and rule_snapshot_ready
        and semantic_ready
        and not any(
            blocker.startswith("action_plan_")
            or blocker.startswith("lottery_action_plan_")
            or blocker.startswith("lottery_rule_")
            or blocker.startswith("authoritative_rule_")
            or blocker.startswith("bilibili_media_")
            for blocker in blockers
        )
    )

    evidence = None
    execution_revision = None
    if account_id is None:
        _append_blocker(blockers, "execution_account_scope_required")
    else:
        account = await database.fetch_one(
            """SELECT id, platform, status, execution_revision,
                      OCTET_LENGTH(encrypted_credential) AS credential_size
               FROM accounts
               WHERE id = :account_id
                 AND deleted_at IS NULL
               LIMIT 1""",
            {"account_id": account_id},
        )
        try:
            execution_revision = int(row_value(account, "execution_revision") or 0)
        except (TypeError, ValueError):
            execution_revision = 0
        if (
            not account
            or str(row_value(account, "platform") or "").strip().lower() != "bilibili"
            or str(row_value(account, "status") or "").strip().lower() != "ready"
            or int(row_value(account, "credential_size") or 0) <= 0
            or execution_revision <= 0
        ):
            _append_blocker(blockers, "execution_account_not_ready")
        elif action_plan_ready and target.valid and target.kind == "dynamic":
            canonical_url = str(lottery_data.get("canonical_url") or "").strip()
            try:
                target_hash = compute_target_hash(canonical_url)
                dynamic_id = extract_bilibili_dynamic_id(
                    canonical_url, str(lottery_data.get("raw_url") or "")
                )
                config_hash = compute_bilibili_api_config_hash(execution_revision)
            except (ActionPlanV2Error, BilibiliPreflightEvidenceError) as exc:
                _append_blocker(blockers, getattr(exc, "code", str(exc)))
            else:
                evidence = await load_exact_bilibili_execution_evidence(
                    lottery_id=int(lottery_data.get("id")),
                    account_id=int(account_id),
                    rule_snapshot_id=plan.rule_snapshot_id,
                    execution_path_id=BILIBILI_API_EXECUTION_PATH,
                    target_hash=target_hash,
                    rule_hash=plan.rule_hash,
                    action_plan_hash=plan.plan_hash,
                    config_hash=config_hash,
                    dynamic_id=dynamic_id,
                    required_actions=plan.required_actions,
                    execution_revision=execution_revision,
                    follow_target_handle=plan.follow_target_handle,
                )

    execution_evidence_bound = bool(
        evidence
        and str(row_value(evidence, "id") or "").strip()
        and str(row_value(evidence, "probe_id") or "").strip()
        and str(row_value(evidence, "shadow_task_id") or "").strip()
        and row_value(evidence, "verified_at") is not None
    )
    if account_id is not None and not execution_evidence_bound:
        _append_blocker(blockers, "exact_execution_evidence_required")

    account_risk = None
    if account_id is not None:
        account_risk = await recent_account_risk(account_id)
        if account_risk["has_recent_risk"]:
            _append_blocker(blockers, "recent_account_risk_event")

    evidence_view = None
    if execution_evidence_bound:
        evidence_view = {
            "id": row_value(evidence, "id"),
            "status": "verified",
            "account_id": account_id,
            "lottery_id": lottery_data.get("id"),
            "rule_snapshot_id": plan.rule_snapshot_id if plan else None,
            "execution_path_id": BILIBILI_API_EXECUTION_PATH,
            "probe_id": row_value(evidence, "probe_id"),
            "shadow_task_id": row_value(evidence, "shadow_task_id"),
            "probe_observation_kind": row_value(
                evidence, "evidence_probe_observation_kind"
            ),
            "probe_observation_hash": row_value(
                evidence, "evidence_probe_observation_hash"
            ),
            "shadow_observation_kind": row_value(
                evidence, "evidence_shadow_observation_kind"
            ),
            "shadow_observation_hash": row_value(
                evidence, "evidence_shadow_observation_hash"
            ),
            "probe_finished_at": normalize_timestamp(
                row_value(evidence, "probe_finished_at")
            ),
            "shadow_finished_at": normalize_timestamp(
                row_value(evidence, "shadow_finished_at")
            ),
            "verified_at": normalize_timestamp(row_value(evidence, "verified_at")),
            "expires_at": normalize_timestamp(row_value(evidence, "expires_at")),
        }
    return {
        "allowed": not blockers,
        "blockers": blockers,
        "probe_ready": execution_evidence_bound,
        "shadow_ready": execution_evidence_bound,
        "action_plan_ready": action_plan_ready,
        "rule_snapshot_ready": rule_snapshot_ready,
        "execution_evidence_bound": execution_evidence_bound,
        "execution_evidence_id": row_value(evidence, "id") if evidence else None,
        "execution_evidence": evidence_view,
        "execution_path_id": BILIBILI_API_EXECUTION_PATH,
        "execution_revision": execution_revision,
        "account_risk": account_risk,
    }


async def validate_manual_only_contract(
    lottery,
    account_id: int | None = None,
    *,
    platform: str,
    execution_path_id: str,
    capability_blocker: str | None,
    execution_path_blocker: str | None = None,
    expected_executable: bool = False,
    media_capability_blocker: str = "bilibili_media_submission_unsupported",
    required_action_contract: tuple[str, ...] | None = None,
    manual_shadow_supported: bool = True,
    shadow_observation_blocker: str | None = None,
    evidence_batch: RealRunEvidenceBatch | None = None,
) -> dict:
    """Validate an exact manual checklist while always denying real-run.

    Selector observations may support a side-effect-free shadow run, but they
    cannot establish an official mutation capability.  Keeping plan readiness
    separate from execution capability lets operators review an exact
    checklist without ever turning that checklist into real-run proof.
    """

    blockers: list[str] = []
    if capability_blocker:
        blockers.append(capability_blocker)
    lottery_data = dict(lottery)
    target = validate_lottery_target(
        lottery_data.get("platform"), lottery_data.get("raw_url")
    )
    if not target.valid:
        _append_blocker(blockers, "invalid_lottery_target")

    raw_plan = parse_json_field(lottery_data.get("action_plan"))
    plan = None
    try:
        plan = validate_action_plan_v2(raw_plan, require_executable=False)
    except ActionPlanV2Error as exc:
        if exc.code == "action_plan_version_unsupported":
            _append_blocker(blockers, "lottery_action_plan_v2_required")
        elif exc.code == "action_plan_review_required":
            _append_blocker(blockers, "lottery_rule_review_required")
        else:
            _append_blocker(blockers, exc.code)

    rule_snapshot_ready = False
    semantic_ready = False
    capability_binding_ready = False
    if plan is not None:
        if plan.plan.get("platform") != platform:
            _append_blocker(blockers, "action_plan_platform_mismatch")
        if plan.execution_path_id != execution_path_id:
            _append_blocker(
                blockers,
                execution_path_blocker
                or (
                    "xiaohongshu_execution_path_not_supported"
                    if platform == "xiaohongshu"
                    else f"{platform}_execution_path_invalid"
                ),
            )
        if (
            required_action_contract is not None
            and tuple(plan.required_actions) != required_action_contract
        ):
            _append_blocker(blockers, "xiaohongshu_four_action_plan_required")
        if plan.plan.get("executable") is not expected_executable:
            _append_blocker(
                blockers,
                f"{platform}_manual_plan_must_be_non_executable"
                if not expected_executable
                else "lottery_action_plan_not_executable",
            )

        rule_text = str(lottery_data.get("rule_text") or "")
        if not rule_text.strip():
            _append_blocker(blockers, "lottery_rule_text_required")
        else:
            try:
                exact_rule_hash = compute_rule_hash(rule_text)
            except ActionPlanV2Error as exc:
                _append_blocker(blockers, exc.code)
                exact_rule_hash = ""
            try:
                authoritative_snapshot_id = int(
                    lottery_data.get("authoritative_rule_snapshot_id") or 0
                )
            except (TypeError, ValueError):
                authoritative_snapshot_id = 0
            if (
                not exact_rule_hash
                or plan.rule_hash != exact_rule_hash
                or str(lottery_data.get("rule_hash") or "") != exact_rule_hash
                or str(lottery_data.get("action_plan_hash") or "")
                != plan.plan_hash
                or authoritative_snapshot_id != plan.rule_snapshot_id
            ):
                _append_blocker(blockers, "action_plan_rule_binding_mismatch")
            else:
                snapshot = await database.fetch_one(
                    """SELECT id, platform, rule_hash, is_complete, attested_by, attested_at
                       FROM lottery_rule_snapshots
                       WHERE id = :snapshot_id
                         AND lottery_id = :lottery_id
                         AND platform = :platform
                         AND rule_hash = :rule_hash
                         AND BINARY rule_text = BINARY :rule_text
                         AND is_complete = 1
                         AND attested_by IS NOT NULL
                         AND attested_at IS NOT NULL
                       LIMIT 1""",
                    {
                        "snapshot_id": plan.rule_snapshot_id,
                        "lottery_id": lottery_data.get("id"),
                        "rule_hash": exact_rule_hash,
                        "rule_text": rule_text,
                        "platform": platform,
                    },
                )
                rule_snapshot_ready = bool(snapshot)
                if not rule_snapshot_ready:
                    _append_blocker(
                        blockers, "authoritative_rule_snapshot_required"
                    )

            parsed_rule = parse_lottery_rule(rule_text, platform)
            platform_action_order = action_order_for_platform(platform)
            parsed_actions = tuple(
                action
                for action in platform_action_order
                if action in set(parsed_rule.get("required_actions") or [])
            )
            source_content_requirements = dict(
                parsed_rule.get("content_requirements")
                or {
                    "follow_targets": [],
                    "commented": {"topic_tags": [], "mentions": []},
                    "reposted": {"topic_tags": [], "mentions": []},
                }
            )
            try:
                expected_friend_mentions = validate_friend_mention_requirements(
                    parsed_rule.get("friend_mention_requirements", {})
                )
            except ActionPlanV2Error:
                expected_friend_mentions = {}
                _append_blocker(
                    blockers,
                    "action_plan_friend_mention_requirement_binding_mismatch",
                )
            expected_content_requirements = bind_manual_follow_target(
                list(parsed_actions),
                plan.action_payloads,
                source_content_requirements,
            )
            expected_content_requirements = bind_manual_friend_mentions(
                plan.action_payloads,
                expected_content_requirements,
                expected_friend_mentions,
            )
            represented, unresolved, semantic_capability = (
                semantic_requirement_status(
                    list(parsed_rule.get("unsupported_actions") or []),
                    plan.action_payloads,
                    expected_content_requirements,
                    friend_mention_requirements=expected_friend_mentions,
                    source_content_requirements=source_content_requirements,
                    media_capability_blocker=media_capability_blocker,
                )
            )
            if not parsed_rule.get("is_lottery"):
                _append_blocker(blockers, "lottery_rule_not_recognized")
            expected_actions = (
                required_action_contract
                if required_action_contract is not None
                else parsed_actions
            )
            source_actions_ready = bool(
                parsed_actions == expected_actions
                and tuple(plan.required_actions) == expected_actions
            )
            if not source_actions_ready:
                _append_blocker(blockers, "lottery_action_plan_stale")
            if parsed_rule.get("ambiguity_patterns"):
                _append_blocker(blockers, "lottery_rule_ambiguous")
            if unresolved:
                _append_blocker(
                    blockers, "lottery_rule_requirements_unresolved"
                )
            for code in semantic_capability:
                _append_blocker(blockers, code)

            expected_capability = list(
                dict.fromkeys(
                    [
                        *semantic_capability,
                        *([capability_blocker] if capability_blocker else []),
                    ]
                )
            )
            capability_binding_ready = (
                list(plan.plan.get("capability_blockers") or [])
                == expected_capability
            )
            if not capability_binding_ready:
                _append_blocker(
                    blockers, "action_plan_capability_binding_mismatch"
                )
            if set(represented) != set(
                plan.plan.get("represented_requirements") or []
            ):
                _append_blocker(
                    blockers, "action_plan_requirement_binding_mismatch"
                )
            if set(unresolved) != set(
                plan.plan.get("unresolved_requirements") or []
            ):
                _append_blocker(
                    blockers, "action_plan_requirement_binding_mismatch"
                )
            if plan.content_requirements != expected_content_requirements:
                _append_blocker(
                    blockers, "action_plan_requirement_binding_mismatch"
                )
            if (
                "source_content_requirements" in plan.plan
                and plan.source_content_requirements
                != source_content_requirements
            ):
                _append_blocker(
                    blockers,
                    "action_plan_friend_mention_requirement_binding_mismatch",
                )
            if plan.friend_mention_requirements != expected_friend_mentions:
                _append_blocker(
                    blockers,
                    "action_plan_friend_mention_requirement_binding_mismatch",
                )
            semantic_ready = bool(
                parsed_rule.get("is_lottery")
                and source_actions_ready
                and bool(expected_actions)
                and not parsed_rule.get("ambiguity_patterns")
                and not unresolved
                and not semantic_capability
            )

    action_plan_ready = bool(
        plan is not None
        and plan.plan.get("executable") is expected_executable
        and plan.execution_path_id == execution_path_id
        and (
            required_action_contract is None
            or tuple(plan.required_actions) == required_action_contract
        )
        and rule_snapshot_ready
        and semantic_ready
        and capability_binding_ready
        and not any(
            blocker.startswith("action_plan_")
            or blocker.startswith("lottery_action_plan_")
            or blocker.startswith("lottery_rule_")
            or blocker.startswith("authoritative_rule_")
            or blocker.startswith("xiaohongshu_four_")
            or blocker.startswith(f"{platform}_execution_path_")
            or blocker.startswith(f"{platform}_manual_plan_")
            for blocker in blockers
        )
    )
    task_values = {"lottery_id": lottery_data.get("id")}
    account_filter = ""
    if account_id is not None:
        account_filter = "AND account_id = :account_id"
        task_values["account_id"] = account_id
    if not manual_shadow_supported:
        shadow = None
    elif evidence_batch is None:
        shadow = await database.fetch_one(
            f"""SELECT task_id, account_id, finished_at, screenshot_path
                FROM task_runs
                WHERE lottery_id = :lottery_id
                  AND task_mode = 'shadow_run'
                  AND status = 'succeeded'
                  AND finished_at >= DATE_SUB(NOW(), INTERVAL 24 HOUR)
                  {account_filter}
                ORDER BY id DESC
                LIMIT 1""",
            task_values,
        )
    else:
        shadow = evidence_batch.shadows.get(int(lottery_data.get("id")))

    selector_observation_complete = False
    if (
        manual_shadow_supported
        and plan is not None
        and shadow
        and row_value(shadow, "screenshot_path")
    ):
        shadow_task_id = row_value(shadow, "task_id")
        shadow_account_id = row_value(shadow, "account_id")
        expected_account_id = (
            account_id if account_id is not None else shadow_account_id
        )
        if evidence_batch is None:
            observation = await database.fetch_one(
                """SELECT payload
                   FROM events
                   WHERE aggregate = 'task'
                     AND aggregate_id = :task_id
                     AND correlation_id = :task_id
                     AND event_type = 'TaskShadowRunObserved'
                   ORDER BY occurred_at DESC
                   LIMIT 1""",
                {"task_id": shadow_task_id},
            )
            evidence_file = await database.fetch_one(
                """SELECT file_path, sha256
                   FROM evidence_files
                   WHERE task_id = :task_id
                     AND account_id = :account_id
                     AND lottery_id = :lottery_id
                     AND evidence_type = 'shadow_run_screenshot'
                   ORDER BY id DESC
                   LIMIT 1""",
                {
                    "task_id": shadow_task_id,
                    "account_id": expected_account_id,
                    "lottery_id": lottery_data.get("id"),
                },
            )
        else:
            observation = evidence_batch.observations.get(
                str(shadow_task_id).casefold()
            )
            evidence_file = None
            if expected_account_id is not None:
                evidence_file = evidence_batch.evidence_files.get(
                    (
                        str(shadow_task_id).casefold(),
                        str(expected_account_id),
                        int(lottery_data.get("id")),
                    )
                )
        observation_payload = parse_json_field(row_value(observation, "payload"))
        screenshot_path = str(row_value(shadow, "screenshot_path") or "")
        evidence_path = str(row_value(evidence_file, "file_path") or "")
        evidence_hash = str(row_value(evidence_file, "sha256") or "").lower()
        metadata_matches = bool(
            qualified_manual_shadow_observation(
                observation_payload,
                required_actions=plan.required_actions,
                capability_blocker=(
                    shadow_observation_blocker or capability_blocker or ""
                ),
            )
            and str(observation_payload.get("account_id"))
            == str(expected_account_id)
            and str(observation_payload.get("lottery_id"))
            == str(lottery_data.get("id"))
            and str(observation_payload.get("platform")) == platform
            and screenshot_path
            == str(observation_payload.get("screenshot_path") or "")
            and screenshot_path == evidence_path
        )
        if metadata_matches:
            selector_observation_complete = await asyncio.to_thread(
                shadow_screenshot_integrity_matches,
                evidence_path,
                evidence_hash,
                integrity_cache=(
                    evidence_batch.screenshot_integrity_cache
                    if evidence_batch is not None
                    else None
                ),
                hash_budget=(
                    evidence_batch.screenshot_hash_budget
                    if evidence_batch is not None
                    else None
                ),
            )

    return {
        "allowed": False,
        "blockers": blockers,
        "probe_ready": False,
        "shadow_ready": False,
        "action_plan_ready": action_plan_ready,
        "rule_snapshot_ready": rule_snapshot_ready,
        "execution_evidence_bound": False,
        "execution_evidence_id": None,
        "execution_evidence": None,
        "execution_path_id": execution_path_id,
        "execution_mode": "manual_assisted" if not expected_executable else "oauth",
        "real_run_supported": False,
        "capability_reason": capability_blocker,
        "manual_shadow_supported": manual_shadow_supported,
        "selector_observation_complete": selector_observation_complete,
        "manual_confirmation_required": True,
        "account_risk": None,
    }


def _parse_utc_timestamp(value) -> datetime | None:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
    ):
        return None
    token = value
    if token.endswith("Z"):
        token = f"{token[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(token)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def validate_weibo_oauth_capability_attestation(
    calibration_result,
    *,
    required_actions: tuple[str, ...] | list[str],
    account_id: int,
    execution_revision: int,
    calibration_fresh: bool,
    expected_calibration_id: str | None = None,
    expected_uid: str | None = None,
    now: datetime | None = None,
) -> dict:
    """Validate a non-secret, account-bound official OAuth capability proof."""

    blockers: list[str] = []
    denied_actions: list[str] = []
    parsed = parse_json_field(calibration_result)
    capabilities = (
        parsed.get("oauth_capabilities")
        if isinstance(parsed, dict)
        else None
    )
    if not isinstance(capabilities, dict):
        return {
            "ready": False,
            "blockers": ["weibo_oauth_capability_evidence_required"],
            "denied_actions": [],
            "evidence": None,
        }
    if expected_uid is not None:
        identity = parsed.get("identity") if isinstance(parsed, dict) else None
        if (
            not isinstance(identity, dict)
            or identity.get("verified") is not True
            or identity.get("method") != "weibo_account_get_uid"
            or str(identity.get("uid") or "") != str(expected_uid)
        ):
            _append_blocker(
                blockers, "weibo_oauth_identity_verification_required"
            )
    if set(capabilities) != WEIBO_OAUTH_ATTESTATION_KEYS:
        _append_blocker(blockers, "weibo_oauth_capability_contract_mismatch")
    if (
        type(capabilities.get("contract_version")) is not int
        or capabilities.get("contract_version")
        != WEIBO_OAUTH_CAPABILITY_CONTRACT_VERSION
    ):
        _append_blocker(blockers, "weibo_oauth_capability_contract_mismatch")
    capability_calibration_id = capabilities.get("calibration_id")
    if (
        not isinstance(capability_calibration_id, str)
        or not capability_calibration_id
        or capability_calibration_id != capability_calibration_id.strip()
        or len(capability_calibration_id) > 128
        or (
            expected_calibration_id is not None
            and capability_calibration_id != expected_calibration_id
        )
    ):
        _append_blocker(blockers, "weibo_oauth_capability_contract_mismatch")
    if (
        type(capabilities.get("account_id")) is not int
        or capabilities.get("account_id") != account_id
    ):
        _append_blocker(blockers, "weibo_oauth_capability_contract_mismatch")
    if (
        type(capabilities.get("execution_revision")) is not int
        or capabilities.get("execution_revision") != execution_revision
    ):
        _append_blocker(blockers, "weibo_oauth_execution_revision_mismatch")
    if capabilities.get("credential_kind") != "weibo_oauth":
        _append_blocker(blockers, "weibo_oauth_credential_kind_invalid")
    if capabilities.get("identity_verified") is not True:
        _append_blocker(blockers, "weibo_oauth_identity_verification_required")
    if (
        capabilities.get("evidence_source")
        != "operator_attested_app_capabilities"
    ):
        _append_blocker(blockers, "weibo_oauth_capability_contract_mismatch")
    attested_by = capabilities.get("attested_by")
    if (
        not isinstance(attested_by, str)
        or not attested_by
        or attested_by != attested_by.strip()
        or len(attested_by.encode("utf-8")) > 128
    ):
        _append_blocker(blockers, "weibo_oauth_capability_contract_mismatch")
    if capabilities.get("app_review_status") not in {
        "approved",
        "test_only",
        "unknown",
    }:
        _append_blocker(blockers, "weibo_oauth_capability_contract_mismatch")
    elif capabilities.get("app_review_status") != "approved":
        _append_blocker(blockers, "weibo_oauth_app_review_required")
    if capabilities.get("client_type") not in {"weibo", "other"}:
        _append_blocker(blockers, "weibo_oauth_capability_contract_mismatch")
    if calibration_fresh is not True:
        _append_blocker(blockers, "weibo_oauth_capability_evidence_stale")

    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        _append_blocker(blockers, "weibo_oauth_capability_evidence_stale")
        current = datetime.now(timezone.utc)
    current = current.astimezone(timezone.utc)
    verified_at = _parse_utc_timestamp(capabilities.get("verified_at"))
    attested_at = _parse_utc_timestamp(capabilities.get("attested_at"))
    if (
        verified_at is None
        or verified_at > current
        or current - verified_at > WEIBO_OAUTH_CAPABILITY_MAX_AGE
    ):
        _append_blocker(blockers, "weibo_oauth_capability_evidence_stale")
    if (
        attested_at is None
        or attested_at > current
        or current - attested_at > WEIBO_OAUTH_CAPABILITY_MAX_AGE
        or (verified_at is not None and attested_at > verified_at)
    ):
        _append_blocker(blockers, "weibo_oauth_capability_evidence_stale")

    expected = weibo_runtime_capability_requirements(required_actions)
    declared_actions = capabilities.get("actions")
    if (
        not isinstance(declared_actions, dict)
        or set(declared_actions) != set(WEIBO_ACTION_ORDER)
    ):
        declared_actions = {}
        _append_blocker(blockers, "weibo_oauth_capability_contract_mismatch")
    else:
        for action in WEIBO_ACTION_ORDER:
            evidence = declared_actions.get(action)
            expected_requirement = WEIBO_ACTION_CAPABILITY_REQUIREMENTS[action]
            if (
                not isinstance(evidence, dict)
                or set(evidence) != WEIBO_OAUTH_ACTION_ATTESTATION_KEYS
                or evidence.get("endpoint")
                != expected_requirement["endpoint"]
                or evidence.get("permission")
                != expected_requirement["permission"]
                or type(evidence.get("granted")) is not bool
            ):
                _append_blocker(
                    blockers, "weibo_oauth_capability_contract_mismatch"
                )
    for action, requirement in expected["actions"].items():
        evidence = declared_actions.get(action)
        if (
            not isinstance(evidence, dict)
            or set(evidence) != WEIBO_OAUTH_ACTION_ATTESTATION_KEYS
            or evidence.get("endpoint") != requirement["endpoint"]
            or evidence.get("permission") != requirement["permission"]
            or type(evidence.get("granted")) is not bool
        ):
            _append_blocker(blockers, "weibo_oauth_capability_contract_mismatch")
            denied_actions.append(action)
            continue
        if evidence.get("granted") is not True:
            denied_actions.append(action)
    denied_actions = [
        action for action in expected["actions"] if action in set(denied_actions)
    ]
    if denied_actions:
        _append_blocker(blockers, "weibo_oauth_action_capability_denied")
    if (
        "followed" in expected["actions"]
        and capabilities.get("client_type") != "weibo"
    ):
        _append_blocker(blockers, "weibo_oauth_follow_client_type_required")

    evidence_view = {
        "contract_version": capabilities.get("contract_version"),
        "calibration_id": capabilities.get("calibration_id"),
        "account_id": capabilities.get("account_id"),
        "execution_revision": capabilities.get("execution_revision"),
        "credential_kind": capabilities.get("credential_kind"),
        "identity_verified": capabilities.get("identity_verified") is True,
        "app_review_status": capabilities.get("app_review_status"),
        "client_type": capabilities.get("client_type"),
        "verified_at": capabilities.get("verified_at"),
        "evidence_source": capabilities.get("evidence_source"),
        "attested_by": capabilities.get("attested_by"),
        "attested_at": capabilities.get("attested_at"),
        "granted_actions": [
            action
            for action in expected["actions"]
            if isinstance(declared_actions.get(action), dict)
            and declared_actions[action].get("granted") is True
        ],
        "denied_actions": denied_actions,
        "secret_material_exposed": False,
    }
    return {
        "ready": not blockers,
        "blockers": blockers,
        "denied_actions": denied_actions,
        "evidence": evidence_view,
    }


async def validate_xiaohongshu_manual_contract(
    lottery,
    account_id: int | None = None,
    *,
    evidence_batch: RealRunEvidenceBatch | None = None,
) -> dict:
    """Validate XHS's exact four-action manual-only contract."""

    return await validate_manual_only_contract(
        lottery,
        account_id=account_id,
        platform="xiaohongshu",
        execution_path_id=XIAOHONGSHU_MANUAL_EXECUTION_PATH,
        capability_blocker=XIAOHONGSHU_NO_OFFICIAL_API_BLOCKER,
        required_action_contract=XIAOHONGSHU_ACTION_ORDER,
        evidence_batch=evidence_batch,
    )


async def validate_douyin_manual_contract(
    lottery,
    account_id: int | None = None,
    *,
    evidence_batch: RealRunEvidenceBatch | None = None,
) -> dict:
    """Validate a variable-action Douyin manual-only contract."""

    return await validate_manual_only_contract(
        lottery,
        account_id=account_id,
        platform="douyin",
        execution_path_id=DOUYIN_MANUAL_EXECUTION_PATH,
        capability_blocker=DOUYIN_NO_OFFICIAL_API_BLOCKER,
        media_capability_blocker="douyin_media_submission_unsupported",
        evidence_batch=evidence_batch,
    )


async def validate_weibo_manual_contract(
    lottery,
    account_id: int | None = None,
    *,
    evidence_batch: RealRunEvidenceBatch | None = None,
) -> dict:
    """Validate the explicit Weibo checklist fallback; never allow writes."""

    return await validate_manual_only_contract(
        lottery,
        account_id=account_id,
        platform="weibo",
        execution_path_id=WEIBO_MANUAL_EXECUTION_PATH,
        capability_blocker=WEIBO_MANUAL_EXECUTION_BLOCKER,
        execution_path_blocker="weibo_execution_path_invalid",
        media_capability_blocker="weibo_media_submission_unsupported",
        evidence_batch=evidence_batch,
    )


async def validate_weibo_oauth_contract(
    lottery,
    account_id: int | None = None,
    *,
    evidence_batch: RealRunEvidenceBatch | None = None,
) -> dict:
    """Validate Weibo OAuth semantics and fresh per-action capability proof."""

    result = await validate_manual_only_contract(
        lottery,
        account_id=account_id,
        platform="weibo",
        execution_path_id=WEIBO_OAUTH_EXECUTION_PATH,
        capability_blocker=None,
        execution_path_blocker="weibo_execution_path_invalid",
        expected_executable=True,
        media_capability_blocker="weibo_media_submission_unsupported",
        # Official OAuth mutations are independently authenticated and
        # capability-attested. A selector shadow requires a browser-session
        # credential, which cannot coexist in the execution account's single
        # credential slot; making it a prerequisite would make this path
        # unreachable or encourage stale cross-account evidence reuse.
        manual_shadow_supported=False,
        evidence_batch=evidence_batch,
    )
    blockers = list(result.get("blockers") or [])
    capability = {
        "ready": False,
        "blockers": [],
        "denied_actions": [],
        "evidence": None,
    }
    execution_revision = None
    calibration_id = None
    credential_present = False
    account_risk = None
    oauth_dry_run_ready = False
    oauth_dry_run_task_id = None
    if account_id is None:
        _append_blocker(blockers, "weibo_oauth_account_scope_required")
    else:
        account = await database.fetch_one(
            """SELECT a.id, a.status, a.execution_revision,
                      a.encrypted_credential,
                      c.calibration_id, c.status AS calibration_status,
                      c.result AS calibration_result,
                      (c.created_at >= DATE_SUB(NOW(), INTERVAL 24 HOUR)) AS calibration_fresh
                 FROM accounts a
                 LEFT JOIN account_calibrations c
                   ON c.id = (
                     SELECT latest.id
                     FROM account_calibrations latest
                     WHERE latest.account_id = a.id
                       AND latest.platform = 'weibo'
                     ORDER BY latest.id DESC
                     LIMIT 1
                   )
                 WHERE a.id = :account_id
                   AND a.platform = 'weibo'
                   AND a.deleted_at IS NULL
                 LIMIT 1""",
            {"account_id": account_id},
        )
        if not account:
            _append_blocker(blockers, "weibo_oauth_account_scope_required")
        else:
            execution_revision = int(row_value(account, "execution_revision") or 0)
            calibration_id = row_value(account, "calibration_id")
            if str(row_value(account, "status") or "").lower() != "ready":
                _append_blocker(blockers, "execution_account_not_ready")
            encrypted_credential = row_value(account, "encrypted_credential")
            oauth_credential = None
            if not encrypted_credential:
                _append_blocker(blockers, "weibo_oauth_credential_required")
            else:
                try:
                    decrypted_credential = cookie_vault.decrypt(
                        encrypted_credential,
                        aad=CREDENTIAL_AAD,
                    )
                    oauth_credential = parse_weibo_oauth_credential(
                        decrypted_credential
                    )
                except WeiboOAuthCredentialError as exc:
                    _append_blocker(blockers, exc.code)
                except Exception:
                    _append_blocker(
                        blockers, "weibo_oauth_credential_invalid"
                    )
            credential_present = oauth_credential is not None
            if row_value(account, "calibration_status") != "succeeded":
                _append_blocker(
                    blockers, "weibo_oauth_capability_evidence_required"
                )
            else:
                plan = None
                try:
                    plan = validate_action_plan_v2(
                        parse_json_field(dict(lottery).get("action_plan")),
                        require_executable=True,
                    )
                except ActionPlanV2Error:
                    pass
                if plan is not None:
                    capability = validate_weibo_oauth_capability_attestation(
                        row_value(account, "calibration_result"),
                        required_actions=plan.required_actions,
                        account_id=account_id,
                        execution_revision=execution_revision,
                        calibration_fresh=bool(
                            row_value(account, "calibration_fresh")
                        ),
                        expected_calibration_id=str(calibration_id or ""),
                        expected_uid=(
                            oauth_credential.get("uid")
                            if oauth_credential is not None
                            else None
                        ),
                    )
                    for blocker in capability["blockers"]:
                        _append_blocker(blockers, blocker)
                    try:
                        dry_config_hash = compute_config_hash(
                            {
                                "execution_path_id": WEIBO_OAUTH_EXECUTION_PATH,
                                "execution_revision": execution_revision,
                                "runtime_capability_requirements": (
                                    plan.runtime_capability_requirements
                                ),
                                "weibo_rip_hash": "",
                            }
                        )
                        dry_run = await database.fetch_one(
                            """SELECT tr.task_id
                                 FROM task_runs tr
                                 JOIN account_operation_leases lease
                                   ON lease.lease_id = tr.account_lease_id
                                  AND lease.account_id = tr.account_id
                                  AND lease.generation = tr.account_lease_generation
                                  AND lease.owner_id = tr.task_id
                                  AND lease.task_id = tr.task_id
                                  AND lease.operation_kind = 'dry_run'
                                WHERE tr.lottery_id = :lottery_id
                                  AND tr.account_id = :account_id
                                  AND tr.task_mode = 'dry_run'
                                  AND tr.status = 'succeeded'
                                  AND tr.finished_at IS NOT NULL
                                  AND tr.finished_at >= DATE_SUB(NOW(), INTERVAL 24 HOUR)
                                  AND tr.finished_at <= NOW()
                                  AND tr.rule_snapshot_id = :rule_snapshot_id
                                  AND tr.execution_path_id = :execution_path_id
                                  AND tr.target_hash = :target_hash
                                  AND tr.rule_hash = :rule_hash
                                  AND tr.action_plan_hash = :action_plan_hash
                                  AND tr.config_hash = :config_hash
                                  AND lease.released_at IS NOT NULL
                                  AND lease.released_at >= tr.finished_at
                                  AND lease.released_at <= NOW()
                                ORDER BY tr.finished_at DESC, tr.id DESC
                                LIMIT 1""",
                            {
                                "lottery_id": dict(lottery).get("id"),
                                "account_id": account_id,
                                "rule_snapshot_id": plan.rule_snapshot_id,
                                "execution_path_id": WEIBO_OAUTH_EXECUTION_PATH,
                                "target_hash": compute_target_hash(
                                    str(dict(lottery).get("canonical_url") or "")
                                ),
                                "rule_hash": plan.rule_hash,
                                "action_plan_hash": plan.plan_hash,
                                "config_hash": dry_config_hash,
                            },
                        )
                    except (ActionPlanV2Error, TypeError, ValueError):
                        dry_run = None
                    if dry_run:
                        oauth_dry_run_ready = True
                        oauth_dry_run_task_id = str(
                            row_value(dry_run, "task_id") or ""
                        ) or None
                    else:
                        _append_blocker(
                            blockers, "recent_oauth_dry_run_required"
                        )
            account_risk = await recent_account_risk(account_id)
            if account_risk["has_recent_risk"]:
                _append_blocker(blockers, "recent_account_risk_event")

    capability_ready = bool(
        credential_present
        and capability.get("ready")
        and result.get("action_plan_ready")
    )
    result.update(
        {
            "allowed": (
                not blockers and capability_ready and oauth_dry_run_ready
            ),
            "blockers": blockers,
            "probe_ready": capability_ready,
            "shadow_ready": False,
            "execution_preflight_ready": oauth_dry_run_ready,
            "oauth_dry_run_ready": oauth_dry_run_ready,
            "oauth_dry_run_task_id": oauth_dry_run_task_id,
            "execution_evidence_bound": capability_ready,
            "execution_evidence_id": calibration_id if capability_ready else None,
            "execution_evidence": capability.get("evidence"),
            "execution_path_id": WEIBO_OAUTH_EXECUTION_PATH,
            "execution_revision": execution_revision,
            "execution_mode": "oauth",
            "real_run_supported": True,
            "capability_reason": blockers[0] if blockers else None,
            "oauth_capability_ready": capability_ready,
            "oauth_capability_denied_actions": capability.get(
                "denied_actions", []
            ),
            "manual_confirmation_required": True,
            "account_risk": account_risk,
        }
    )
    return result


async def validate_real_run_evidence(
    lottery,
    account_id: int | None = None,
    *,
    evidence_batch: RealRunEvidenceBatch | None = None,
) -> dict:
    if evidence_batch is not None and evidence_batch.account_id != account_id:
        raise ValueError("real-run evidence batch account scope mismatch")
    if lottery["platform"] == "xiaohongshu":
        return await validate_xiaohongshu_manual_contract(
            lottery,
            account_id=account_id,
            evidence_batch=evidence_batch,
        )
    if lottery["platform"] == "douyin":
        return await validate_douyin_manual_contract(
            lottery,
            account_id=account_id,
            evidence_batch=evidence_batch,
        )
    if lottery["platform"] == "weibo":
        raw_plan = parse_json_field(dict(lottery).get("action_plan"))
        execution_path_id = (
            str(raw_plan.get("execution_path_id") or "")
            if isinstance(raw_plan, dict)
            else ""
        )
        if execution_path_id == WEIBO_MANUAL_EXECUTION_PATH:
            return await validate_weibo_manual_contract(
                lottery,
                account_id=account_id,
                evidence_batch=evidence_batch,
            )
        return await validate_weibo_oauth_contract(
            lottery,
            account_id=account_id,
            evidence_batch=evidence_batch,
        )
    if platform_has_api_real_adapter(lottery["platform"]):
        # Bilibili's API execution path has a stronger v2 contract.  Browser
        # selector observations and legacy v1 plans can never substitute for
        # an exact probe+shadow binding.
        return await validate_bilibili_v2_evidence(lottery, account_id)
    blockers = []
    target = validate_lottery_target(lottery["platform"], lottery["raw_url"])
    if not target.valid:
        blockers.append("invalid_lottery_target")
    if platform_has_api_real_adapter(lottery["platform"]) and target.valid and target.kind != "dynamic":
        blockers.append("bilibili_dynamic_target_required")
    lottery_data = dict(lottery)
    parsed_action_plan = parse_json_field(lottery_data.get("action_plan"))
    action_plan = parsed_action_plan if isinstance(parsed_action_plan, dict) else {}
    required_actions = action_plan.get("required_actions", [])
    required_actions_valid = bool(
        isinstance(required_actions, list)
        and required_actions
        and all(
            isinstance(action, str) and action in SHADOW_PHASE_ORDER
            for action in required_actions
        )
        and len(required_actions) == len(set(required_actions))
    )
    missing_rule_actions = (
        action_plan_missing_rule_actions(lottery_data, action_plan)
        if required_actions_valid
        else []
    )
    if not action_plan:
        blockers.append("lottery_action_plan_required")
    elif action_plan.get("review_required") is not False:
        blockers.append("lottery_rule_review_required")
    elif not required_actions:
        blockers.append("lottery_required_actions_missing")
    elif not required_actions_valid:
        # Keep the existing blocker vocabulary while failing closed on legacy
        # or manually-written plans that the Worker would reject (wrong JSON
        # shape, unknown actions, or duplicates).
        blockers.append("lottery_rule_review_required")
    elif missing_rule_actions:
        blockers.append("lottery_action_plan_stale")
    rule_requires_review = False
    rule_text = str(lottery_data.get("rule_text") or "").strip()
    if not rule_text:
        blockers.append("lottery_rule_text_required")
    else:
        current_rule = parse_lottery_rule(rule_text, lottery["platform"])
        if current_rule.get("review_required") or current_rule.get("unsupported_actions"):
            rule_requires_review = True
            if "lottery_rule_review_required" not in blockers:
                blockers.append("lottery_rule_review_required")
    probe_values = {"platform": lottery["platform"], "lottery_id": lottery["id"]}
    task_values = {"lottery_id": lottery["id"]}
    account_filter = ""
    if account_id is not None:
        account_filter = "AND account_id = :account_id"
        probe_values["account_id"] = account_id
        task_values["account_id"] = account_id

    probe_summary = None
    if not platform_has_api_real_adapter(lottery["platform"]):
        if evidence_batch is None:
            probe = await database.fetch_one(
                f"""SELECT result, status, created_at
                    FROM adapter_calibrations
                    WHERE platform = :platform
                      AND lottery_id = :lottery_id
                      AND status = 'succeeded'
                      AND created_at >= DATE_SUB(NOW(), INTERVAL 24 HOUR)
                      {account_filter}
                    ORDER BY id DESC
                    LIMIT 1""",
                probe_values,
            )
        else:
            probe = evidence_batch.probes.get(
                (int(lottery["id"]), str(lottery["platform"]).casefold())
            )
        if probe and probe["result"]:
            probe_result = parse_json_field(probe["result"])
            probe_summary = probe_result.get("_summary") if isinstance(probe_result, dict) else None
    probe_ready = platform_probe_ready_for_real_actions(lottery["platform"], probe_summary)
    if not probe_ready:
        if platform_has_api_real_adapter(lottery["platform"]):
            blockers.append("api_path_probe_evidence_not_implemented")
        elif lottery["platform"] in STRUCTURED_SELECTOR_PLATFORMS:
            blockers.append("selector_config_evidence_binding_not_implemented")
        else:
            blockers.append("recent_complete_probe_required")

    if evidence_batch is None:
        shadow = await database.fetch_one(
            f"""SELECT task_id, account_id, finished_at
                       , screenshot_path
                FROM task_runs
                WHERE lottery_id = :lottery_id
                  AND task_mode = 'shadow_run'
                  AND status = 'succeeded'
                  AND finished_at >= DATE_SUB(NOW(), INTERVAL 24 HOUR)
                  {account_filter}
                ORDER BY id DESC
                LIMIT 1""",
            task_values,
        )
    else:
        shadow = evidence_batch.shadows.get(int(lottery["id"]))
    shadow_ready = False
    if shadow and row_value(shadow, "screenshot_path"):
        shadow_task_id = row_value(shadow, "task_id")
        shadow_account_id = row_value(shadow, "account_id")
        expected_account_id = account_id if account_id is not None else shadow_account_id
        if evidence_batch is None:
            observation = await database.fetch_one(
                """SELECT payload
                   FROM events
                   WHERE aggregate = 'task'
                     AND aggregate_id = :task_id
                     AND correlation_id = :task_id
                     AND event_type = 'TaskShadowRunObserved'
                   ORDER BY occurred_at DESC
                   LIMIT 1""",
                {"task_id": shadow_task_id},
            )
            evidence_file = await database.fetch_one(
                """SELECT file_path, sha256
                   FROM evidence_files
                   WHERE task_id = :task_id
                     AND account_id = :account_id
                     AND lottery_id = :lottery_id
                     AND evidence_type = 'shadow_run_screenshot'
                   ORDER BY id DESC
                   LIMIT 1""",
                {
                    "task_id": shadow_task_id,
                    "account_id": expected_account_id,
                    "lottery_id": lottery["id"],
                },
            )
        else:
            observation = evidence_batch.observations.get(str(shadow_task_id).casefold())
            evidence_file = None
            if expected_account_id is not None:
                evidence_file = evidence_batch.evidence_files.get(
                    (str(shadow_task_id).casefold(), str(expected_account_id), int(lottery["id"]))
                )
        observation_payload = parse_json_field(row_value(observation, "payload"))
        screenshot_path = str(row_value(shadow, "screenshot_path") or "")
        evidence_path = str(row_value(evidence_file, "file_path") or "")
        evidence_hash = str(row_value(evidence_file, "sha256") or "").lower()
        evidence_metadata_matches = bool(
            qualified_shadow_observation(observation_payload, [str(action) for action in required_actions])
            and str(observation_payload.get("account_id")) == str(expected_account_id)
            and str(observation_payload.get("lottery_id")) == str(lottery["id"])
            and str(observation_payload.get("platform")) == str(lottery["platform"])
            and screenshot_path == str(observation_payload.get("screenshot_path") or "")
            and screenshot_path == evidence_path
        )
        if evidence_metadata_matches:
            # Hashing up to the evidence-size cap is blocking filesystem/CPU
            # work. Keep it off the async API event loop while preserving the
            # request-scoped identity cache and fail-closed result.
            shadow_ready = await asyncio.to_thread(
                shadow_screenshot_integrity_matches,
                evidence_path,
                evidence_hash,
                integrity_cache=evidence_batch.screenshot_integrity_cache if evidence_batch is not None else None,
                hash_budget=evidence_batch.screenshot_hash_budget if evidence_batch is not None else None,
            )
            if evidence_batch is not None:
                budget = evidence_batch.screenshot_hash_budget
                if budget.get("exhausted") and not budget.get("exhaustion_logged"):
                    budget["exhaustion_logged"] = 1
                    structured_log(
                        "warning",
                        "real_run_evidence_hash_budget_exhausted",
                        lottery_id=lottery["id"],
                        remaining_bytes=max(int(budget.get("remaining", 0)), 0),
                        required_bytes=max(int(budget.get("required_bytes", 0)), 0),
                        request_budget_bytes=MAX_EVIDENCE_REQUEST_HASH_BYTES,
                    )
    if not shadow_ready:
        blockers.append("recent_shadow_run_required")

    account_risk = None
    if account_id is not None:
        account_risk = await recent_account_risk(account_id)
        if account_risk["has_recent_risk"]:
            blockers.append("recent_account_risk_event")

    return {
        "allowed": not blockers,
        "blockers": blockers,
        "probe_ready": probe_ready,
        "shadow_ready": shadow_ready,
        "action_plan_ready": bool(
            rule_text
            and action_plan
            and required_actions_valid
            and action_plan.get("review_required") is False
            and not missing_rule_actions
            and not rule_requires_review
        ),
        "account_risk": account_risk,
    }


async def emit_real_run_gate_notification(lottery, reason, *, actor_id: str | None = None):
    platform = lottery["platform"]
    if platform not in STRUCTURED_SELECTOR_PLATFORMS:
        return
    platform_label = (get_platform(platform) or {}).get("label", platform)

    blockers = extract_real_run_blockers(reason)
    next_action = next_action_for_blockers(blockers)
    content_lines = [
        f"Platform: {platform}",
        f"Lottery: L{lottery['id']}",
        f"URL: {lottery['canonical_url'] or lottery['raw_url']}",
        f"Next action: {next_action}",
    ]
    if blockers:
        content_lines.append(f"Blockers: {', '.join(blockers)}")
    else:
        content_lines.append(f"Reason: {format_real_run_reason(reason)}")
    if actor_id:
        content_lines.append(f"Actor: {actor_id}")

    try:
        await redis.xadd(
            "notify_events",
            {
                "event_type": f"{platform}.real_run_gate.blocked",
                "severity": "warning",
                "title": f"{platform_label} real-run gate blocked: L{lottery['id']}",
                "content": "\n".join(content_lines),
                "channels": "all",
            },
        )
    except Exception as exc:
        structured_log(
            "error",
            "real_run_gate_notification_failed",
            lottery_id=lottery["id"],
            platform=lottery["platform"],
            exception=exc,
        )


def extract_real_run_blockers(reason) -> list[str]:
    if isinstance(reason, dict):
        blockers = reason.get("blockers")
        if isinstance(blockers, list):
            return [str(item) for item in blockers]
        nested = reason.get("reason")
        if nested is not None:
            return extract_real_run_blockers(nested)
    return []


def next_action_for_blockers(blockers: list[str]) -> str:
    if "invalid_lottery_target" in blockers:
        return "add_target"
    if "bilibili_dynamic_target_required" in blockers:
        return "add_target"
    if any(
        blocker in blockers
        for blocker in (
            "lottery_action_plan_required",
            "lottery_rule_text_required",
            "lottery_rule_review_required",
            "lottery_required_actions_missing",
            "lottery_action_plan_stale",
            "lottery_action_plan_v2_required",
            "lottery_action_plan_not_executable",
            "lottery_rule_requirements_unresolved",
            "lottery_rule_ambiguous",
            "authoritative_rule_snapshot_required",
            "action_plan_rule_binding_mismatch",
        )
    ):
        return "review_rule"
    if (
        XIAOHONGSHU_NO_OFFICIAL_API_BLOCKER in blockers
        or DOUYIN_NO_OFFICIAL_API_BLOCKER in blockers
        or WEIBO_MANUAL_EXECUTION_BLOCKER in blockers
    ):
        return "manual_assisted"
    if "weibo_oauth_account_scope_required" in blockers:
        return "select_account"
    if any(
        str(blocker).startswith("weibo_oauth_")
        for blocker in blockers
    ):
        return "configure_oauth"
    if "no_calibrated_ready_account" in blockers:
        return "add_account"
    if "recent_account_risk_event" in blockers:
        return "review_risk"
    if any(
        blocker in blockers
        for blocker in (
            "api_path_probe_evidence_not_implemented",
            "selector_config_evidence_binding_not_implemented",
        )
    ):
        return "blocked"
    if "recent_complete_probe_required" in blockers:
        return "probe"
    if "exact_execution_evidence_required" in blockers:
        return "probe"
    if "execution_account_scope_required" in blockers:
        return "select_account"
    if "real_adapter_not_enabled" in blockers:
        return "configure_adapter"
    if "recent_shadow_run_required" in blockers:
        return "shadow_run"
    if "global_real_run_disabled" in blockers:
        return "enable_real_run"
    return "review_gate"


def format_real_run_reason(reason) -> str:
    if isinstance(reason, dict):
        message = reason.get("message") or reason.get("detail")
        blockers = reason.get("blockers")
        if message and blockers:
            return f"{message}; blockers={', '.join(map(str, blockers))}"
        if message:
            return str(message)
        return json.dumps(reason, ensure_ascii=False)
    return str(reason)


async def real_run_gate_status(
    lottery,
    *,
    selector_config: dict,
    real_run_enabled: bool,
    account_id: int | None = None,
    account_summary: dict | None = None,
    evidence_batch: RealRunEvidenceBatch | None = None,
) -> dict:
    platform = lottery["platform"]
    cfg = get_platform(platform) or {}
    target = validate_lottery_target(platform, lottery["raw_url"])
    real_run_target_valid = target.valid and not (
        platform_has_api_real_adapter(platform) and target.kind != "dynamic"
    )
    if account_summary is None:
        account_summary = await real_run_account_risk_summary(platform)
    selector_ready = platform_selectors_complete(selector_config, platform)
    adapter_kind = platform_real_adapter_kind(selector_config, platform)
    adapter_enabled = bool(cfg.get("action_adapter")) or platform_has_runtime_real_adapter(selector_config, platform)
    evidence = await validate_real_run_evidence(
        lottery,
        account_id=account_id,
        evidence_batch=evidence_batch,
    )
    blockers = list(evidence["blockers"])
    if not account_summary["ready_accounts"]:
        blockers.insert(0, "no_calibrated_ready_account")
    elif account_id is None and not account_summary["runnable_accounts"]:
        blockers.insert(0, "recent_account_risk_event")
    if not adapter_enabled:
        blockers.insert(0, "real_adapter_not_enabled")
    if not real_run_enabled:
        blockers.insert(0, "global_real_run_disabled")

    next_action = next_action_for_blockers(blockers) if blockers else "real_run"
    if next_action == "review_gate" and not selector_ready:
        next_action = "configure_adapter"

    return {
        "lottery_id": lottery["id"],
        "platform": platform,
        "status": lottery["status"],
        "raw_url": lottery["raw_url"],
        "target_valid": real_run_target_valid,
        "target_kind": target.kind,
        "target_error": None
        if real_run_target_valid
        else (target.reason or "bilibili_dynamic_target_required"),
        "allowed": not blockers,
        "blockers": blockers,
        "next_action": next_action,
        "real_run_enabled": real_run_enabled,
        "adapter_enabled": adapter_enabled,
        "adapter_kind": adapter_kind,
        "selector_ready": selector_ready,
        "api_adapter_ready": adapter_kind == "api",
        "oauth_adapter_ready": adapter_kind == "oauth" and bool(
            evidence.get("oauth_capability_ready")
        ),
        "safe_accounts": account_summary["ready_accounts"],
        "risk_clear_accounts": account_summary["runnable_accounts"],
        "account_risk": evidence["account_risk"] or account_summary["latest_recent_risk"],
        "probe_ready": evidence["probe_ready"],
        "shadow_ready": evidence["shadow_ready"],
        "execution_preflight_ready": bool(
            evidence.get("execution_preflight_ready", evidence["shadow_ready"])
        ),
        "oauth_dry_run_ready": bool(evidence.get("oauth_dry_run_ready")),
        "oauth_dry_run_task_id": evidence.get("oauth_dry_run_task_id"),
        "action_plan_ready": evidence["action_plan_ready"],
        "rule_snapshot_ready": bool(evidence.get("rule_snapshot_ready")),
        "execution_evidence_bound": bool(evidence.get("execution_evidence_bound")),
        "execution_evidence_id": evidence.get("execution_evidence_id"),
        "execution_evidence": evidence.get("execution_evidence"),
        "execution_path_id": evidence.get("execution_path_id"),
        "execution_mode": evidence.get("execution_mode"),
        "real_run_supported": evidence.get("real_run_supported", True),
        "capability_reason": evidence.get("capability_reason"),
        "oauth_capability_denied_actions": list(
            evidence.get("oauth_capability_denied_actions") or []
        ),
        "manual_shadow_supported": bool(
            evidence.get("manual_shadow_supported")
        ),
        "selector_observation_complete": bool(
            evidence.get("selector_observation_complete")
        ),
        "manual_confirmation_required": bool(
            evidence.get("manual_confirmation_required")
        ),
        "action_plan": parse_json_field(dict(lottery).get("action_plan")),
    }
