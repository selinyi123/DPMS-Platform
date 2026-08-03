import asyncio
import hashlib
import hmac
from itertools import islice
import json
import os
import stat
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

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
    XIAOHONGSHU_BROWSER_EXECUTION_PATH,
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
    compute_xiaohongshu_browser_config_hash,
    semantic_requirement_status,
    validate_action_plan_v2,
    validate_friend_mention_requirements,
    weibo_runtime_capability_requirements,
)
from app.adapter_config import (
    STRUCTURED_SELECTOR_PLATFORMS,
    click_selectors,
    load_runtime_selector_config,
    platform_has_api_real_adapter,
    platform_has_runtime_real_adapter,
    platform_probe_ready_for_real_actions,
    platform_real_adapter_kind,
    selector_config_complete,
    selector_phase_configured,
    selector_values,
)
from app.db import database, redis
from app.platform_modules import (
    PlatformCapabilityError,
    PlatformModuleUnavailableError,
    get_platform_module,
)
from app.platform_modules.catalog import XIAOHONGSHU_MANUAL_EXECUTION_BLOCKER
from app.platforms import get_platform
from app.services.lottery_rules import parse_lottery_rule
from app.utils.log import structured_log
from app.utils.secure_files import (
    SecureFileError,
    open_file_beneath_resolved_root,
)
from app.utils.lottery_targets import (
    validate_lottery_identity,
    validate_lottery_target,  # compatibility seam for existing test/runtime patches
)
from app.utils.crypto import CREDENTIAL_AAD, cookie_vault
from app.utils.credential_kind import decrypt_douyin_device_credential
from app.utils.weibo_oauth_credential import (
    WeiboOAuthCredentialError,
    parse_weibo_oauth_credential,
)
from shared.weibo_oauth_evidence import (
    WeiboOAuthCalibrationEnvelopeError,
    validate_weibo_oauth_calibration_envelope,
)
from shared.platform_ids import PLATFORM_IDS
from shared.xiaohongshu_browser_contract import (
    XIAOHONGSHU_BROWSER_PROBE_OBSERVATION_KIND,
    XIAOHONGSHU_BROWSER_SHADOW_OBSERVATION_KIND,
    XiaohongshuBrowserContractError,
    compute_xiaohongshu_comment_text_hash,
    validate_xiaohongshu_browser_observation_binding,
)
from shared.douyin_device_contract import (
    DOUYIN_DEVICE_EXECUTION_PATH,
    DOUYIN_DEVICE_PROBE_OBSERVATION_KIND,
    DOUYIN_DEVICE_SHADOW_OBSERVATION_KIND,
    DouyinDeviceContractError,
    compute_douyin_device_config_hash,
    compute_douyin_exact_text_hash,
    normalize_douyin_device_public_config,
    validate_douyin_device_observation_binding,
)

ACCOUNT_RISK_COOLDOWN_HOURS = 24
MAX_ACCOUNT_RISK_COOLDOWN_HOURS = 24
# A risk lookup must never materialize or lock an arbitrary number of rows.
# The extra row is an overflow sentinel: when it is present and none of the
# bounded candidates is active, callers stay fail-closed because an unseen
# older 24-hour event could still be authoritative.
ACCOUNT_RISK_CANDIDATE_LIMIT = 128
SHADOW_PHASE_ORDER = list(ACTION_ORDER)
EVIDENCE_ROOT = Path(os.getenv("EVIDENCE_ROOT", "/profiles"))
SHADOW_SCREENSHOT_ROOT = EVIDENCE_ROOT / "shadow-runs"
EVIDENCE_HASH_CHUNK_SIZE = 1024 * 1024
MAX_SHADOW_SCREENSHOT_BYTES = 32 * 1024 * 1024
MAX_EVIDENCE_REQUEST_HASH_BYTES = 128 * 1024 * 1024
MAX_BATCH_EVIDENCE_ROWS_PER_PAIR = 8
# Strategy queues are capped at 100 lotteries. Keep both dimensions of every
# account-scoped evidence query independently bounded so a large account pool
# cannot turn one read endpoint into an unbounded SQL statement/result set.
MAX_ACCOUNT_SCOPED_READINESS_LOTTERIES = 100
ACCOUNT_SCOPED_READINESS_ACCOUNT_BATCH_SIZE = 16
# A request may advance beyond the first 16 ranked accounts (the 17th account
# is an intentional compatibility contract), but it may not scan an arbitrary
# recommendation population. Four batches keep that behavior while placing a
# hard ceiling on CPU work and evidence-query dimensions.
ACCOUNT_SCOPED_READINESS_MAX_ACCOUNT_BATCHES_PER_PLATFORM = 4
ACCOUNT_SCOPED_READINESS_MAX_CANDIDATES_PER_PLATFORM = (
    ACCOUNT_SCOPED_READINESS_ACCOUNT_BATCH_SIZE
    * ACCOUNT_SCOPED_READINESS_MAX_ACCOUNT_BATCHES_PER_PLATFORM
)
EXACT_REAL_CANDIDATE_ACCOUNT_QUERY_TIMEOUT_SECONDS = 2.0
EXACT_REAL_CANDIDATE_ACCOUNT_QUERY_TIMEOUT_BLOCKER = (
    "exact_real_candidate_account_query_timeout"
)
EXACT_REAL_CANDIDATE_ACCOUNT_QUERY_FAILED_BLOCKER = (
    "exact_real_candidate_account_query_failed"
)
# Keep the target-history projection identical to the fields consumed by the
# strategy ladder and Autopilot's final candidate filter.  The query remains
# read-only and returns counts only; task payloads and evidence paths are never
# selected into the production-readiness response.
EXACT_REAL_CANDIDATE_TARGET_METRICS_SQL = """
                  (SELECT COUNT(*)
                     FROM task_runs tr
                    WHERE tr.lottery_id = l.id
                      AND tr.status IN ('queued','running')) AS active_runs,
                  (SELECT COUNT(*)
                     FROM task_runs tr
                    WHERE tr.lottery_id = l.id
                      AND COALESCE(
                            tr.task_mode,
                            IF(tr.dry_run = 1, 'dry_run', 'real_run')
                          ) = 'dry_run'
                      AND tr.status = 'succeeded') AS dry_success,
                  (SELECT COUNT(*)
                     FROM task_runs tr
                    WHERE tr.lottery_id = l.id
                      AND tr.task_mode = 'shadow_run'
                      AND tr.status = 'succeeded') AS shadow_success,
                  (SELECT COUNT(*)
                     FROM task_runs tr
                    WHERE tr.lottery_id = l.id
                      AND tr.status = 'failed') AS failed_runs"""
# Necessary-condition evidence is capped independently for each lottery.
# Reusing the platform-ranking name here previously concealed a global
# ORDER/LIMIT that let one lottery starve every later lottery in the lane.
ACCOUNT_SCOPED_READINESS_MAX_PREFILTER_CANDIDATES_PER_LOTTERY = (
    ACCOUNT_SCOPED_READINESS_MAX_CANDIDATES_PER_PLATFORM
)
# Read projections must remain bounded too: the summary is consumed by the
# real-run gate when no account has been selected yet. Any overflow is treated
# as an active risk instead of inferring safety from an incomplete population.
REAL_RUN_RISK_SUMMARY_ACCOUNT_LIMIT_PER_PLATFORM = (
    ACCOUNT_SCOPED_READINESS_MAX_CANDIDATES_PER_PLATFORM
)


def platform_shadow_screenshot_root(
    platform: object,
    file_path: object = None,
) -> Path:
    platform_id = str(platform or "").strip().casefold()
    if platform_id not in PLATFORM_IDS:
        return EVIDENCE_ROOT / ".invalid-platform" / "shadow-runs"
    candidate = Path(str(file_path or ""))
    legacy_root = EVIDENCE_ROOT / "shadow-runs"
    if candidate.is_absolute() and candidate.is_relative_to(legacy_root):
        # Historical evidence remains read-only in Core. New platform Workers
        # mount only their platform root and cannot add to this compatibility
        # path after the cutover.
        return legacy_root
    return EVIDENCE_ROOT / platform_id / "shadow-runs"


REAL_RUN_RISK_SUMMARY_EVENT_LIMIT_PER_PLATFORM = 4096
REAL_RUN_RISK_SUMMARY_PLATFORM_LIMIT = 8
REAL_RUN_RISK_SUMMARY_TIMEOUT_SECONDS = 2.0
ACCOUNT_SCOPED_READINESS_MAX_CANDIDATE_PLATFORM_KEYS = 8
# The readiness-owned 10-second work budget is explicitly split into three
# platform-fair phases. Each phase runs platform lanes concurrently, so one
# platform cannot consume a sibling's allocation. Ranking happens between the
# phases and never consumes the evidence-evaluation allocation.
ACCOUNT_SCOPED_READINESS_PHASE_BUDGET_SECONDS = 10.0
ACCOUNT_SCOPED_READINESS_PREFILTER_TIMEOUT_SECONDS = 2.0
ACCOUNT_SCOPED_READINESS_FRESHNESS_RECHECK_TIMEOUT_SECONDS = 1.0
ACCOUNT_SCOPED_READINESS_PLATFORM_EVALUATION_TIMEOUT_SECONDS = (
    ACCOUNT_SCOPED_READINESS_PHASE_BUDGET_SECONDS
    - ACCOUNT_SCOPED_READINESS_PREFILTER_TIMEOUT_SECONDS
    - ACCOUNT_SCOPED_READINESS_FRESHNESS_RECHECK_TIMEOUT_SECONDS
)
ACCOUNT_SCOPED_READINESS_EXACT_EVIDENCE_DB_TIMEOUT_SECONDS = 2.0
ACCOUNT_SCOPED_READINESS_RISK_DB_TIMEOUT_SECONDS = 2.0
ACCOUNT_SCOPED_READINESS_MAX_EXACT_EVIDENCE_PAGES = 4
# Final validation proves that expiring evidence remains valid briefly after
# the response, instead of merely being valid at the SELECT NOW() instant.
ACCOUNT_SCOPED_READINESS_RESPONSE_SAFETY_MARGIN_SECONDS = 5.0

ACCOUNT_SCOPED_READINESS_PREFILTER_TIMEOUT_BLOCKER = (
    "account_scoped_real_run_readiness_candidate_prefilter_timeout"
)
ACCOUNT_SCOPED_READINESS_PLATFORM_EVALUATION_TIMEOUT_BLOCKER = (
    "account_scoped_real_run_readiness_platform_evaluation_timeout"
)
ACCOUNT_SCOPED_READINESS_CANDIDATE_BUDGET_BLOCKER = (
    "account_scoped_real_run_readiness_candidate_budget_exhausted"
)
ACCOUNT_SCOPED_READINESS_EVIDENCE_PAGE_BUDGET_BLOCKER = (
    "account_scoped_real_run_readiness_evidence_page_budget_exhausted"
)
ACCOUNT_SCOPED_READINESS_EVIDENCE_QUERY_TIMEOUT_BLOCKER = (
    "account_scoped_real_run_readiness_evidence_query_timeout"
)
ACCOUNT_SCOPED_READINESS_RISK_QUERY_TIMEOUT_BLOCKER = (
    "account_scoped_real_run_readiness_risk_query_timeout"
)
ACCOUNT_SCOPED_READINESS_FRESHNESS_RECHECK_TIMEOUT_BLOCKER = (
    "account_scoped_real_run_readiness_freshness_recheck_timeout"
)
ACCOUNT_SCOPED_READINESS_FRESHNESS_RECHECK_FAILED_BLOCKER = (
    "account_scoped_real_run_readiness_freshness_recheck_failed"
)
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
    """Request-scoped, read-only evidence used by bulk readiness endpoints.

    Dispatch keeps using the non-batched path, so every authoritative decision
    still performs fresh, locked reads. The maps replace per-lottery/account
    reads without relaxing the validators' account and immutable-plan binding.
    """

    account_id: int | None
    account_ids: frozenset[int] = frozenset()
    account_scoped_readiness: bool = False
    probes: dict[tuple[int, str], object] = field(default_factory=dict)
    shadows: dict[int, object] = field(default_factory=dict)
    observations: dict[str, object] = field(default_factory=dict)
    evidence_files: dict[tuple[str, str, int], object] = field(default_factory=dict)
    accounts: dict[int, object] = field(default_factory=dict)
    account_risks: dict[int, dict] = field(default_factory=dict)
    rule_snapshots: dict[tuple[int, int], object] = field(default_factory=dict)
    bilibili_execution_evidence: dict[tuple[int, int], list[object]] = field(
        default_factory=dict
    )
    weibo_oauth_dry_runs: dict[tuple[int, int], list[object]] = field(
        default_factory=dict
    )
    # Cache only the non-secret outcome needed by readiness. This avoids
    # decrypting the same OAuth envelope once per lottery while keeping access
    # tokens out of the request-scoped cache.
    weibo_oauth_credential_states: dict[
        int, tuple[bool, str | None, str | None]
    ] = field(default_factory=dict)
    readiness_budget_blockers_by_platform: dict[str, str] = field(
        default_factory=dict
    )
    # SQL initially filters evidence with NOW(), but a strategy evaluation may
    # run for several seconds. The final read-model pass advances
    # ``freshness_cutoff_at`` beyond a new database timestamp by the response
    # safety margin, invalidating proof that is about to expire as well as proof
    # that already crossed its boundary.
    freshness_snapshot_at: datetime | None = None
    freshness_cutoff_at: datetime | None = None
    screenshot_integrity_cache: dict[tuple[str, str, str], tuple[tuple[int, int, int, int, int], bool]] = field(
        default_factory=dict
    )
    screenshot_hash_budget: dict[str, int] = field(
        default_factory=lambda: {"remaining": MAX_EVIDENCE_REQUEST_HASH_BYTES}
    )

    def supports_account(self, account_id: int | None) -> bool:
        """Reject accidental reuse outside the batch's explicit account scope."""

        if account_id is None:
            return self.account_id is None
        if self.account_ids:
            return account_id in self.account_ids
        return self.account_id == account_id


@dataclass(frozen=True)
class AccountScopedReadinessCandidatePrefilter:
    """Necessary persisted-evidence candidates, isolated per lottery."""

    account_ids_by_lottery: dict[int, frozenset[int]]
    failed_platforms: frozenset[str] = frozenset()
    budget_blockers_by_platform: dict[str, str] = field(
        default_factory=dict
    )
    budget_blockers_by_lottery: dict[int, str] = field(
        default_factory=dict
    )

    def account_ids_for(self, lottery_id: int) -> frozenset[int]:
        return self.account_ids_by_lottery.get(int(lottery_id), frozenset())


class AccountScopedReadinessAccountCandidates(dict):
    """Bounded, payload-free account candidates plus fail-closed diagnostics."""

    def __init__(
        self,
        values: dict[str, list[dict]] | None = None,
        *,
        blockers_by_platform: dict[str, str] | None = None,
        truncated_platforms: frozenset[str] = frozenset(),
    ):
        super().__init__(values or {})
        self.blockers_by_platform = dict(blockers_by_platform or {})
        self.truncated_platforms = frozenset(truncated_platforms)


@dataclass(frozen=True)
class AccountScopedReadinessPhaseBudget:
    """One monotonic wall-clock budget for one platform/phase lane."""

    deadline: float

    @classmethod
    def start(
        cls,
        timeout_seconds: float,
    ) -> "AccountScopedReadinessPhaseBudget":
        return cls(
            deadline=(
                time.monotonic()
                + max(
                    float(timeout_seconds),
                    0.001,
                )
            )
        )

    def remaining_seconds(self) -> float:
        return max(self.deadline - time.monotonic(), 0.0)

    async def run(self, operation):
        remaining = self.remaining_seconds()
        if remaining <= 0:
            raise AccountScopedReadinessPhaseBudgetExhausted
        try:
            return await asyncio.wait_for(operation(), timeout=remaining)
        except asyncio.TimeoutError as exc:
            # Preserve an operation's own timeout unless the shared deadline
            # was actually consumed. Exact-evidence DB timeouts have their own
            # platform-local blocker and must not masquerade as phase timeout.
            if self.remaining_seconds() > 0:
                raise
            raise AccountScopedReadinessPhaseBudgetExhausted from exc


class AccountScopedReadinessPhaseBudgetExhausted(RuntimeError):
    """One platform's readiness phase deadline was consumed."""


class AccountScopedReadinessRiskQueryTimeout(RuntimeError):
    """The bounded account-risk evidence query exceeded its DB deadline."""


def _sql_in_values(prefix: str, values) -> tuple[str, dict]:
    parameters = {}
    placeholders = []
    for index, value in enumerate(values):
        key = f"{prefix}_{index}"
        placeholders.append(f":{key}")
        parameters[key] = value
    return ", ".join(placeholders), parameters


async def load_account_scoped_readiness_account_candidates(
    platforms,
    *,
    exclude_active_leases: bool = True,
) -> AccountScopedReadinessAccountCandidates:
    """Load the bounded account population used by exact readiness checks.

    This deliberately selects no credential, profile, or evidence material.
    Eligibility matches the strategy queue's account source, while the shared
    account-scoped evaluator below remains the authority for exact
    ``lottery_id + account_id`` execution evidence.
    """

    requested_platforms = tuple(
        dict.fromkeys(
            str(platform or "").strip().casefold()
            for platform in platforms
            if str(platform or "").strip().casefold() in PLATFORM_IDS
        )
    )
    lease_filter = (
        """AND NOT EXISTS (
                SELECT 1
                  FROM account_operation_leases lease
                 WHERE lease.account_id = a.id
                   AND lease.released_at IS NULL
                   AND lease.expires_at > NOW()
              )"""
        if exclude_active_leases
        else ""
    )

    async def load_platform(platform: str):
        try:
            rows = await asyncio.wait_for(
                database.fetch_all(
                    f"""SELECT a.id AS account_id, a.platform
                          FROM accounts a
                          JOIN account_calibrations latest_calibration
                            ON latest_calibration.id = (
                              SELECT candidate.id
                                FROM account_calibrations candidate
                               WHERE candidate.account_id = a.id
                                 AND candidate.platform = a.platform
                               ORDER BY candidate.id DESC
                               LIMIT 1
                            )
                         WHERE a.platform = :candidate_platform
                           AND a.status = 'ready'
                           AND a.deleted_at IS NULL
                           AND OCTET_LENGTH(a.encrypted_credential) > 0
                           AND latest_calibration.status = 'succeeded'
                           {lease_filter}
                         ORDER BY a.daily_task_count ASC, a.id ASC
                         LIMIT :candidate_limit""",
                    {
                        "candidate_platform": platform,
                        "candidate_limit": (
                            ACCOUNT_SCOPED_READINESS_MAX_CANDIDATES_PER_PLATFORM
                            + 1
                        ),
                    },
                ),
                timeout=max(
                    float(
                        EXACT_REAL_CANDIDATE_ACCOUNT_QUERY_TIMEOUT_SECONDS
                    ),
                    0.001,
                ),
            )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            structured_log(
                "warning",
                EXACT_REAL_CANDIDATE_ACCOUNT_QUERY_TIMEOUT_BLOCKER,
                platform=platform,
            )
            return platform, [], EXACT_REAL_CANDIDATE_ACCOUNT_QUERY_TIMEOUT_BLOCKER
        except Exception as exc:
            structured_log(
                "warning",
                EXACT_REAL_CANDIDATE_ACCOUNT_QUERY_FAILED_BLOCKER,
                platform=platform,
                cause_type=type(exc).__name__,
            )
            return platform, [], EXACT_REAL_CANDIDATE_ACCOUNT_QUERY_FAILED_BLOCKER
        sanitized = []
        for raw_row in list(rows)[
            : ACCOUNT_SCOPED_READINESS_MAX_CANDIDATES_PER_PLATFORM + 1
        ]:
            row = dict(raw_row)
            try:
                account_id = int(row.get("account_id"))
            except (TypeError, ValueError):
                continue
            if account_id <= 0:
                continue
            if str(row.get("platform") or "").strip().casefold() != platform:
                continue
            sanitized.append({"account_id": account_id, "platform": platform})
        return platform, sanitized, None

    loaded = await asyncio.gather(
        *(load_platform(platform) for platform in requested_platforms)
    )
    grouped = {
        platform: rows
        for platform, rows, blocker in loaded
        if rows and blocker is None
    }
    blockers = {
        platform: blocker
        for platform, _rows, blocker in loaded
        if blocker is not None
    }
    truncated = frozenset(
        platform
        for platform, rows, blocker in loaded
        if blocker is None
        and len(rows) > ACCOUNT_SCOPED_READINESS_MAX_CANDIDATES_PER_PLATFORM
    )
    return AccountScopedReadinessAccountCandidates(
        grouped,
        blockers_by_platform=blockers,
        truncated_platforms=truncated,
    )


async def load_account_scoped_readiness_candidate_prefilter(
    lotteries,
) -> AccountScopedReadinessCandidatePrefilter:
    """Load only accounts that possess a necessary real-run evidence row.

    This is deliberately a necessary-condition filter, not authorization:
    normal readiness validators still re-check the account, immutable plan,
    hashes, observations, capability attestation, risk, and exact evidence.
    A platform query failure is retained explicitly so it cannot be confused
    with an empty candidate set or fall back to scanning every ready account.
    """

    lottery_rows = [
        dict(lottery)
        for lottery in islice(
            iter(lotteries),
            MAX_ACCOUNT_SCOPED_READINESS_LOTTERIES + 1,
        )
    ]
    if len(lottery_rows) > MAX_ACCOUNT_SCOPED_READINESS_LOTTERIES:
        raise ValueError("account-scoped readiness lottery batch exceeds limit")
    lottery_ids_by_platform: dict[str, list[int]] = {}
    account_ids_by_lottery: dict[int, set[int]] = {}
    lottery_platforms: dict[int, str] = {}
    for lottery in lottery_rows:
        lottery_id = int(lottery["id"])
        platform = str(lottery.get("platform") or "").strip().casefold()
        previous_platform = lottery_platforms.setdefault(lottery_id, platform)
        if previous_platform != platform:
            raise ValueError("readiness lottery platform identity conflict")
        account_ids_by_lottery.setdefault(lottery_id, set())
        lottery_ids_by_platform.setdefault(platform, []).append(lottery_id)

    query_platforms = [
        platform
        for platform in ("bilibili", "xiaohongshu", "douyin", "weibo")
        if lottery_ids_by_platform.get(platform)
    ]
    if len(query_platforms) > 1:
        # Each authoritative platform owns a separate prefilter deadline.
        # Running them together bounds wall-clock by the slowest lane rather
        # than the sum and prevents ordered head-of-line budget consumption.
        partials = await asyncio.gather(
            *(
                load_account_scoped_readiness_candidate_prefilter(
                    [
                        lottery
                        for lottery in lottery_rows
                        if (
                            str(lottery.get("platform") or "")
                            .strip()
                            .casefold()
                            == platform
                        )
                    ]
                )
                for platform in query_platforms
            )
        )
        failed_platforms: set[str] = set()
        budget_blockers_by_platform: dict[str, str] = {}
        budget_blockers_by_lottery: dict[int, str] = {}
        for partial in partials:
            failed_platforms.update(partial.failed_platforms)
            budget_blockers_by_platform.update(
                partial.budget_blockers_by_platform
            )
            budget_blockers_by_lottery.update(
                partial.budget_blockers_by_lottery
            )
            for lottery_id, account_ids in (
                partial.account_ids_by_lottery.items()
            ):
                account_ids_by_lottery[lottery_id].update(account_ids)
        return AccountScopedReadinessCandidatePrefilter(
            account_ids_by_lottery={
                lottery_id: frozenset(account_ids)
                for lottery_id, account_ids
                in account_ids_by_lottery.items()
            },
            failed_platforms=frozenset(failed_platforms),
            budget_blockers_by_platform=budget_blockers_by_platform,
            budget_blockers_by_lottery=budget_blockers_by_lottery,
        )

    phase_budget = AccountScopedReadinessPhaseBudget.start(
        ACCOUNT_SCOPED_READINESS_PREFILTER_TIMEOUT_SECONDS
    )
    failed_platforms: set[str] = set()
    budget_blockers_by_platform: dict[str, str] = {}
    budget_blockers_by_lottery: dict[int, str] = {}
    for platform in ("bilibili", "xiaohongshu", "douyin", "weibo"):
        lottery_ids = list(
            dict.fromkeys(lottery_ids_by_platform.get(platform, ()))
        )
        if not lottery_ids:
            continue
        per_lottery_limit = (
            ACCOUNT_SCOPED_READINESS_MAX_PREFILTER_CANDIDATES_PER_LOTTERY
        )
        values = {
            "candidate_prefilter_per_lottery_limit": (
                per_lottery_limit + 1
            )
        }
        branches = []
        for index, lottery_id in enumerate(lottery_ids):
            lottery_key = f"candidate_{platform}_lottery_{index}"
            values[lottery_key] = lottery_id
            if platform in {"bilibili", "xiaohongshu", "douyin"}:
                candidate_select = f"""
                    SELECT DISTINCT e.lottery_id, e.account_id
                    FROM execution_evidence_bindings e
                    WHERE e.lottery_id = :{lottery_key}
                      AND e.platform = '{platform}'
                      AND e.status = 'verified'
                      AND e.verified_at IS NOT NULL
                      AND e.verified_at <= NOW()
                      AND e.expires_at > NOW()
                      AND e.probe_id IS NOT NULL
                      AND e.shadow_task_id IS NOT NULL
                    ORDER BY e.account_id
                    LIMIT :candidate_prefilter_per_lottery_limit"""
            else:
                candidate_select = f"""
                    SELECT DISTINCT tr.lottery_id, tr.account_id
                    FROM task_runs tr
                    JOIN account_operation_leases lease
                      ON lease.lease_id = tr.account_lease_id
                     AND lease.account_id = tr.account_id
                     AND lease.generation =
                           tr.account_lease_generation
                     AND lease.owner_id = tr.task_id
                     AND lease.task_id = tr.task_id
                     AND lease.operation_kind = 'dry_run'
                    WHERE tr.lottery_id = :{lottery_key}
                      AND tr.task_mode = 'dry_run'
                      AND tr.status = 'succeeded'
                      AND tr.finished_at IS NOT NULL
                      AND tr.finished_at >= DATE_SUB(
                            NOW(), INTERVAL 24 HOUR
                          )
                      AND tr.finished_at <= NOW()
                      AND lease.released_at IS NOT NULL
                      AND lease.released_at >= tr.finished_at
                      AND lease.released_at <= NOW()
                    ORDER BY tr.account_id
                    LIMIT :candidate_prefilter_per_lottery_limit"""
            # Each derived branch owns its LIMIT. A single global LIMIT lets
            # a low lottery_id consume the whole result and starve peers.
            branches.append(
                "SELECT lottery_id, account_id FROM ("
                + candidate_select
                + f") AS candidate_branch_{index}"
            )
        query = (
            "SELECT candidate_rows.lottery_id, "
            "candidate_rows.account_id FROM ("
            + " UNION ALL ".join(branches)
            + ") AS candidate_rows "
            "ORDER BY candidate_rows.lottery_id, "
            "candidate_rows.account_id"
        )
        try:
            rows = await phase_budget.run(
                lambda: database.fetch_all(query, values)
            )

            requested_ids = set(lottery_ids)
            loaded: dict[int, set[int]] = {
                lottery_id: set() for lottery_id in lottery_ids
            }
            for row in rows:
                lottery_id = int(row_value(row, "lottery_id"))
                account_id = int(row_value(row, "account_id"))
                if lottery_id not in requested_ids or account_id <= 0:
                    raise RuntimeError(
                        "candidate prefilter returned an out-of-scope row"
                    )
                if len(loaded[lottery_id]) >= per_lottery_limit:
                    budget_blockers_by_lottery[lottery_id] = (
                        ACCOUNT_SCOPED_READINESS_CANDIDATE_BUDGET_BLOCKER
                    )
                    continue
                loaded[lottery_id].add(account_id)
        except asyncio.CancelledError:
            raise
        except AccountScopedReadinessPhaseBudgetExhausted:
            budget_blockers_by_platform[platform] = (
                ACCOUNT_SCOPED_READINESS_PREFILTER_TIMEOUT_BLOCKER
            )
            structured_log(
                "warning",
                "account_readiness_candidate_prefilter_timeout",
                platform=platform,
            )
            continue
        except Exception as exc:
            failed_platforms.add(platform)
            structured_log(
                "error",
                "account_readiness_candidate_prefilter_failed",
                platform=platform,
                exception=exc,
            )
            continue

        for lottery_id, account_ids in loaded.items():
            account_ids_by_lottery[lottery_id].update(account_ids)

    return AccountScopedReadinessCandidatePrefilter(
        account_ids_by_lottery={
            lottery_id: frozenset(account_ids)
            for lottery_id, account_ids in account_ids_by_lottery.items()
        },
        failed_platforms=frozenset(failed_platforms),
        budget_blockers_by_platform=budget_blockers_by_platform,
        budget_blockers_by_lottery=budget_blockers_by_lottery,
    )


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
    """Validate a canonical non-empty XHS manual-action subset."""

    if not isinstance(payload, dict):
        return False
    required_actions = payload.get("required_phases")
    if (
        not isinstance(required_actions, list)
        or not required_actions
        or required_actions
        != [
            action
            for action in XIAOHONGSHU_ACTION_ORDER
            if action in required_actions
        ]
    ):
        return False
    return qualified_manual_shadow_observation(
        payload,
        required_actions=required_actions,
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
    """Compatibility wrapper around the shared secure-volume opener."""

    try:
        return open_file_beneath_resolved_root(
            resolved_root,
            resolved_candidate,
        )
    except SecureFileError as exc:
        if str(exc) == "secure_file_open_unsupported":
            raise OSError("secure_evidence_open_unsupported") from exc
        raise


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
        if value.tzinfo is not None:
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        return value
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    if " " in text and "T" not in text:
        text = text.replace(" ", "T", 1)
    try:
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is not None:
            return parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed
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
    now_dt = normalize_datetime(now) or datetime.utcnow()
    return cooldown_until > now_dt


async def current_db_time(*, required: bool = False, db=None) -> datetime:
    target = db or database
    row = await target.fetch_one("SELECT UTC_TIMESTAMP() AS db_now")
    db_now = normalize_datetime(row_value(row, "db_now"))
    if db_now is not None:
        return db_now
    if required:
        raise RuntimeError("database clock response is missing or invalid")
    return datetime.utcnow()


def account_risk_payload(row) -> dict:
    if not row:
        return {"has_recent_risk": False, "cooldown_hours": ACCOUNT_RISK_COOLDOWN_HOURS}
    detail = parse_json_field(row_value(row, "detail"))
    event_type = row_value(row, "event_type")
    cooldown_hours = account_risk_cooldown_hours(detail, event_type)
    cooldown_until = account_risk_cooldown_until(row)
    controlling_event = {
        "id": row_value(row, "id"),
        "account_id": row_value(row, "account_id"),
        "event_type": event_type,
        "detail": detail if isinstance(detail, dict) else {},
        "created_at": normalize_timestamp(row_value(row, "created_at")),
    }
    return {
        "has_recent_risk": True,
        "cooldown_hours": cooldown_hours,
        # The materialized row is selected by the longest active cooldown,
        # not necessarily by event creation time. Keep latest_event as a
        # compatibility alias while exposing the semantically exact name.
        "controlling_event": controlling_event,
        "latest_event": controlling_event,
        "cooldown_until": normalize_timestamp(cooldown_until),
    }


def account_risk_budget_exhausted_payload(
    account_id: int | None,
    *,
    reason: str = "risk_history_budget_exhausted",
    candidate_limit: int = ACCOUNT_RISK_CANDIDATE_LIMIT,
) -> dict:
    """Represent an incomplete bounded lookup as an active safety blocker."""

    safe_account_id = int(account_id or 0)
    controlling_event = {
        "id": None,
        "account_id": safe_account_id,
        "event_type": reason,
        "detail": {
            "reason": reason,
            "candidate_limit": int(candidate_limit),
        },
        "created_at": None,
    }
    return {
        "has_recent_risk": True,
        "cooldown_hours": MAX_ACCOUNT_RISK_COOLDOWN_HOURS,
        "controlling_event": controlling_event,
        "latest_event": controlling_event,
        "cooldown_until": None,
        "query_budget_exhausted": True,
    }


def account_risk_summary_payload(
    *,
    ready_accounts: int,
    runnable_accounts: int,
    controlling_active_risk: dict,
    query_budget_exhausted: bool = False,
) -> dict:
    """Return an exact name plus the legacy summary field for API stability."""

    payload = {
        "ready_accounts": int(ready_accounts),
        "runnable_accounts": int(runnable_accounts),
        "controlling_active_risk": controlling_active_risk,
        # Compatibility alias retained for existing API clients.
        "latest_recent_risk": controlling_active_risk,
    }
    if query_budget_exhausted:
        payload["query_budget_exhausted"] = True
    return payload


async def recent_account_risk(
    account_id: int,
    *,
    now=None,
    for_update: bool = False,
) -> dict:
    now_dt = normalize_datetime(now) or await current_db_time(required=True)
    lock_clause = "FOR UPDATE" if for_update else ""
    row = await database.fetch_one(
        f"""SELECT re.id, re.account_id, re.event_type, re.detail,
                  re.created_at
             FROM account_active_risk_states active_risk
             JOIN risk_events re
               ON re.id = active_risk.risk_event_id
              AND re.account_id = active_risk.account_id
            WHERE active_risk.account_id = :account_id
              AND active_risk.active_until > :risk_now
            LIMIT 1
            {lock_clause}""",
        {
            "account_id": account_id,
            "risk_now": now_dt,
        },
    )
    if row is not None and account_risk_is_active(row, now_dt):
        return account_risk_payload(row)
    return account_risk_payload(None)


async def real_run_account_risk_summaries(platforms) -> dict[str, dict]:
    """Load bounded, fail-closed ready-account/risk summaries per platform."""

    requested_platforms = list(
        dict.fromkeys(str(platform) for platform in platforms)
    )
    if not requested_platforms:
        return {}

    def exhausted_summary(
        *,
        reason: str,
        ready_accounts: int = 0,
        candidate_limit: int,
    ) -> dict:
        return account_risk_summary_payload(
            ready_accounts=ready_accounts,
            runnable_accounts=0,
            controlling_active_risk=account_risk_budget_exhausted_payload(
                None,
                reason=reason,
                candidate_limit=candidate_limit,
            ),
            query_budget_exhausted=True,
        )

    try:
        db_now = await asyncio.wait_for(
            current_db_time(required=True),
            timeout=max(float(REAL_RUN_RISK_SUMMARY_TIMEOUT_SECONDS), 0.001),
        )
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError as exc:
        structured_log(
            "warning",
            "real_run_risk_summary_clock_query_timeout",
            timeout_seconds=REAL_RUN_RISK_SUMMARY_TIMEOUT_SECONDS,
            error=str(exc),
        )
        return {
            platform: exhausted_summary(
                reason="risk_summary_query_timeout",
                candidate_limit=REAL_RUN_RISK_SUMMARY_ACCOUNT_LIMIT_PER_PLATFORM,
            )
            for platform in requested_platforms
        }
    except Exception as exc:
        structured_log(
            "warning",
            "real_run_risk_summary_clock_query_failed",
            error=str(exc),
        )
        return {
            platform: exhausted_summary(
                reason="risk_summary_query_failed",
                candidate_limit=REAL_RUN_RISK_SUMMARY_ACCOUNT_LIMIT_PER_PLATFORM,
            )
            for platform in requested_platforms
        }

    async def load_platform(platform: str) -> tuple[str, dict]:
        async def load_bounded_summary() -> dict:
            ready_rows = list(
                await database.fetch_all(
                    """SELECT a.id, a.platform
                       FROM accounts a
                       JOIN account_calibrations latest_calibration
                         ON latest_calibration.id = (
                           SELECT candidate.id
                           FROM account_calibrations candidate
                           WHERE candidate.account_id = a.id
                             AND candidate.platform = a.platform
                           ORDER BY candidate.id DESC
                           LIMIT 1
                         )
                       WHERE a.platform = :risk_summary_platform
                         AND a.status = 'ready'
                         AND a.deleted_at IS NULL
                         AND OCTET_LENGTH(a.encrypted_credential) > 0
                         AND latest_calibration.status = 'succeeded'
                       ORDER BY a.daily_task_count ASC, a.id ASC
                       LIMIT :risk_summary_account_limit""",
                    {
                        "risk_summary_platform": platform,
                        "risk_summary_account_limit": (
                            REAL_RUN_RISK_SUMMARY_ACCOUNT_LIMIT_PER_PLATFORM
                            + 1
                        ),
                    },
                )
            )[: REAL_RUN_RISK_SUMMARY_ACCOUNT_LIMIT_PER_PLATFORM + 1]
            if any(
                str(row_value(row, "platform") or "").casefold()
                != platform.casefold()
                for row in ready_rows
            ):
                raise RuntimeError(
                    "account query returned a platform outside the requested "
                    "risk-summary scope"
                )
            if (
                len(ready_rows)
                > REAL_RUN_RISK_SUMMARY_ACCOUNT_LIMIT_PER_PLATFORM
            ):
                structured_log(
                    "warning",
                    "real_run_risk_summary_account_budget_exhausted",
                    platform=platform,
                    observed_account_count=len(ready_rows),
                    account_limit=(
                        REAL_RUN_RISK_SUMMARY_ACCOUNT_LIMIT_PER_PLATFORM
                    ),
                )
                return exhausted_summary(
                    reason="risk_summary_account_budget_exhausted",
                    ready_accounts=len(ready_rows),
                    candidate_limit=(
                        REAL_RUN_RISK_SUMMARY_ACCOUNT_LIMIT_PER_PLATFORM
                    ),
                )

            account_ids = list(
                dict.fromkeys(
                    int(row_value(account, "id")) for account in ready_rows
                )
            )
            if not account_ids:
                return account_risk_summary_payload(
                    ready_accounts=0,
                    runnable_accounts=0,
                    controlling_active_risk=account_risk_payload(None),
                )

            account_clause, account_values = _sql_in_values(
                "risk_summary_account",
                account_ids,
            )
            risk_rows = list(
                await database.fetch_all(
                    f"""SELECT re.id, re.account_id, re.event_type,
                               re.detail, re.created_at
                          FROM account_active_risk_states active_risk
                          JOIN risk_events re
                            ON re.id = active_risk.risk_event_id
                           AND re.account_id = active_risk.account_id
                         WHERE active_risk.account_id IN ({account_clause})
                           AND active_risk.active_until > :risk_summary_now
                         ORDER BY re.account_id, re.created_at DESC,
                                  re.id DESC""",
                    {
                        **account_values,
                        "risk_summary_now": db_now,
                    },
                )
            )[: len(account_ids) + 1]
            if len(risk_rows) > len(account_ids):
                raise RuntimeError(
                    "active risk state query exceeded one row per account"
                )

            account_id_set = set(account_ids)
            risks_by_account: dict[int, list] = {
                account_id: [] for account_id in account_ids
            }
            for row in risk_rows:
                risk_account_id = int(row_value(row, "account_id"))
                if risk_account_id not in account_id_set:
                    raise RuntimeError(
                        "risk query returned an account outside the requested "
                        "risk-summary scope"
                    )
                risks_by_account[risk_account_id].append(row)

            runnable_count = 0
            latest_risk = None
            latest_risk_created_at = None
            latest_risk_id = -1
            for account_id in account_ids:
                active_rows = [
                    row
                    for row in risks_by_account[account_id]
                    if account_risk_is_active(row, db_now)
                ]
                if not active_rows:
                    runnable_count += 1
                    continue
                active_row = max(
                    active_rows,
                    key=lambda row: (
                        normalize_datetime(row_value(row, "created_at"))
                        or datetime.min,
                        int(row_value(row, "id") or 0),
                    ),
                )
                risk = account_risk_payload(active_row)
                created_at = normalize_datetime(
                    risk["controlling_event"].get("created_at")
                )
                risk_id = int(risk["controlling_event"].get("id") or 0)
                if latest_risk is None or (
                    created_at or datetime.min,
                    risk_id,
                ) > (
                    latest_risk_created_at or datetime.min,
                    latest_risk_id,
                ):
                    latest_risk = risk
                    latest_risk_created_at = created_at
                    latest_risk_id = risk_id
            return account_risk_summary_payload(
                ready_accounts=len(account_ids),
                runnable_accounts=runnable_count,
                controlling_active_risk=(
                    latest_risk or account_risk_payload(None)
                ),
            )

        try:
            summary = await asyncio.wait_for(
                load_bounded_summary(),
                timeout=max(
                    float(REAL_RUN_RISK_SUMMARY_TIMEOUT_SECONDS),
                    0.001,
                ),
            )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError as exc:
            structured_log(
                "warning",
                "real_run_risk_summary_query_timeout",
                platform=platform,
                timeout_seconds=REAL_RUN_RISK_SUMMARY_TIMEOUT_SECONDS,
                error=str(exc),
            )
            summary = exhausted_summary(
                reason="risk_summary_query_timeout",
                candidate_limit=(
                    REAL_RUN_RISK_SUMMARY_ACCOUNT_LIMIT_PER_PLATFORM
                ),
            )
        except Exception as exc:
            structured_log(
                "warning",
                "real_run_risk_summary_query_failed",
                platform=platform,
                error=str(exc),
            )
            summary = exhausted_summary(
                reason="risk_summary_query_failed",
                candidate_limit=(
                    REAL_RUN_RISK_SUMMARY_ACCOUNT_LIMIT_PER_PLATFORM
                ),
            )
        return platform, summary

    bounded_platforms = requested_platforms[
        :REAL_RUN_RISK_SUMMARY_PLATFORM_LIMIT
    ]
    loaded = dict(
        await asyncio.gather(
            *(load_platform(platform) for platform in bounded_platforms)
        )
    )
    skipped_platforms = requested_platforms[
        REAL_RUN_RISK_SUMMARY_PLATFORM_LIMIT:
    ]
    if skipped_platforms:
        structured_log(
            "warning",
            "real_run_risk_summary_platform_budget_exhausted",
            requested_platform_count=len(requested_platforms),
            platform_limit=REAL_RUN_RISK_SUMMARY_PLATFORM_LIMIT,
            skipped_platform_count=len(skipped_platforms),
        )
    for platform in skipped_platforms:
        loaded[platform] = exhausted_summary(
            reason="risk_summary_platform_budget_exhausted",
            candidate_limit=REAL_RUN_RISK_SUMMARY_PLATFORM_LIMIT,
        )
    return loaded


async def real_run_account_risk_summary(platform: str) -> dict:
    return (await real_run_account_risk_summaries([platform]))[platform]


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


def _account_scoped_exact_evidence_bindings(
    lottery_rows: list[dict],
    accounts: dict[int, object],
    *,
    platform: str,
) -> list[dict]:
    """Build the exact persisted bindings that a readiness validator will use.

    This is only a bounded query prefilter. The normal validators still
    recompute and compare every binding and, for Bilibili, validate both
    observation payloads. The loader keyset-pages matching rows within the
    explicit page/DB budget; exhaustion is propagated as a dedicated blocker
    instead of being confused with missing evidence.
    """

    platform_key = str(platform or "").strip().casefold()
    if platform_key == "bilibili":
        execution_path_id = BILIBILI_API_EXECUTION_PATH
        require_executable = False
        try:
            from app.services.bilibili_preflight_evidence import (
                BilibiliPreflightEvidenceError,
                extract_bilibili_dynamic_id,
            )
        except ImportError:
            return []
    elif platform_key == "weibo":
        execution_path_id = WEIBO_OAUTH_EXECUTION_PATH
        require_executable = True
    else:
        return []

    account_revisions: dict[int, int] = {}
    for account_id, account in accounts.items():
        if (
            str(row_value(account, "platform") or "").strip().casefold()
            != platform_key
        ):
            continue
        try:
            execution_revision = int(
                row_value(account, "execution_revision") or 0
            )
        except (TypeError, ValueError):
            continue
        if execution_revision > 0:
            account_revisions[int(account_id)] = execution_revision

    bindings_by_pair: dict[tuple[int, int], dict] = {}
    for lottery in lottery_rows:
        if (
            str(lottery.get("platform") or "").strip().casefold()
            != platform_key
        ):
            continue
        try:
            lottery_id = int(lottery["id"])
            plan = validate_action_plan_v2(
                parse_json_field(lottery.get("action_plan")),
                require_executable=require_executable,
            )
            if (
                plan.plan.get("platform") != platform_key
                or plan.execution_path_id != execution_path_id
                or plan.plan.get("executable") is not True
            ):
                continue
            target_hash = compute_target_hash(
                str(lottery.get("canonical_url") or "")
            )
        except (ActionPlanV2Error, KeyError, TypeError, ValueError):
            continue
        dynamic_id = ""
        if platform_key == "bilibili":
            try:
                dynamic_id = extract_bilibili_dynamic_id(
                    str(lottery.get("canonical_url") or ""),
                    str(lottery.get("raw_url") or ""),
                )
            except BilibiliPreflightEvidenceError:
                continue

        for account_id, execution_revision in account_revisions.items():
            try:
                if platform_key == "bilibili":
                    config_hash = compute_bilibili_api_config_hash(
                        execution_revision
                    )
                else:
                    config_hash = compute_config_hash(
                        {
                            "execution_path_id": WEIBO_OAUTH_EXECUTION_PATH,
                            "execution_revision": execution_revision,
                            "runtime_capability_requirements": (
                                plan.runtime_capability_requirements
                            ),
                            "weibo_rip_hash": "",
                        }
                    )
            except (ActionPlanV2Error, TypeError, ValueError):
                continue
            bindings_by_pair.setdefault(
                (lottery_id, account_id),
                {
                    "lottery_id": lottery_id,
                    "account_id": account_id,
                    "rule_snapshot_id": plan.rule_snapshot_id,
                    "execution_path_id": execution_path_id,
                    "target_hash": target_hash,
                    "rule_hash": plan.rule_hash,
                    "action_plan_hash": plan.plan_hash,
                    "config_hash": config_hash,
                    "_dynamic_id": dynamic_id,
                    "_required_actions": plan.required_actions,
                    "_execution_revision": execution_revision,
                    "_follow_target_handle": plan.follow_target_handle,
                },
            )

    max_pairs = (
        MAX_ACCOUNT_SCOPED_READINESS_LOTTERIES
        * ACCOUNT_SCOPED_READINESS_ACCOUNT_BATCH_SIZE
    )
    if len(bindings_by_pair) > max_pairs:
        raise ValueError("account-scoped readiness evidence pair limit exceeded")
    return list(bindings_by_pair.values())


def _exact_evidence_bindings_json(bindings: list[dict]) -> str:
    """Encode bounded internal bindings for a MySQL ``JSON_TABLE`` join."""

    persisted_fields = (
        "lottery_id",
        "account_id",
        "rule_snapshot_id",
        "execution_path_id",
        "target_hash",
        "rule_hash",
        "action_plan_hash",
        "config_hash",
    )
    return json.dumps(
        [
            {field: binding[field] for field in persisted_fields}
            for binding in bindings
        ],
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _row_matches_exact_evidence_binding(row, binding: dict) -> bool:
    """Defensively recheck a row returned by the exact-binding SQL join."""

    try:
        if (
            int(row_value(row, "lottery_id")) != int(binding["lottery_id"])
            or int(row_value(row, "account_id")) != int(binding["account_id"])
            or int(row_value(row, "rule_snapshot_id") or 0)
            != int(binding["rule_snapshot_id"])
        ):
            return False
    except (KeyError, TypeError, ValueError):
        return False
    return all(
        str(row_value(row, field) or "") == str(binding[field])
        for field in (
            "execution_path_id",
            "target_hash",
            "rule_hash",
            "action_plan_hash",
            "config_hash",
        )
    )


async def _fetch_account_scoped_exact_evidence_page(
    *,
    platform: str,
    query: str,
    values: dict,
):
    """Run one exact-evidence page under an independent DB deadline."""

    try:
        return await asyncio.wait_for(
            database.fetch_all(query, values),
            timeout=max(
                float(
                    ACCOUNT_SCOPED_READINESS_EXACT_EVIDENCE_DB_TIMEOUT_SECONDS
                ),
                0.001,
            ),
        )
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError:
        structured_log(
            "warning",
            "account_readiness_exact_evidence_query_timeout",
            platform=platform,
        )
        return None


async def _fetch_account_scoped_active_risks(
    *,
    account_clause: str,
    account_values: dict,
    now: datetime,
    db=None,
    enforce_query_timeout: bool = True,
):
    """Return the transactionally maintained active-risk row per account."""

    values = dict(account_values)
    values["readiness_risk_now"] = now
    target = db or database
    query = f"""SELECT re.id, re.account_id, re.event_type,
                       re.detail, re.created_at
                  FROM account_active_risk_states active_risk
                  JOIN risk_events re
                    ON re.id = active_risk.risk_event_id
                   AND re.account_id = active_risk.account_id
                 WHERE active_risk.account_id IN ({account_clause})
                   AND active_risk.active_until > :readiness_risk_now
                 ORDER BY re.account_id, re.created_at DESC,
                          re.id DESC"""
    try:
        if not enforce_query_timeout:
            # The final mutable-state reload runs on an explicit acquired
            # connection inside one transaction. ``asyncio.wait_for`` creates
            # a child Task; databases binds implicit connections by current
            # Task, which would otherwise escape that snapshot.
            return await target.fetch_all(query, values)
        return await asyncio.wait_for(
            target.fetch_all(query, values),
            timeout=max(
                float(ACCOUNT_SCOPED_READINESS_RISK_DB_TIMEOUT_SECONDS),
                0.001,
            ),
        )
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError as exc:
        structured_log(
            "warning",
            "account_readiness_risk_query_timeout",
            account_count=len(account_values),
        )
        raise AccountScopedReadinessRiskQueryTimeout from exc


async def _load_final_account_mutable_state_snapshot(
    account_ids,
) -> tuple[datetime, dict[int, object], dict[int, dict]]:
    """Reload selected accounts, leases, calibration and risk in one snapshot."""

    scoped_account_ids = list(
        dict.fromkeys(
            int(account_id)
            for account_id in account_ids
            if int(account_id) > 0
        )
    )
    if (
        not scoped_account_ids
        or len(scoped_account_ids)
        > MAX_ACCOUNT_SCOPED_READINESS_LOTTERIES
    ):
        raise ValueError("final mutable-state account scope is invalid")
    account_clause, account_values = _sql_in_values(
        "final_readiness_account",
        scoped_account_ids,
    )
    async with database.connection() as connection:
        async with connection.transaction():
            db_now = await current_db_time(required=True, db=connection)
            account_rows = await connection.fetch_all(
                f"""SELECT a.id, a.platform, a.status, a.execution_revision,
                       CASE
                         WHEN a.platform IN ('weibo', 'douyin')
                         THEN a.encrypted_credential
                         ELSE NULL
                       END AS encrypted_credential,
                       OCTET_LENGTH(a.encrypted_credential)
                         AS credential_size,
                       c.calibration_id,
                       c.status AS calibration_status,
                       c.result AS calibration_result,
                       c.created_at AS calibration_created_at,
                       c.finished_at AS calibration_finished_at,
                       (
                         c.created_at >= DATE_SUB(
                           :final_state_now, INTERVAL 24 HOUR
                         )
                         AND c.created_at <= :final_state_now
                         AND c.finished_at IS NOT NULL
                         AND c.finished_at <= :final_state_now
                       ) AS calibration_fresh
                FROM accounts a
                LEFT JOIN account_calibrations c
                  ON c.id = (
                    SELECT latest.id
                    FROM account_calibrations latest
                    WHERE latest.account_id = a.id
                      AND latest.platform = a.platform
                    ORDER BY latest.id DESC
                    LIMIT 1
                  )
                WHERE a.id IN ({account_clause})
                  AND a.deleted_at IS NULL
                  AND NOT EXISTS (
                    SELECT 1
                    FROM account_operation_leases lease
                    WHERE lease.account_id = a.id
                      AND lease.released_at IS NULL
                      AND lease.expires_at > :final_state_now
                  )""",
                {
                    **account_values,
                    "final_state_now": db_now,
                },
            )
            risk_rows = await _fetch_account_scoped_active_risks(
                account_clause=account_clause,
                account_values=account_values,
                now=db_now,
                db=connection,
                enforce_query_timeout=False,
            )

    requested_ids = frozenset(scoped_account_ids)
    accounts_by_id: dict[int, object] = {}
    for row in account_rows:
        account_id = int(row_value(row, "id") or 0)
        if account_id not in requested_ids:
            raise RuntimeError(
                "final account query returned an out-of-scope row"
            )
        if account_id in accounts_by_id:
            raise RuntimeError(
                "final account query returned duplicate account rows"
            )
        accounts_by_id[account_id] = row

    if len(risk_rows) > len(scoped_account_ids):
        raise RuntimeError(
            "final risk query exceeded one row per readiness account"
        )
    risks_by_id: dict[int, dict] = {
        account_id: account_risk_payload(None)
        for account_id in scoped_account_ids
    }
    seen_risk_accounts: set[int] = set()
    for row in risk_rows:
        account_id = int(row_value(row, "account_id") or 0)
        if account_id not in requested_ids:
            raise RuntimeError(
                "final risk query returned an out-of-scope row"
            )
        if account_id in seen_risk_accounts:
            raise RuntimeError(
                "final risk query returned duplicate account rows"
            )
        seen_risk_accounts.add(account_id)
        if account_risk_is_active(row, db_now):
            risks_by_id[account_id] = account_risk_payload(row)
    return db_now, accounts_by_id, risks_by_id


async def load_account_scoped_real_run_readiness_batch(
    lotteries,
    *,
    account_ids,
) -> RealRunEvidenceBatch:
    """Preload authoritative readiness rows for many lottery/account pairs.

    The caller advances through ranked account batches. This loader enforces
    the strategy endpoint's lottery bound and one small account batch so every
    generated ``IN`` list and evidence result set has a fixed upper bound.
    Every security-sensitive binding that differs per target remains
    recomputed and compared by the normal validators.
    """

    lottery_rows = [dict(lottery) for lottery in lotteries]
    scoped_account_ids: list[int] = []
    seen_account_ids: set[int] = set()
    for raw_account_id in account_ids:
        try:
            account_id = int(raw_account_id)
        except (TypeError, ValueError):
            continue
        if account_id > 0 and account_id not in seen_account_ids:
            seen_account_ids.add(account_id)
            scoped_account_ids.append(account_id)
    if (
        len(lottery_rows)
        > MAX_ACCOUNT_SCOPED_READINESS_LOTTERIES
    ):
        raise ValueError("account-scoped readiness lottery batch exceeds limit")
    if (
        len(scoped_account_ids)
        > ACCOUNT_SCOPED_READINESS_ACCOUNT_BATCH_SIZE
    ):
        raise ValueError("account-scoped readiness account batch exceeds limit")
    batch = RealRunEvidenceBatch(
        account_id=None,
        account_ids=frozenset(scoped_account_ids),
        account_scoped_readiness=True,
    )
    if not lottery_rows:
        return batch

    lottery_ids = list(
        dict.fromkeys(int(lottery["id"]) for lottery in lottery_rows)
    )
    lottery_clause, lottery_values = _sql_in_values(
        "readiness_lottery", lottery_ids
    )

    snapshot_ids = []
    for lottery in lottery_rows:
        try:
            snapshot_id = int(
                lottery.get("authoritative_rule_snapshot_id") or 0
            )
        except (TypeError, ValueError):
            continue
        if snapshot_id > 0:
            snapshot_ids.append(snapshot_id)
    snapshot_ids = list(dict.fromkeys(snapshot_ids))
    if snapshot_ids:
        snapshot_clause, snapshot_values = _sql_in_values(
            "readiness_snapshot", snapshot_ids
        )
        snapshot_query_values = dict(lottery_values)
        snapshot_query_values.update(snapshot_values)
        snapshot_rows = await database.fetch_all(
            f"""SELECT id, lottery_id, platform, rule_hash, rule_text,
                       is_complete, attested_by, attested_at
                FROM lottery_rule_snapshots
                WHERE lottery_id IN ({lottery_clause})
                  AND id IN ({snapshot_clause})""",
            snapshot_query_values,
        )
        for row in snapshot_rows:
            try:
                key = (
                    int(row_value(row, "lottery_id")),
                    int(row_value(row, "id")),
                )
            except (TypeError, ValueError):
                continue
            if key[0] in lottery_ids and key[1] in snapshot_ids:
                batch.rule_snapshots.setdefault(key, row)

    if not scoped_account_ids:
        return batch

    account_clause, account_values = _sql_in_values(
        "readiness_account", scoped_account_ids
    )
    account_rows = await database.fetch_all(
        f"""SELECT a.id, a.platform, a.status, a.execution_revision,
                   CASE
                     WHEN a.platform IN ('weibo', 'douyin')
                     THEN a.encrypted_credential
                     ELSE NULL
                   END AS encrypted_credential,
                   OCTET_LENGTH(a.encrypted_credential) AS credential_size,
                   c.calibration_id, c.status AS calibration_status,
                   c.result AS calibration_result,
                   c.created_at AS calibration_created_at,
                   c.finished_at AS calibration_finished_at,
                   (
                     c.created_at >= DATE_SUB(NOW(), INTERVAL 24 HOUR)
                     AND c.created_at <= NOW()
                     AND c.finished_at IS NOT NULL
                     AND c.finished_at <= NOW()
                   )
                     AS calibration_fresh
            FROM accounts a
            LEFT JOIN account_calibrations c
              ON c.id = (
                SELECT latest.id
                FROM account_calibrations latest
                WHERE latest.account_id = a.id
                  AND latest.platform = a.platform
                ORDER BY latest.id DESC
                LIMIT 1
              )
            WHERE a.id IN ({account_clause})
              AND a.deleted_at IS NULL
              AND NOT EXISTS (
                SELECT 1
                FROM account_operation_leases lease
                WHERE lease.account_id = a.id
                  AND lease.released_at IS NULL
                  AND lease.expires_at > NOW()
              )""",
        account_values,
    )
    for row in account_rows:
        try:
            loaded_account_id = int(row_value(row, "id"))
        except (TypeError, ValueError):
            continue
        if loaded_account_id not in batch.account_ids:
            raise RuntimeError(
                "account query returned an account outside the readiness scope"
            )
        batch.accounts.setdefault(loaded_account_id, row)

    db_now = await current_db_time(required=True)
    batch.freshness_snapshot_at = db_now
    batch.freshness_cutoff_at = db_now
    risk_rows = await _fetch_account_scoped_active_risks(
        account_clause=account_clause,
        account_values=account_values,
        now=db_now,
    )
    if len(risk_rows) > len(scoped_account_ids):
        raise RuntimeError(
            "bounded risk query exceeded one row per readiness account"
        )
    risks_by_account: dict[int, object] = {}
    for row in risk_rows:
        try:
            risk_account_id = int(row_value(row, "account_id"))
        except (TypeError, ValueError):
            continue
        if risk_account_id not in risks_by_account:
            if risk_account_id not in batch.account_ids:
                raise RuntimeError(
                    "risk query returned an account outside the readiness scope"
                )
            risks_by_account[risk_account_id] = row
        else:
            raise RuntimeError(
                "bounded risk query returned multiple rows for one account"
            )
    for account_id in scoped_account_ids:
        account_risk = account_risk_payload(None)
        row = risks_by_account.get(account_id)
        # The SQL predicate is authoritative for bounding; this second check
        # handles the narrow clock-boundary where the row expires between the
        # DB query and application materialisation.
        if row is not None and account_risk_is_active(row, db_now):
            account_risk = account_risk_payload(row)
        batch.account_risks[account_id] = account_risk

    bilibili_lottery_ids = list(
        dict.fromkeys(
            int(lottery["id"])
            for lottery in lottery_rows
            if str(lottery.get("platform") or "").casefold() == "bilibili"
        )
    )
    if bilibili_lottery_ids:
        bilibili_bindings = _account_scoped_exact_evidence_bindings(
            lottery_rows,
            batch.accounts,
            platform="bilibili",
        )
        bilibili_clause, bilibili_values = _sql_in_values(
            "readiness_bilibili_lottery", bilibili_lottery_ids
        )
        bilibili_values.update(account_values)
        bilibili_values["readiness_bilibili_exact_bindings"] = (
            _exact_evidence_bindings_json(bilibili_bindings)
        )
        bilibili_values["readiness_evidence_page_limit"] = (
            max(1, len(bilibili_bindings))
            * MAX_BATCH_EVIDENCE_ROWS_PER_PAIR
        )
        bilibili_bindings_by_pair = {
            (
                int(binding["lottery_id"]),
                int(binding["account_id"]),
            ): binding
            for binding in bilibili_bindings
        }
        evidence_cursor_verified_at = None
        evidence_cursor_id = None
        evidence_page_count = 0
        while True:
            if evidence_page_count >= max(
                int(ACCOUNT_SCOPED_READINESS_MAX_EXACT_EVIDENCE_PAGES),
                1,
            ):
                batch.readiness_budget_blockers_by_platform[
                    "bilibili"
                ] = ACCOUNT_SCOPED_READINESS_EVIDENCE_PAGE_BUDGET_BLOCKER
                break
            evidence_page_count += 1
            page_values = dict(bilibili_values)
            page_values["readiness_evidence_cursor_verified_at"] = (
                evidence_cursor_verified_at
            )
            page_values["readiness_evidence_cursor_id"] = evidence_cursor_id
            evidence_rows = (
                await _fetch_account_scoped_exact_evidence_page(
                    platform="bilibili",
                    query=f"""WITH exact_bindings AS (
                  SELECT binding_rows.*
                  FROM JSON_TABLE(
                    CAST(:readiness_bilibili_exact_bindings AS JSON),
                    '$[*]' COLUMNS (
                      lottery_id BIGINT PATH '$.lottery_id',
                      account_id BIGINT PATH '$.account_id',
                      rule_snapshot_id BIGINT PATH '$.rule_snapshot_id',
                      execution_path_id VARCHAR(128)
                        PATH '$.execution_path_id',
                      target_hash CHAR(64) PATH '$.target_hash',
                      rule_hash CHAR(64) PATH '$.rule_hash',
                      action_plan_hash CHAR(64) PATH '$.action_plan_hash',
                      config_hash CHAR(64) PATH '$.config_hash'
                    )
                  ) AS binding_rows
                )
                SELECT e.id, e.lottery_id, e.account_id,
                        e.rule_snapshot_id, e.execution_path_id,
                        e.target_hash, e.rule_hash, e.action_plan_hash,
                        e.config_hash, e.probe_id, e.shadow_task_id,
                       e.verified_at, e.expires_at,
                       e.probe_observation_kind
                         AS evidence_probe_observation_kind,
                       e.probe_observation_hash
                         AS evidence_probe_observation_hash,
                       e.shadow_observation_kind
                         AS evidence_shadow_observation_kind,
                       e.shadow_observation_hash
                         AS evidence_shadow_observation_hash,
                       ac.result AS probe_observation,
                       ac.observation_kind AS probe_observation_kind,
                       ac.observation_hash AS probe_observation_hash,
                       ac.finished_at AS probe_finished_at,
                       shadow.preflight_observation AS shadow_observation,
                       shadow.preflight_observation_kind
                         AS shadow_observation_kind,
                        shadow.preflight_observation_hash
                          AS shadow_observation_hash,
                        shadow.finished_at AS shadow_finished_at,
                        probe_lease.released_at AS probe_lease_released_at,
                        shadow_lease.released_at AS shadow_lease_released_at
                   FROM execution_evidence_bindings e
                   JOIN exact_bindings expected
                     ON expected.lottery_id = e.lottery_id
                    AND expected.account_id = e.account_id
                    AND expected.rule_snapshot_id = e.rule_snapshot_id
                    AND CAST(expected.execution_path_id AS BINARY) =
                          CAST(e.execution_path_id AS BINARY)
                    AND CAST(expected.target_hash AS BINARY) =
                          CAST(e.target_hash AS BINARY)
                    AND CAST(expected.rule_hash AS BINARY) =
                          CAST(e.rule_hash AS BINARY)
                    AND CAST(expected.action_plan_hash AS BINARY) =
                          CAST(e.action_plan_hash AS BINARY)
                    AND CAST(expected.config_hash AS BINARY) =
                          CAST(e.config_hash AS BINARY)
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
                    AND shadow.preflight_observation_kind =
                          e.shadow_observation_kind
                    AND shadow.preflight_observation_hash =
                          e.shadow_observation_hash
                   JOIN account_operation_leases probe_lease
                     ON probe_lease.lease_id = ac.account_lease_id
                    AND probe_lease.account_id = ac.account_id
                    AND probe_lease.generation =
                          ac.account_lease_generation
                    AND probe_lease.owner_id = ac.probe_id
                    AND probe_lease.operation_kind = 'adapter_probe'
                    AND probe_lease.task_id IS NULL
                   JOIN account_operation_leases shadow_lease
                     ON shadow_lease.lease_id = shadow.account_lease_id
                    AND shadow_lease.account_id = shadow.account_id
                    AND shadow_lease.generation =
                          shadow.account_lease_generation
                    AND shadow_lease.owner_id = shadow.task_id
                    AND shadow_lease.operation_kind = 'shadow_run'
                    AND shadow_lease.task_id = shadow.task_id
                  WHERE e.lottery_id IN ({bilibili_clause})
                    AND e.account_id IN ({account_clause})
                    AND e.platform = 'bilibili'
                    AND e.status = 'verified'
                    AND e.verified_at IS NOT NULL
                    AND e.verified_at <= NOW()
                    AND e.expires_at > NOW()
                    AND e.probe_id IS NOT NULL
                    AND e.shadow_task_id IS NOT NULL
                    AND ac.status = 'succeeded'
                    AND ac.finished_at IS NOT NULL
                    AND ac.finished_at >= DATE_SUB(
                          NOW(), INTERVAL 24 HOUR
                        )
                    AND ac.finished_at <= NOW()
                    AND shadow.task_mode = 'shadow_run'
                    AND shadow.status = 'succeeded'
                    AND shadow.finished_at IS NOT NULL
                    AND shadow.finished_at >= DATE_SUB(
                          NOW(), INTERVAL 24 HOUR
                        )
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
                    AND e.verified_at >= GREATEST(
                          ac.finished_at, shadow.finished_at
                        )
                    AND e.expires_at <= LEAST(
                          DATE_ADD(ac.finished_at, INTERVAL 24 HOUR),
                          DATE_ADD(shadow.finished_at, INTERVAL 24 HOUR)
                        )
                    AND (
                      :readiness_evidence_cursor_verified_at IS NULL
                      OR e.verified_at <
                           :readiness_evidence_cursor_verified_at
                      OR (
                        e.verified_at =
                          :readiness_evidence_cursor_verified_at
                        AND CAST(e.id AS BINARY) <
                            CAST(
                              :readiness_evidence_cursor_id AS BINARY
                            )
                      )
                  )
                  ORDER BY e.verified_at DESC, e.id DESC
                  LIMIT :readiness_evidence_page_limit""",
                    values=page_values,
                )
            )
            if evidence_rows is None:
                batch.readiness_budget_blockers_by_platform[
                    "bilibili"
                ] = ACCOUNT_SCOPED_READINESS_EVIDENCE_QUERY_TIMEOUT_BLOCKER
                break
            if len(evidence_rows) > bilibili_values[
                "readiness_evidence_page_limit"
            ]:
                raise RuntimeError(
                    "bilibili evidence page exceeded configured limit"
                )
            for row in evidence_rows:
                try:
                    key = (
                        int(row_value(row, "lottery_id")),
                        int(row_value(row, "account_id")),
                    )
                except (TypeError, ValueError):
                    continue
                binding = bilibili_bindings_by_pair.get(key)
                if (
                    binding is None
                    or key in batch.bilibili_execution_evidence
                    or not _row_matches_exact_evidence_binding(row, binding)
                ):
                    continue
                if _exact_bilibili_evidence_observations_valid(
                    row,
                    dynamic_id=str(binding["_dynamic_id"]),
                    required_actions=tuple(binding["_required_actions"]),
                    execution_revision=int(binding["_execution_revision"]),
                    config_hash=str(binding["config_hash"]),
                    follow_target_handle=str(
                        binding["_follow_target_handle"]
                    ),
                ):
                    batch.bilibili_execution_evidence[key] = [row]
            if bilibili_bindings_by_pair and all(
                key in batch.bilibili_execution_evidence
                for key in bilibili_bindings_by_pair
            ):
                break
            if not evidence_rows:
                break
            next_verified_at = row_value(
                evidence_rows[-1], "verified_at"
            )
            next_id = row_value(evidence_rows[-1], "id")
            next_cursor = (next_verified_at, str(next_id or ""))
            if (
                next_verified_at is None
                or not next_cursor[1]
                or next_cursor
                == (
                    evidence_cursor_verified_at,
                    str(evidence_cursor_id or ""),
                )
            ):
                raise RuntimeError(
                    "bilibili evidence pagination cursor did not advance"
                )
            evidence_cursor_verified_at, evidence_cursor_id = next_cursor
            if len(evidence_rows) < bilibili_values[
                "readiness_evidence_page_limit"
            ]:
                break

    weibo_lottery_ids = list(
        dict.fromkeys(
            int(lottery["id"])
            for lottery in lottery_rows
            if str(lottery.get("platform") or "").casefold() == "weibo"
        )
    )
    if weibo_lottery_ids:
        weibo_bindings = _account_scoped_exact_evidence_bindings(
            lottery_rows,
            batch.accounts,
            platform="weibo",
        )
        weibo_clause, weibo_values = _sql_in_values(
            "readiness_weibo_lottery", weibo_lottery_ids
        )
        weibo_values.update(account_values)
        weibo_values["readiness_weibo_exact_bindings"] = (
            _exact_evidence_bindings_json(weibo_bindings)
        )
        weibo_values["readiness_dry_run_page_limit"] = (
            max(1, len(weibo_bindings))
            * MAX_BATCH_EVIDENCE_ROWS_PER_PAIR
        )
        weibo_bindings_by_pair = {
            (
                int(binding["lottery_id"]),
                int(binding["account_id"]),
            ): binding
            for binding in weibo_bindings
        }
        dry_run_cursor_finished_at = None
        dry_run_cursor_id = None
        dry_run_page_count = 0
        while True:
            if dry_run_page_count >= max(
                int(ACCOUNT_SCOPED_READINESS_MAX_EXACT_EVIDENCE_PAGES),
                1,
            ):
                batch.readiness_budget_blockers_by_platform[
                    "weibo"
                ] = ACCOUNT_SCOPED_READINESS_EVIDENCE_PAGE_BUDGET_BLOCKER
                break
            dry_run_page_count += 1
            page_values = dict(weibo_values)
            page_values["readiness_dry_run_cursor_finished_at"] = (
                dry_run_cursor_finished_at
            )
            page_values["readiness_dry_run_cursor_id"] = dry_run_cursor_id
            dry_run_rows = (
                await _fetch_account_scoped_exact_evidence_page(
                    platform="weibo",
                    query=f"""WITH exact_bindings AS (
                  SELECT binding_rows.*
                  FROM JSON_TABLE(
                    CAST(:readiness_weibo_exact_bindings AS JSON),
                    '$[*]' COLUMNS (
                      lottery_id BIGINT PATH '$.lottery_id',
                      account_id BIGINT PATH '$.account_id',
                      rule_snapshot_id BIGINT PATH '$.rule_snapshot_id',
                      execution_path_id VARCHAR(128)
                        PATH '$.execution_path_id',
                      target_hash CHAR(64) PATH '$.target_hash',
                      rule_hash CHAR(64) PATH '$.rule_hash',
                      action_plan_hash CHAR(64) PATH '$.action_plan_hash',
                      config_hash CHAR(64) PATH '$.config_hash'
                    )
                  ) AS binding_rows
                )
                SELECT tr.id, tr.task_id, tr.lottery_id, tr.account_id,
                        tr.rule_snapshot_id, tr.execution_path_id,
                        tr.target_hash, tr.rule_hash, tr.action_plan_hash,
                        tr.config_hash, tr.finished_at
                   FROM task_runs tr
                   JOIN exact_bindings expected
                     ON expected.lottery_id = tr.lottery_id
                    AND expected.account_id = tr.account_id
                    AND expected.rule_snapshot_id = tr.rule_snapshot_id
                    AND CAST(expected.execution_path_id AS BINARY) =
                          CAST(tr.execution_path_id AS BINARY)
                    AND CAST(expected.target_hash AS BINARY) =
                          CAST(tr.target_hash AS BINARY)
                    AND CAST(expected.rule_hash AS BINARY) =
                          CAST(tr.rule_hash AS BINARY)
                    AND CAST(expected.action_plan_hash AS BINARY) =
                          CAST(tr.action_plan_hash AS BINARY)
                    AND CAST(expected.config_hash AS BINARY) =
                          CAST(tr.config_hash AS BINARY)
                   JOIN account_operation_leases lease
                     ON lease.lease_id = tr.account_lease_id
                    AND lease.account_id = tr.account_id
                    AND lease.generation =
                          tr.account_lease_generation
                    AND lease.owner_id = tr.task_id
                    AND lease.task_id = tr.task_id
                    AND lease.operation_kind = 'dry_run'
                  WHERE tr.lottery_id IN ({weibo_clause})
                    AND tr.account_id IN ({account_clause})
                    AND tr.task_mode = 'dry_run'
                    AND tr.status = 'succeeded'
                    AND tr.finished_at IS NOT NULL
                    AND tr.finished_at >= DATE_SUB(
                          NOW(), INTERVAL 24 HOUR
                        )
                    AND tr.finished_at <= NOW()
                    AND lease.released_at IS NOT NULL
                    AND lease.released_at >= tr.finished_at
                    AND lease.released_at <= NOW()
                    AND (
                      :readiness_dry_run_cursor_finished_at IS NULL
                      OR tr.finished_at <
                           :readiness_dry_run_cursor_finished_at
                      OR (
                        tr.finished_at =
                          :readiness_dry_run_cursor_finished_at
                        AND tr.id < :readiness_dry_run_cursor_id
                      )
                  )
                  ORDER BY tr.finished_at DESC, tr.id DESC
                  LIMIT :readiness_dry_run_page_limit""",
                    values=page_values,
                )
            )
            if dry_run_rows is None:
                batch.readiness_budget_blockers_by_platform[
                    "weibo"
                ] = ACCOUNT_SCOPED_READINESS_EVIDENCE_QUERY_TIMEOUT_BLOCKER
                break
            if len(dry_run_rows) > weibo_values[
                "readiness_dry_run_page_limit"
            ]:
                raise RuntimeError(
                    "weibo dry-run page exceeded configured limit"
                )
            for row in dry_run_rows:
                try:
                    key = (
                        int(row_value(row, "lottery_id")),
                        int(row_value(row, "account_id")),
                    )
                except (TypeError, ValueError):
                    continue
                binding = weibo_bindings_by_pair.get(key)
                if (
                    binding is None
                    or key in batch.weibo_oauth_dry_runs
                    or not _row_matches_exact_evidence_binding(row, binding)
                ):
                    continue
                batch.weibo_oauth_dry_runs[key] = [row]
            if weibo_bindings_by_pair and all(
                key in batch.weibo_oauth_dry_runs
                for key in weibo_bindings_by_pair
            ):
                break
            if not dry_run_rows:
                break
            next_finished_at = row_value(
                dry_run_rows[-1], "finished_at"
            )
            next_id = row_value(dry_run_rows[-1], "id")
            try:
                next_cursor = (next_finished_at, int(next_id))
            except (TypeError, ValueError):
                raise RuntimeError(
                    "weibo dry-run pagination cursor is invalid"
                ) from None
            if (
                next_finished_at is None
                or next_cursor
                == (
                    dry_run_cursor_finished_at,
                    dry_run_cursor_id,
                )
            ):
                raise RuntimeError(
                    "weibo dry-run pagination cursor did not advance"
                )
            dry_run_cursor_finished_at, dry_run_cursor_id = next_cursor
            if len(dry_run_rows) < weibo_values[
                "readiness_dry_run_page_limit"
            ]:
                break
    return batch


def _append_blocker(blockers: list[str], code: str) -> None:
    if code not in blockers:
        blockers.append(code)


def _timestamp_in_freshness_window(
    value,
    *,
    cutoff: datetime,
    max_age: timedelta = timedelta(hours=24),
) -> bool:
    timestamp = normalize_datetime(value)
    cutoff_value = normalize_datetime(cutoff)
    if timestamp is None or cutoff_value is None:
        return False
    return cutoff_value - max_age <= timestamp <= cutoff_value


def _batched_bilibili_evidence_fresh_at(
    row,
    *,
    cutoff: datetime | None,
) -> bool:
    """Re-check every expiring Bilibili timestamp at response finalisation."""

    if cutoff is None:
        return True
    cutoff_value = normalize_datetime(cutoff)
    verified_at = normalize_datetime(row_value(row, "verified_at"))
    expires_at = normalize_datetime(row_value(row, "expires_at"))
    if (
        cutoff_value is None
        or verified_at is None
        or expires_at is None
        or verified_at > cutoff_value
        or expires_at <= cutoff_value
    ):
        return False
    return all(
        _timestamp_in_freshness_window(
            row_value(row, field),
            cutoff=cutoff_value,
        )
        for field in ("probe_finished_at", "shadow_finished_at")
    )


def _batched_weibo_calibration_fresh_at(
    account,
    *,
    cutoff: datetime | None,
) -> bool:
    if cutoff is None:
        return True
    if not _timestamp_in_freshness_window(
        row_value(account, "calibration_created_at"),
        cutoff=cutoff,
    ):
        return False
    finished_at = normalize_datetime(
        row_value(account, "calibration_finished_at")
    )
    cutoff_value = normalize_datetime(cutoff)
    if (
        finished_at is None
        or cutoff_value is None
        or finished_at > cutoff_value
    ):
        return False
    parsed = parse_json_field(row_value(account, "calibration_result"))
    if not isinstance(parsed, dict):
        return False
    capabilities = parsed.get("oauth_capabilities")
    if not isinstance(capabilities, dict):
        return False
    verified_at = _parse_utc_timestamp(capabilities.get("verified_at"))
    attested_at = _parse_utc_timestamp(capabilities.get("attested_at"))
    # Capability timestamps are UTC-aware; convert the database cutoff to the
    # same frame before applying the exact Core/Worker 24-hour contract.
    cutoff_utc = cutoff_value.replace(tzinfo=timezone.utc)
    return bool(
        verified_at is not None
        and attested_at is not None
        and cutoff_utc - WEIBO_OAUTH_CAPABILITY_MAX_AGE
        <= verified_at
        <= cutoff_utc
        and cutoff_utc - WEIBO_OAUTH_CAPABILITY_MAX_AGE
        <= attested_at
        <= verified_at
    )


def _batched_weibo_dry_run_fresh_at(
    row,
    *,
    cutoff: datetime | None,
) -> bool:
    if cutoff is None:
        return True
    return _timestamp_in_freshness_window(
        row_value(row, "finished_at"),
        cutoff=cutoff,
    )


def _batched_rule_snapshot_matches(
    batch: RealRunEvidenceBatch,
    *,
    lottery_id: int,
    snapshot_id: int,
    platform: str,
    rule_hash: str,
    rule_text: str,
) -> bool:
    snapshot = batch.rule_snapshots.get((lottery_id, snapshot_id))
    return bool(
        snapshot
        and str(row_value(snapshot, "platform") or "").casefold()
        == platform.casefold()
        and str(row_value(snapshot, "rule_hash") or "") == rule_hash
        and str(row_value(snapshot, "rule_text") or "") == rule_text
        and int(row_value(snapshot, "is_complete") or 0) == 1
        and row_value(snapshot, "attested_by") is not None
        and row_value(snapshot, "attested_at") is not None
    )


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
        from app.services.bilibili_preflight_evidence import (
            BilibiliPreflightEvidenceError,
            validate_preflight_observation_binding,
        )
    except ImportError:
        # This validator is reached only through the Bilibili module. Keep an
        # unavailable provider local to that platform and fail its evidence
        # closed without poisoning readiness for peer platforms.
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
    except (
        BilibiliPreflightEvidenceError,
        ActionPlanV2Error,
        TypeError,
        ValueError,
    ):
        return False
    return True


def _select_batched_bilibili_execution_evidence(
    batch: RealRunEvidenceBatch,
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
):
    """Select an exact row from already time/lease-filtered evidence."""

    for row in batch.bilibili_execution_evidence.get(
        (lottery_id, account_id), []
    ):
        try:
            row_snapshot_id = int(row_value(row, "rule_snapshot_id") or 0)
        except (TypeError, ValueError):
            continue
        if (
            row_snapshot_id != rule_snapshot_id
            or str(row_value(row, "execution_path_id") or "")
            != execution_path_id
            or str(row_value(row, "target_hash") or "") != target_hash
            or str(row_value(row, "rule_hash") or "") != rule_hash
            or str(row_value(row, "action_plan_hash") or "")
            != action_plan_hash
            or str(row_value(row, "config_hash") or "") != config_hash
        ):
            continue
        if not _batched_bilibili_evidence_fresh_at(
            row,
            cutoff=batch.freshness_cutoff_at,
        ):
            continue
        if _exact_bilibili_evidence_observations_valid(
            row,
            dynamic_id=dynamic_id,
            required_actions=required_actions,
            execution_revision=execution_revision,
            config_hash=config_hash,
            follow_target_handle=follow_target_handle,
        ):
            return row
    return None


async def _load_exact_probe_shadow_execution_evidence(
    *,
    platform: str,
    lottery_id: int,
    account_id: int,
    rule_snapshot_id: int,
    execution_path_id: str,
    target_hash: str,
    rule_hash: str,
    action_plan_hash: str,
    config_hash: str,
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
        "platform": platform,
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
                 AND e.platform = :platform
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
    return row


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
    row = await _load_exact_probe_shadow_execution_evidence(
        platform="bilibili",
        lottery_id=lottery_id,
        account_id=account_id,
        rule_snapshot_id=rule_snapshot_id,
        execution_path_id=execution_path_id,
        target_hash=target_hash,
        rule_hash=rule_hash,
        action_plan_hash=action_plan_hash,
        config_hash=config_hash,
        evidence_id=evidence_id,
        for_update=for_update,
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


def _exact_xiaohongshu_browser_observations_valid(
    row,
    *,
    lottery_id: int,
    account_id: int,
    rule_snapshot_id: int,
    target_hash: str,
    rule_hash: str,
    action_plan_hash: str,
    config_hash: str,
    required_actions: tuple[str, ...],
    execution_revision: int,
    follow_target_handle: str,
    comment_text_hash: str,
) -> bool:
    """Independently hash and validate the exact XHS Probe + Shadow pair."""

    if not row:
        return False
    probe_id = str(row_value(row, "probe_id") or "")
    shadow_task_id = str(row_value(row, "shadow_task_id") or "")
    common = {
        "expected_lottery_id": lottery_id,
        "expected_account_id": account_id,
        "expected_execution_revision": execution_revision,
        "expected_target_hash": target_hash,
        "expected_rule_snapshot_id": rule_snapshot_id,
        "expected_rule_hash": rule_hash,
        "expected_action_plan_hash": action_plan_hash,
        "expected_config_hash": config_hash,
        "expected_actions": required_actions,
        "expected_follow_target_handle": follow_target_handle,
        "expected_comment_text_hash": comment_text_hash,
    }
    try:
        validate_xiaohongshu_browser_observation_binding(
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
            expected_observation_kind=(
                XIAOHONGSHU_BROWSER_PROBE_OBSERVATION_KIND
            ),
            expected_evidence_id=probe_id,
            **common,
        )
        validate_xiaohongshu_browser_observation_binding(
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
            expected_observation_kind=(
                XIAOHONGSHU_BROWSER_SHADOW_OBSERVATION_KIND
            ),
            expected_evidence_id=shadow_task_id,
            **common,
        )
    except (XiaohongshuBrowserContractError, TypeError, ValueError):
        return False
    return True


async def load_exact_xiaohongshu_browser_execution_evidence(
    *,
    lottery_id: int,
    account_id: int,
    rule_snapshot_id: int,
    execution_path_id: str,
    target_hash: str,
    rule_hash: str,
    action_plan_hash: str,
    config_hash: str,
    required_actions: tuple[str, ...],
    execution_revision: int,
    follow_target_handle: str,
    comment_text_hash: str,
    evidence_id: str | None = None,
    for_update: bool = False,
):
    row = await _load_exact_probe_shadow_execution_evidence(
        platform="xiaohongshu",
        lottery_id=lottery_id,
        account_id=account_id,
        rule_snapshot_id=rule_snapshot_id,
        execution_path_id=execution_path_id,
        target_hash=target_hash,
        rule_hash=rule_hash,
        action_plan_hash=action_plan_hash,
        config_hash=config_hash,
        evidence_id=evidence_id,
        for_update=for_update,
    )
    if not _exact_xiaohongshu_browser_observations_valid(
        row,
        lottery_id=lottery_id,
        account_id=account_id,
        rule_snapshot_id=rule_snapshot_id,
        target_hash=target_hash,
        rule_hash=rule_hash,
        action_plan_hash=action_plan_hash,
        config_hash=config_hash,
        required_actions=required_actions,
        execution_revision=execution_revision,
        follow_target_handle=follow_target_handle,
        comment_text_hash=comment_text_hash,
    ):
        return None
    return row


def _exact_douyin_device_observations_valid(
    row,
    *,
    lottery_id: int,
    account_id: int,
    rule_snapshot_id: int,
    target_hash: str,
    rule_hash: str,
    action_plan_hash: str,
    config_hash: str,
    required_actions: tuple[str, ...],
    execution_revision: int,
    follow_target_handle_hash: str,
    comment_text_hash: str,
    public_config,
) -> bool:
    if not row:
        return False
    common = {
        "expected_lottery_id": lottery_id,
        "expected_account_id": account_id,
        "expected_execution_revision": execution_revision,
        "expected_target_hash": target_hash,
        "expected_rule_snapshot_id": rule_snapshot_id,
        "expected_rule_hash": rule_hash,
        "expected_action_plan_hash": action_plan_hash,
        "expected_config_hash": config_hash,
        "expected_actions": required_actions,
        "expected_follow_target_handle_hash": follow_target_handle_hash,
        "expected_comment_text_hash": comment_text_hash,
        "expected_public_config": public_config,
    }
    try:
        validate_douyin_device_observation_binding(
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
            expected_observation_kind=DOUYIN_DEVICE_PROBE_OBSERVATION_KIND,
            expected_evidence_id=str(row_value(row, "probe_id") or ""),
            **common,
        )
        validate_douyin_device_observation_binding(
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
            expected_observation_kind=DOUYIN_DEVICE_SHADOW_OBSERVATION_KIND,
            expected_evidence_id=str(row_value(row, "shadow_task_id") or ""),
            **common,
        )
    except (DouyinDeviceContractError, TypeError, ValueError):
        return False
    return True


async def load_exact_douyin_device_execution_evidence(
    *,
    lottery_id: int,
    account_id: int,
    rule_snapshot_id: int,
    execution_path_id: str,
    target_hash: str,
    rule_hash: str,
    action_plan_hash: str,
    config_hash: str,
    required_actions: tuple[str, ...],
    execution_revision: int,
    follow_target_handle_hash: str,
    comment_text_hash: str,
    public_config,
    evidence_id: str | None = None,
    for_update: bool = False,
):
    row = await _load_exact_probe_shadow_execution_evidence(
        platform="douyin",
        lottery_id=lottery_id,
        account_id=account_id,
        rule_snapshot_id=rule_snapshot_id,
        execution_path_id=execution_path_id,
        target_hash=target_hash,
        rule_hash=rule_hash,
        action_plan_hash=action_plan_hash,
        config_hash=config_hash,
        evidence_id=evidence_id,
        for_update=for_update,
    )
    if not _exact_douyin_device_observations_valid(
        row,
        lottery_id=lottery_id,
        account_id=account_id,
        rule_snapshot_id=rule_snapshot_id,
        target_hash=target_hash,
        rule_hash=rule_hash,
        action_plan_hash=action_plan_hash,
        config_hash=config_hash,
        required_actions=required_actions,
        execution_revision=execution_revision,
        follow_target_handle_hash=follow_target_handle_hash,
        comment_text_hash=comment_text_hash,
        public_config=public_config,
    ):
        return None
    return row


async def validate_bilibili_v2_evidence(
    lottery,
    account_id: int | None,
    *,
    evidence_batch: RealRunEvidenceBatch | None = None,
) -> dict:
    """Validate the exact immutable Bilibili API-path execution contract."""

    blockers: list[str] = []
    lottery_data = dict(lottery)
    target = validate_lottery_identity(
        lottery_data.get("platform"),
        lottery_data.get("raw_url"),
        lottery_data.get("canonical_url"),
    )
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
                if (
                    evidence_batch is None
                    or not evidence_batch.account_scoped_readiness
                ):
                    snapshot = await database.fetch_one(
                        """SELECT id, platform, rule_hash, is_complete,
                                  attested_by, attested_at
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
                else:
                    rule_snapshot_ready = _batched_rule_snapshot_matches(
                        evidence_batch,
                        lottery_id=int(lottery_data.get("id")),
                        snapshot_id=plan.rule_snapshot_id,
                        platform="bilibili",
                        rule_hash=exact_rule_hash,
                        rule_text=rule_text,
                    )
                if not rule_snapshot_ready:
                    _append_blocker(blockers, "authoritative_rule_snapshot_required")

            parsed_rule = parse_lottery_rule(rule_text, "bilibili")
            parsed_actions = [
                action
                for action in SHADOW_PHASE_ORDER
                if action in set(parsed_rule.get("required_actions") or [])
            ]
            source_content_requirements = dict(
                parsed_rule.get("content_requirements")
                or {
                    "follow_targets": [],
                    "commented": {"topic_tags": [], "mentions": []},
                    "reposted": {"topic_tags": [], "mentions": []},
                }
            )
            expected_content_requirements = bind_manual_follow_target(
                parsed_actions,
                plan.action_payloads,
                source_content_requirements,
            )
            represented, unresolved, capability = semantic_requirement_status(
                list(parsed_rule.get("unsupported_actions") or []),
                plan.action_payloads,
                expected_content_requirements,
                source_content_requirements=source_content_requirements,
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
            if plan.content_requirements != expected_content_requirements:
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
        if (
            evidence_batch is None
            or not evidence_batch.account_scoped_readiness
        ):
            account = await database.fetch_one(
                """SELECT id, platform, status, execution_revision,
                          OCTET_LENGTH(encrypted_credential)
                            AS credential_size
                   FROM accounts
                   WHERE id = :account_id
                     AND deleted_at IS NULL
                   LIMIT 1""",
                {"account_id": account_id},
            )
        else:
            account = evidence_batch.accounts.get(int(account_id))
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
                from app.services.bilibili_preflight_evidence import (
                    BilibiliPreflightEvidenceError,
                    extract_bilibili_dynamic_id,
                )

                target_hash = compute_target_hash(canonical_url)
                dynamic_id = extract_bilibili_dynamic_id(
                    canonical_url, str(lottery_data.get("raw_url") or "")
                )
                config_hash = compute_bilibili_api_config_hash(execution_revision)
            except ImportError:
                _append_blocker(blockers, "platform_module_unavailable")
            except (
                ActionPlanV2Error,
                BilibiliPreflightEvidenceError,
            ) as exc:
                _append_blocker(blockers, getattr(exc, "code", str(exc)))
            else:
                if (
                    evidence_batch is None
                    or not evidence_batch.account_scoped_readiness
                ):
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
                else:
                    evidence = _select_batched_bilibili_execution_evidence(
                        evidence_batch,
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
        if (
            evidence_batch is None
            or not evidence_batch.account_scoped_readiness
        ):
            account_risk = await recent_account_risk(account_id)
        else:
            account_risk = evidence_batch.account_risks.get(
                int(account_id), account_risk_payload(None)
            )
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


async def validate_xiaohongshu_browser_contract(
    lottery,
    account_id: int | None = None,
    *,
    evidence_batch: RealRunEvidenceBatch | None = None,
) -> dict:
    """Validate XHS real-run against one exact selector/plan evidence pair."""

    # Reuse the established exact rule-snapshot and semantic reconciliation
    # checks.  This invocation has no manual capability blocker and requires
    # an executable browser plan; only the XHS-specific evidence below can
    # turn the result into an allow decision.
    plan_contract = await validate_manual_only_contract(
        lottery,
        account_id=account_id,
        platform="xiaohongshu",
        execution_path_id=XIAOHONGSHU_BROWSER_EXECUTION_PATH,
        capability_blocker=None,
        execution_path_blocker=(
            "xiaohongshu_execution_path_not_supported"
        ),
        expected_executable=True,
        media_capability_blocker=(
            "xiaohongshu_media_submission_unsupported"
        ),
        manual_shadow_supported=False,
        evidence_batch=evidence_batch,
    )
    blockers = list(plan_contract.get("blockers") or [])
    lottery_data = dict(lottery)
    plan = None
    try:
        plan = validate_action_plan_v2(
            parse_json_field(lottery_data.get("action_plan")),
            require_executable=False,
        )
    except ActionPlanV2Error:
        # The shared plan contract already emitted the precise blocker.
        pass

    selector_config = {}
    try:
        runtime_config = await load_runtime_selector_config()
        configured = runtime_config.get("xiaohongshu", {})
        if isinstance(configured, dict):
            selector_config = configured
    except Exception:
        selector_config = {}
    selector_ready = selector_config_complete(
        "xiaohongshu",
        selector_config,
    )
    if not selector_ready:
        _append_blocker(
            blockers,
            "xiaohongshu_selector_config_incomplete",
        )

    evidence = None
    execution_revision = 0
    account = None
    if account_id is None:
        _append_blocker(blockers, "execution_account_scope_required")
    else:
        if evidence_batch is not None and evidence_batch.account_scoped_readiness:
            account = evidence_batch.accounts.get(int(account_id))
        else:
            account = await database.fetch_one(
                """SELECT id, platform, status, execution_revision,
                          OCTET_LENGTH(encrypted_credential)
                            AS credential_size
                   FROM accounts
                   WHERE id = :account_id
                     AND deleted_at IS NULL
                   LIMIT 1""",
                {"account_id": account_id},
            )
        try:
            execution_revision = int(
                row_value(account, "execution_revision") or 0
            )
        except (TypeError, ValueError):
            execution_revision = 0
        account_ready = bool(
            account
            and str(row_value(account, "platform") or "").casefold()
            == "xiaohongshu"
            and str(row_value(account, "status") or "").casefold()
            == "ready"
            and int(row_value(account, "credential_size") or 0) > 0
            and execution_revision > 0
        )
        if not account_ready:
            _append_blocker(blockers, "execution_account_not_ready")
        elif (
            plan is not None
            and plan_contract.get("action_plan_ready") is True
            and selector_ready
        ):
            try:
                target_hash = compute_target_hash(
                    str(lottery_data.get("canonical_url") or "")
                )
                config_hash = compute_xiaohongshu_browser_config_hash(
                    execution_revision,
                    selector_config,
                )
                comment_text = str(
                    plan.payload_for("commented").get("text", "")
                )
                comment_text_hash = (
                    compute_xiaohongshu_comment_text_hash(comment_text)
                )
            except (
                ActionPlanV2Error,
                XiaohongshuBrowserContractError,
                TypeError,
                ValueError,
            ) as exc:
                _append_blocker(
                    blockers,
                    getattr(exc, "code", "xiaohongshu_evidence_binding_invalid"),
                )
            else:
                # Account-scoped read projections may still use this exact,
                # bounded one-row query. Dispatch always revalidates it under
                # the account lock with ``for_update=True``.
                evidence = (
                    await load_exact_xiaohongshu_browser_execution_evidence(
                        lottery_id=int(lottery_data.get("id")),
                        account_id=int(account_id),
                        rule_snapshot_id=plan.rule_snapshot_id,
                        execution_path_id=(
                            XIAOHONGSHU_BROWSER_EXECUTION_PATH
                        ),
                        target_hash=target_hash,
                        rule_hash=plan.rule_hash,
                        action_plan_hash=plan.plan_hash,
                        config_hash=config_hash,
                        required_actions=plan.required_actions,
                        execution_revision=execution_revision,
                        follow_target_handle=plan.follow_target_handle,
                        comment_text_hash=comment_text_hash,
                    )
                )

    execution_evidence_bound = bool(
        evidence
        and str(row_value(evidence, "id") or "")
        and str(row_value(evidence, "probe_id") or "")
        and str(row_value(evidence, "shadow_task_id") or "")
        and row_value(evidence, "verified_at") is not None
    )
    if account_id is not None and not execution_evidence_bound:
        _append_blocker(blockers, "exact_execution_evidence_required")

    account_risk = None
    if account_id is not None:
        if evidence_batch is not None and evidence_batch.account_scoped_readiness:
            account_risk = evidence_batch.account_risks.get(
                int(account_id),
                account_risk_payload(None),
            )
        else:
            account_risk = await recent_account_risk(int(account_id))
        if account_risk.get("has_recent_risk") is True:
            _append_blocker(blockers, "recent_account_risk_event")

    evidence_view = None
    if execution_evidence_bound:
        evidence_view = {
            "id": row_value(evidence, "id"),
            "status": "verified",
            "account_id": account_id,
            "lottery_id": lottery_data.get("id"),
            "rule_snapshot_id": plan.rule_snapshot_id if plan else None,
            "execution_path_id": XIAOHONGSHU_BROWSER_EXECUTION_PATH,
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
            "verified_at": normalize_timestamp(
                row_value(evidence, "verified_at")
            ),
            "expires_at": normalize_timestamp(
                row_value(evidence, "expires_at")
            ),
        }
    return {
        "allowed": not blockers,
        "blockers": blockers,
        "probe_ready": execution_evidence_bound,
        "shadow_ready": execution_evidence_bound,
        "action_plan_ready": bool(
            plan_contract.get("action_plan_ready")
        ),
        "rule_snapshot_ready": bool(
            plan_contract.get("rule_snapshot_ready")
        ),
        "execution_evidence_bound": execution_evidence_bound,
        "execution_evidence_id": (
            row_value(evidence, "id") if evidence else None
        ),
        "execution_evidence": evidence_view,
        "execution_path_id": XIAOHONGSHU_BROWSER_EXECUTION_PATH,
        "execution_revision": execution_revision or None,
        "execution_mode": "selector",
        "real_run_supported": True,
        "selector_config_complete": selector_ready,
        "account_risk": account_risk,
    }


async def validate_douyin_device_contract(
    lottery,
    account_id: int | None = None,
    *,
    evidence_batch: RealRunEvidenceBatch | None = None,
) -> dict:
    """Validate an exact local Android device Probe + Shadow contract."""

    plan_contract = await validate_manual_only_contract(
        lottery,
        account_id=account_id,
        platform="douyin",
        execution_path_id=DOUYIN_DEVICE_EXECUTION_PATH,
        capability_blocker=None,
        execution_path_blocker="douyin_execution_path_not_supported",
        expected_executable=True,
        media_capability_blocker="douyin_media_submission_unsupported",
        manual_shadow_supported=False,
        evidence_batch=evidence_batch,
    )
    blockers = list(plan_contract.get("blockers") or [])
    lottery_data = dict(lottery)
    try:
        plan = validate_action_plan_v2(
            parse_json_field(lottery_data.get("action_plan")),
            require_executable=False,
        )
    except ActionPlanV2Error:
        plan = None

    selector_config = {}
    public_config = None
    try:
        runtime_config = await load_runtime_selector_config()
        configured = runtime_config.get("douyin", {})
        if isinstance(configured, dict):
            selector_config = configured
        public_config = normalize_douyin_device_public_config(selector_config)
    except Exception:
        _append_blocker(blockers, "douyin_device_config_invalid")

    evidence = None
    account = None
    execution_revision = 0
    if account_id is None:
        _append_blocker(blockers, "execution_account_scope_required")
    else:
        if evidence_batch is not None and evidence_batch.account_scoped_readiness:
            account = evidence_batch.accounts.get(int(account_id))
        else:
            account = await database.fetch_one(
                """SELECT a.id, a.platform, a.status, a.execution_revision,
                          a.encrypted_credential,
                          OCTET_LENGTH(a.encrypted_credential) AS credential_size,
                          c.status AS calibration_status,
                          (c.created_at >= DATE_SUB(NOW(), INTERVAL 24 HOUR)
                           AND c.created_at <= NOW()
                           AND c.finished_at IS NOT NULL
                           AND c.finished_at <= NOW()) AS calibration_fresh
                     FROM accounts a
                     LEFT JOIN account_calibrations c
                       ON c.id = (
                         SELECT latest.id FROM account_calibrations latest
                          WHERE latest.account_id = a.id
                            AND latest.platform = a.platform
                          ORDER BY latest.id DESC LIMIT 1
                       )
                    WHERE a.id = :account_id AND a.deleted_at IS NULL LIMIT 1""",
                {"account_id": account_id},
            )
        try:
            execution_revision = int(row_value(account, "execution_revision") or 0)
        except (TypeError, ValueError):
            execution_revision = 0
        credential_ready = False
        try:
            credential = decrypt_douyin_device_credential(
                row_value(account, "encrypted_credential")
            )
            credential_ready = bool(
                public_config is not None
                and credential.get("device_agent") == public_config
            )
        except Exception:
            credential_ready = False
        account_ready = bool(
            account
            and str(row_value(account, "platform") or "").casefold() == "douyin"
            and str(row_value(account, "status") or "").casefold() == "ready"
            and credential_ready
            and str(row_value(account, "calibration_status") or "").casefold()
            == "succeeded"
            and int(row_value(account, "calibration_fresh") or 0) == 1
            and execution_revision > 0
        )
        if not account_ready:
            _append_blocker(blockers, "execution_account_not_ready")
        elif (
            plan is not None
            and plan_contract.get("action_plan_ready") is True
            and public_config is not None
        ):
            try:
                target_hash = compute_target_hash(
                    str(lottery_data.get("canonical_url") or "")
                )
                config_hash = compute_douyin_device_config_hash(
                    execution_revision, selector_config
                )
                follow_text = (
                    plan.follow_target_handle
                    if "followed" in plan.required_actions
                    else ""
                )
                comment_text = (
                    plan.payload_for("commented").get("text", "")
                    if "commented" in plan.required_actions
                    else ""
                )
                evidence = await load_exact_douyin_device_execution_evidence(
                    lottery_id=int(lottery_data.get("id")),
                    account_id=int(account_id),
                    rule_snapshot_id=plan.rule_snapshot_id,
                    execution_path_id=DOUYIN_DEVICE_EXECUTION_PATH,
                    target_hash=target_hash,
                    rule_hash=plan.rule_hash,
                    action_plan_hash=plan.plan_hash,
                    config_hash=config_hash,
                    required_actions=plan.required_actions,
                    execution_revision=execution_revision,
                    follow_target_handle_hash=compute_douyin_exact_text_hash(
                        follow_text
                    ),
                    comment_text_hash=compute_douyin_exact_text_hash(comment_text),
                    public_config=public_config,
                )
            except (DouyinDeviceContractError, TypeError, ValueError) as exc:
                _append_blocker(
                    blockers,
                    getattr(exc, "code", "douyin_device_evidence_binding_invalid"),
                )

    execution_evidence_bound = bool(
        evidence
        and str(row_value(evidence, "id") or "")
        and str(row_value(evidence, "probe_id") or "")
        and str(row_value(evidence, "shadow_task_id") or "")
        and row_value(evidence, "verified_at") is not None
    )
    if account_id is not None and not execution_evidence_bound:
        _append_blocker(blockers, "exact_execution_evidence_required")
    account_risk = None
    if account_id is not None:
        if evidence_batch is not None and evidence_batch.account_scoped_readiness:
            account_risk = evidence_batch.account_risks.get(
                int(account_id), account_risk_payload(None)
            )
        else:
            account_risk = await recent_account_risk(account_id)
        if account_risk["has_recent_risk"]:
            _append_blocker(blockers, "recent_account_risk_event")

    return {
        "allowed": not blockers,
        "blockers": blockers,
        "probe_ready": execution_evidence_bound,
        "shadow_ready": execution_evidence_bound,
        "action_plan_ready": bool(plan_contract.get("action_plan_ready")),
        "rule_snapshot_ready": bool(plan_contract.get("rule_snapshot_ready")),
        "execution_evidence_bound": execution_evidence_bound,
        "execution_evidence_id": row_value(evidence, "id") if evidence else None,
        "execution_evidence": (
            {
                "id": row_value(evidence, "id"),
                "status": "verified",
                "account_id": account_id,
                "lottery_id": lottery_data.get("id"),
                "probe_id": row_value(evidence, "probe_id"),
                "shadow_task_id": row_value(evidence, "shadow_task_id"),
                "verified_at": normalize_timestamp(
                    row_value(evidence, "verified_at")
                ),
                "expires_at": normalize_timestamp(row_value(evidence, "expires_at")),
            }
            if execution_evidence_bound
            else None
        ),
        "execution_path_id": DOUYIN_DEVICE_EXECUTION_PATH,
        "execution_revision": execution_revision or None,
        "execution_mode": "device_agent",
        "real_run_supported": True,
        "device_agent_config_complete": public_config is not None,
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
    required_action_contract_blocker: str | None = None,
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
    target = validate_lottery_identity(
        lottery_data.get("platform"),
        lottery_data.get("raw_url"),
        lottery_data.get("canonical_url"),
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
                or f"{platform}_execution_path_invalid",
            )
        if (
            required_action_contract is not None
            and tuple(plan.required_actions) != required_action_contract
        ):
            _append_blocker(
                blockers,
                required_action_contract_blocker
                or f"{platform}_required_action_contract_mismatch",
            )
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
                if (
                    evidence_batch is None
                    or not evidence_batch.account_scoped_readiness
                ):
                    snapshot = await database.fetch_one(
                        """SELECT id, platform, rule_hash, is_complete,
                                  attested_by, attested_at
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
                else:
                    rule_snapshot_ready = _batched_rule_snapshot_matches(
                        evidence_batch,
                        lottery_id=int(lottery_data.get("id")),
                        snapshot_id=plan.rule_snapshot_id,
                        platform=platform,
                        rule_hash=exact_rule_hash,
                        rule_text=rule_text,
                    )
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
            or (
                required_action_contract_blocker is not None
                and blocker == required_action_contract_blocker
            )
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
                allowed_root=platform_shadow_screenshot_root(
                    platform,
                    evidence_path,
                ),
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
    if not isinstance(parsed, dict):
        return {
            "ready": False,
            "blockers": ["weibo_oauth_capability_evidence_required"],
            "denied_actions": [],
            "evidence": None,
        }
    try:
        envelope = validate_weibo_oauth_calibration_envelope(
            parsed,
        )
    except WeiboOAuthCalibrationEnvelopeError as exc:
        return {
            "ready": False,
            "blockers": [exc.code],
            "denied_actions": [],
            "evidence": None,
        }
    capabilities = envelope["oauth_capabilities"]
    if (
        expected_uid is not None
        and envelope["identity"]["uid"] != str(expected_uid)
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
    try:
        parsed_calibration_id = UUID(str(capability_calibration_id))
    except (TypeError, ValueError, AttributeError):
        parsed_calibration_id = None
    if (
        not isinstance(capability_calibration_id, str)
        or not capability_calibration_id
        or capability_calibration_id != capability_calibration_id.strip()
        or len(capability_calibration_id) > 128
        or parsed_calibration_id is None
        or str(parsed_calibration_id) != capability_calibration_id.lower()
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
    """Validate an exact rule-derived XHS manual-action subset."""

    return await validate_manual_only_contract(
        lottery,
        account_id=account_id,
        platform="xiaohongshu",
        execution_path_id=XIAOHONGSHU_MANUAL_EXECUTION_PATH,
        capability_blocker=XIAOHONGSHU_MANUAL_EXECUTION_BLOCKER,
        execution_path_blocker="xiaohongshu_execution_path_not_supported",
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


def _select_batched_weibo_oauth_dry_run(
    batch: RealRunEvidenceBatch,
    *,
    lottery_id: int,
    account_id: int,
    rule_snapshot_id: int,
    execution_path_id: str,
    target_hash: str,
    rule_hash: str,
    action_plan_hash: str,
    config_hash: str,
):
    """Select a released dry-run row with an exact immutable plan binding."""

    for row in batch.weibo_oauth_dry_runs.get(
        (lottery_id, account_id), []
    ):
        try:
            row_snapshot_id = int(row_value(row, "rule_snapshot_id") or 0)
        except (TypeError, ValueError):
            continue
        if (
            row_snapshot_id == rule_snapshot_id
            and str(row_value(row, "execution_path_id") or "")
            == execution_path_id
            and str(row_value(row, "target_hash") or "") == target_hash
            and str(row_value(row, "rule_hash") or "") == rule_hash
            and str(row_value(row, "action_plan_hash") or "")
            == action_plan_hash
            and str(row_value(row, "config_hash") or "") == config_hash
            and _batched_weibo_dry_run_fresh_at(
                row,
                cutoff=batch.freshness_cutoff_at,
            )
        ):
            return row
    return None


async def validate_weibo_oauth_contract(
    lottery,
    account_id: int | None = None,
    *,
    execution_required_actions: tuple[str, ...] | None = None,
    evidence_batch: RealRunEvidenceBatch | None = None,
    for_update: bool = False,
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
        if (
            evidence_batch is None
            or not evidence_batch.account_scoped_readiness
        ):
            account_lock_clause = "FOR UPDATE" if for_update else ""
            account = await database.fetch_one(
                f"""SELECT a.id, a.status, a.execution_revision,
                          a.encrypted_credential,
                          c.calibration_id, c.status AS calibration_status,
                          c.result AS calibration_result,
                          (
                            c.created_at >= DATE_SUB(NOW(), INTERVAL 24 HOUR)
                            AND c.created_at <= NOW()
                            AND c.finished_at IS NOT NULL
                            AND c.finished_at <= NOW()
                          ) AS calibration_fresh
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
                     LIMIT 1
                 {account_lock_clause}""",
                {"account_id": account_id},
            )
        else:
            account = evidence_batch.accounts.get(int(account_id))
        if (
            not account
            or (
                evidence_batch is not None
                and evidence_batch.account_scoped_readiness
                and str(row_value(account, "platform") or "").casefold()
                != "weibo"
            )
        ):
            _append_blocker(blockers, "weibo_oauth_account_scope_required")
        else:
            execution_revision = int(row_value(account, "execution_revision") or 0)
            calibration_id = row_value(account, "calibration_id")
            if str(row_value(account, "status") or "").lower() != "ready":
                _append_blocker(blockers, "execution_account_not_ready")
            credential_state = None
            if (
                evidence_batch is not None
                and evidence_batch.account_scoped_readiness
            ):
                credential_state = (
                    evidence_batch.weibo_oauth_credential_states.get(
                        int(account_id)
                    )
                )
            if credential_state is None:
                encrypted_credential = row_value(
                    account, "encrypted_credential"
                )
                credential_uid = None
                credential_blocker = None
                if not encrypted_credential:
                    credential_blocker = "weibo_oauth_credential_required"
                else:
                    try:
                        decrypted_credential = cookie_vault.decrypt_strict(
                            encrypted_credential,
                            aad=CREDENTIAL_AAD,
                        )
                        parsed_credential = parse_weibo_oauth_credential(
                            decrypted_credential
                        )
                        credential_uid = parsed_credential.get("uid")
                    except WeiboOAuthCredentialError as exc:
                        credential_blocker = exc.code
                    except Exception:
                        credential_blocker = (
                            "weibo_oauth_credential_invalid"
                        )
                credential_state = (
                    credential_blocker is None,
                    credential_uid,
                    credential_blocker,
                )
                if (
                    evidence_batch is not None
                    and evidence_batch.account_scoped_readiness
                ):
                    evidence_batch.weibo_oauth_credential_states[
                        int(account_id)
                    ] = credential_state
            credential_present, credential_uid, credential_blocker = (
                credential_state
            )
            if credential_blocker:
                _append_blocker(blockers, credential_blocker)
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
                    capability_required_actions = (
                        execution_required_actions
                        if execution_required_actions is not None
                        else plan.required_actions
                    )
                    calibration_fresh = bool(
                        row_value(account, "calibration_fresh")
                    )
                    capability_now = None
                    if (
                        evidence_batch is not None
                        and evidence_batch.account_scoped_readiness
                        and evidence_batch.freshness_cutoff_at is not None
                    ):
                        calibration_fresh = (
                            _batched_weibo_calibration_fresh_at(
                                account,
                                cutoff=(
                                    evidence_batch.freshness_cutoff_at
                                ),
                            )
                        )
                        cutoff_value = normalize_datetime(
                            evidence_batch.freshness_cutoff_at
                        )
                        if cutoff_value is not None:
                            capability_now = cutoff_value.replace(
                                tzinfo=timezone.utc
                            )
                    capability = validate_weibo_oauth_capability_attestation(
                        row_value(account, "calibration_result"),
                        required_actions=capability_required_actions,
                        account_id=account_id,
                        execution_revision=execution_revision,
                        calibration_fresh=calibration_fresh,
                        expected_calibration_id=str(calibration_id or ""),
                        expected_uid=credential_uid,
                        now=capability_now,
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
                        target_hash = compute_target_hash(
                            str(dict(lottery).get("canonical_url") or "")
                        )
                        if (
                            evidence_batch is None
                            or not evidence_batch.account_scoped_readiness
                        ):
                            dry_run_lock_clause = "FOR UPDATE" if for_update else ""
                            dry_run = await database.fetch_one(
                                f"""SELECT tr.task_id
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
                                     LIMIT 1
                                 {dry_run_lock_clause}""",
                                {
                                    "lottery_id": dict(lottery).get("id"),
                                    "account_id": account_id,
                                    "rule_snapshot_id": plan.rule_snapshot_id,
                                    "execution_path_id": WEIBO_OAUTH_EXECUTION_PATH,
                                    "target_hash": target_hash,
                                    "rule_hash": plan.rule_hash,
                                    "action_plan_hash": plan.plan_hash,
                                    "config_hash": dry_config_hash,
                                },
                            )
                        else:
                            dry_run = _select_batched_weibo_oauth_dry_run(
                                evidence_batch,
                                lottery_id=int(dict(lottery).get("id")),
                                account_id=int(account_id),
                                rule_snapshot_id=plan.rule_snapshot_id,
                                execution_path_id=(
                                    WEIBO_OAUTH_EXECUTION_PATH
                                ),
                                target_hash=target_hash,
                                rule_hash=plan.rule_hash,
                                action_plan_hash=plan.plan_hash,
                                config_hash=dry_config_hash,
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
            if (
                evidence_batch is None
                or not evidence_batch.account_scoped_readiness
            ):
                account_risk = await recent_account_risk(
                    account_id,
                    for_update=for_update,
                )
            else:
                account_risk = evidence_batch.account_risks.get(
                    int(account_id), account_risk_payload(None)
                )
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


async def validate_generic_real_run_evidence(
    lottery,
    account_id: int | None = None,
    *,
    evidence_batch: RealRunEvidenceBatch | None = None,
) -> dict:
    """Preserve the shared legacy evidence gate for unregistered platforms.

    Registered lottery platforms never use this fallback. API-labelled future
    platforms fail closed without inheriting Bilibili target or evidence rules.
    """

    blockers = []
    target = validate_lottery_identity(
        lottery["platform"],
        lottery["raw_url"],
        dict(lottery).get("canonical_url"),
    )
    if not target.valid:
        blockers.append("invalid_lottery_target")
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
                allowed_root=platform_shadow_screenshot_root(
                    lottery["platform"],
                    evidence_path,
                ),
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
        if (
            evidence_batch is not None
            and evidence_batch.account_scoped_readiness
        ):
            account_risk = evidence_batch.account_risks.get(
                int(account_id), account_risk_payload(None)
            )
        else:
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



async def validate_real_run_evidence(
    lottery,
    account_id: int | None = None,
    *,
    execution_required_actions: tuple[str, ...] | None = None,
    evidence_batch: RealRunEvidenceBatch | None = None,
    for_update: bool = False,
) -> dict:
    if evidence_batch is not None:
        if for_update:
            raise ValueError(
                "read-only real-run evidence batch cannot authorize dispatch"
            )
        if not evidence_batch.supports_account(account_id):
            raise ValueError("real-run evidence batch account scope mismatch")
    platform_module = get_platform_module(str(lottery["platform"]))
    if platform_module is None:
        return await validate_generic_real_run_evidence(
            lottery,
            account_id=account_id,
            evidence_batch=evidence_batch,
        )
    readiness_context = {
        "lottery": lottery,
        "account_id": account_id,
        "evidence_batch": evidence_batch,
        "for_update": for_update,
    }
    if execution_required_actions is not None:
        readiness_context["execution_required_actions"] = (
            execution_required_actions
        )
    return await platform_module.validate_real_run_readiness(
        **readiness_context,
    )


async def evaluate_account_scoped_real_run_readiness_batch(
    lotteries,
    *,
    account_candidates: dict[str, list[dict]],
    candidate_prefilter: AccountScopedReadinessCandidatePrefilter | None = None,
    recommendation_blockers_by_platform: dict[str, str] | None = None,
    _platform_isolated: bool = False,
) -> dict[int, dict]:
    """Revalidate authoritative candidates in ranked, bounded DB batches.

    The display/ranking projection remains one O(ready accounts) query; it is
    not authorization and does not issue per-account evidence reads. Only
    accounts with a necessary persisted-evidence row for the specific lottery
    enter the account-scoped evidence batches below. This removes evidence
    batches for the much larger no-evidence population. Ranked candidates are
    inspected only up to an explicit per-platform batch budget; exhaustion is
    surfaced as its own fail-closed result instead of being misreported as
    missing evidence.
    """

    lottery_rows = [
        dict(lottery)
        for lottery in islice(
            iter(lotteries),
            MAX_ACCOUNT_SCOPED_READINESS_LOTTERIES + 1,
        )
    ]
    if len(lottery_rows) > MAX_ACCOUNT_SCOPED_READINESS_LOTTERIES:
        raise ValueError("account-scoped readiness lottery batch exceeds limit")
    if candidate_prefilter is None:
        candidate_prefilter = (
            await load_account_scoped_readiness_candidate_prefilter(
                lottery_rows
            )
        )

    platform_lottery_rows: dict[str, list[dict]] = {}
    for lottery in lottery_rows:
        platform = str(lottery.get("platform") or "").casefold()
        platform_lottery_rows.setdefault(platform, []).append(lottery)
    if not _platform_isolated and len(platform_lottery_rows) > 1:
        # Split once, then reuse the single-platform evaluator concurrently.
        # Each child creates its own deadline, so input ordering cannot lend
        # one platform another platform's evidence budget.
        partial_results = await asyncio.gather(
            *(
                evaluate_account_scoped_real_run_readiness_batch(
                    platform_lotteries,
                    account_candidates=account_candidates,
                    candidate_prefilter=(
                        AccountScopedReadinessCandidatePrefilter(
                            account_ids_by_lottery={
                                int(lottery["id"]): (
                                    candidate_prefilter.account_ids_for(
                                        int(lottery["id"])
                                    )
                                )
                                for lottery in platform_lotteries
                            },
                            failed_platforms=(
                                frozenset({platform})
                                if platform
                                in candidate_prefilter.failed_platforms
                                else frozenset()
                            ),
                            budget_blockers_by_platform=(
                                {
                                    platform: (
                                        candidate_prefilter
                                        .budget_blockers_by_platform[
                                            platform
                                        ]
                                    )
                                }
                                if platform
                                in candidate_prefilter
                                .budget_blockers_by_platform
                                else {}
                            ),
                            budget_blockers_by_lottery={
                                int(lottery["id"]): (
                                    candidate_prefilter
                                    .budget_blockers_by_lottery[
                                        int(lottery["id"])
                                    ]
                                )
                                for lottery in platform_lotteries
                                if int(lottery["id"])
                                in candidate_prefilter
                                .budget_blockers_by_lottery
                            },
                        )
                    ),
                    recommendation_blockers_by_platform=(
                        {
                            platform: (
                                recommendation_blockers_by_platform[platform]
                            )
                        }
                        if recommendation_blockers_by_platform
                        and platform in recommendation_blockers_by_platform
                        else {}
                    ),
                    _platform_isolated=True,
                )
                for platform, platform_lotteries
                in platform_lottery_rows.items()
            )
        )
        return {
            lottery_id: result
            for partial in partial_results
            for lottery_id, result in partial.items()
        }

    phase_budget = AccountScopedReadinessPhaseBudget.start(
        ACCOUNT_SCOPED_READINESS_PLATFORM_EVALUATION_TIMEOUT_SECONDS
    )

    def bounded_platform_candidates(
        platform: str,
    ) -> tuple[list[tuple[int, dict]], bool]:
        candidate_sources = account_candidates or {}
        missing = object()
        raw_candidates = candidate_sources.get(platform, missing)
        if raw_candidates is missing:
            for raw_platform, candidate_values in islice(
                iter(candidate_sources.items()),
                ACCOUNT_SCOPED_READINESS_MAX_CANDIDATE_PLATFORM_KEYS,
            ):
                if str(raw_platform or "").casefold() == platform:
                    raw_candidates = candidate_values
                    break
        if raw_candidates is missing or raw_candidates is None:
            raw_candidates = ()
        try:
            inspected = list(
                islice(
                    iter(raw_candidates),
                    ACCOUNT_SCOPED_READINESS_MAX_CANDIDATES_PER_PLATFORM
                    + 1,
                )
            )
        except TypeError:
            return [], False
        truncated = (
            len(inspected)
            > ACCOUNT_SCOPED_READINESS_MAX_CANDIDATES_PER_PLATFORM
        )
        selected: list[tuple[int, dict]] = []
        seen: set[int] = set()
        for candidate in inspected[
            :ACCOUNT_SCOPED_READINESS_MAX_CANDIDATES_PER_PLATFORM
        ]:
            try:
                account_id = int(candidate.get("account_id"))
            except (AttributeError, TypeError, ValueError):
                continue
            if account_id <= 0 or account_id in seen:
                continue
            if (
                str(candidate.get("platform") or "").casefold()
                != platform
            ):
                continue
            seen.add(account_id)
            selected.append((account_id, candidate))
        return selected, truncated

    async def evaluate(lottery, account_id, evidence_batch):
        try:
            return await validate_real_run_evidence(
                lottery,
                account_id=account_id,
                evidence_batch=evidence_batch,
            )
        except PlatformModuleUnavailableError:
            return {
                "allowed": False,
                "blockers": ["platform_module_unavailable"],
                "probe_ready": False,
                "shadow_ready": False,
                "execution_evidence_bound": False,
                "real_run_supported": False,
                "capability_reason": "platform_module_unavailable",
                "account_risk": None,
            }
        except PlatformCapabilityError as exc:
            return {
                "allowed": False,
                "blockers": [exc.code],
                "probe_ready": False,
                "shadow_ready": False,
                "execution_evidence_bound": False,
                "real_run_supported": False,
                "capability_reason": exc.code,
                "account_risk": None,
            }
        except Exception as exc:
            # Account-scoped readiness is a read-model recommendation, not an
            # execution authority. Keep an unexpected provider/data failure
            # inside the affected lottery/platform instead of failing the
            # complete four-platform strategy response. Dispatch performs its
            # own locked, exact revalidation and remains fail-closed.
            structured_log(
                "error",
                "account_readiness_candidate_revalidation_failed",
                platform=str(lottery.get("platform") or "").casefold(),
                lottery_id=lottery.get("id"),
                account_id=account_id,
                error=str(exc),
            )
            return failed_prefilter_readiness()

    def failed_prefilter_readiness() -> dict:
        blocker = "account_scoped_real_run_readiness_unavailable"
        return {
            "allowed": False,
            "blockers": [blocker],
            "probe_ready": False,
            "shadow_ready": False,
            "execution_evidence_bound": False,
            "real_run_supported": True,
            "capability_reason": blocker,
            "account_risk": None,
        }

    def budget_exhausted_readiness(blocker: str) -> dict:
        return {
            "allowed": False,
            "blockers": [blocker],
            "probe_ready": False,
            "shadow_ready": False,
            "execution_evidence_bound": False,
            "real_run_supported": True,
            "capability_reason": blocker,
            "account_risk": None,
        }

    def freshness_recheck_failed_readiness(
        readiness: dict,
        blocker: str,
    ) -> dict:
        updated = dict(readiness)
        blockers = [
            str(item)
            for item in list(readiness.get("blockers") or [])
        ]
        if blocker not in blockers:
            blockers.append(blocker)
        updated.update(
            {
                "allowed": False,
                "blockers": blockers,
                "probe_ready": False,
                "shadow_ready": False,
                "execution_preflight_ready": False,
                "oauth_dry_run_ready": False,
                "oauth_dry_run_task_id": None,
                "execution_evidence_bound": False,
                "execution_evidence_id": None,
                "capability_reason": blocker,
            }
        )
        return updated

    def evidence_missing_readiness(
        readiness: dict,
        *,
        platform: str,
        has_ready_account: bool,
    ) -> dict:
        """Replace an artificial null-account blocker with the real gap."""

        if not has_ready_account:
            return readiness
        blocker_replacement = {
            "bilibili": (
                "execution_account_scope_required",
                "exact_execution_evidence_required",
            ),
            "xiaohongshu": (
                "execution_account_scope_required",
                "exact_execution_evidence_required",
            ),
            "weibo": (
                "weibo_oauth_account_scope_required",
                "recent_oauth_dry_run_required",
            ),
        }.get(platform)
        if blocker_replacement is None:
            return readiness
        account_blocker, evidence_blocker = blocker_replacement
        updated = dict(readiness)
        blockers: list[str] = []
        replaced = False
        for blocker in list(readiness.get("blockers") or []):
            normalized = str(blocker)
            if normalized == account_blocker:
                normalized = evidence_blocker
                replaced = True
            if normalized not in blockers:
                blockers.append(normalized)
        if not replaced and evidence_blocker not in blockers:
            blockers.append(evidence_blocker)
        updated["blockers"] = blockers
        if updated.get("capability_reason") == account_blocker:
            updated["capability_reason"] = evidence_blocker
        return updated

    lotteries_by_platform: dict[str, list[dict]] = {}
    for lottery in lottery_rows:
        platform = str(lottery.get("platform") or "").casefold()
        lotteries_by_platform.setdefault(platform, []).append(lottery)

    results: dict[int, dict] = {}
    freshness_contexts: dict[
        int,
        tuple[dict, int, RealRunEvidenceBatch],
    ] = {}
    recommendation_blockers_by_platform = (
        recommendation_blockers_by_platform or {}
    )
    for platform, platform_lotteries in lotteries_by_platform.items():
        recommendation_blocker = (
            recommendation_blockers_by_platform.get(platform)
        )
        if recommendation_blocker:
            for lottery in platform_lotteries:
                results[int(lottery["id"])] = {
                    "account_id": None,
                    "readiness": budget_exhausted_readiness(
                        recommendation_blocker
                    ),
                }
            continue
        if platform in candidate_prefilter.failed_platforms:
            for lottery in platform_lotteries:
                results[int(lottery["id"])] = {
                    "account_id": None,
                    "readiness": failed_prefilter_readiness(),
                }
            continue

        platform_budget_blocker = (
            candidate_prefilter.budget_blockers_by_platform.get(platform)
        )
        if phase_budget.remaining_seconds() <= 0:
            platform_budget_blocker = (
                ACCOUNT_SCOPED_READINESS_PLATFORM_EVALUATION_TIMEOUT_BLOCKER
            )
        if (
            platform_budget_blocker
            in {
                ACCOUNT_SCOPED_READINESS_PREFILTER_TIMEOUT_BLOCKER,
                ACCOUNT_SCOPED_READINESS_PLATFORM_EVALUATION_TIMEOUT_BLOCKER,
            }
        ):
            for lottery in platform_lotteries:
                results[int(lottery["id"])] = {
                    "account_id": None,
                    "readiness": budget_exhausted_readiness(
                        platform_budget_blocker
                    ),
                }
            continue

        eligible_ids_by_lottery = {
            int(lottery["id"]): candidate_prefilter.account_ids_for(
                int(lottery["id"])
            )
            for lottery in platform_lotteries
        }
        eligible_union = frozenset(
            account_id
            for account_ids in eligible_ids_by_lottery.values()
            for account_id in account_ids
        )
        scoped_platform_candidates, candidate_source_truncated = (
            bounded_platform_candidates(platform)
        )
        candidates = [
            candidate
            for candidate in scoped_platform_candidates
            if candidate[0] in eligible_union
        ]
        ranked_candidate_ids = frozenset(
            account_id for account_id, _candidate in candidates
        )
        if (
            candidate_source_truncated
            and not eligible_union.issubset(ranked_candidate_ids)
            and platform_budget_blocker is None
        ):
            platform_budget_blocker = (
                ACCOUNT_SCOPED_READINESS_CANDIDATE_BUDGET_BLOCKER
            )
        without_ranked_candidate = [
            lottery
            for lottery in platform_lotteries
            if not (
                eligible_ids_by_lottery[int(lottery["id"])]
                & ranked_candidate_ids
            )
        ]
        without_ranked_candidate_ids = {
            int(lottery["id"]) for lottery in without_ranked_candidate
        }
        platform_budget_exhausted = False
        if without_ranked_candidate:
            try:
                evidence_batch = (
                    await phase_budget.run(
                        lambda: (
                            load_account_scoped_real_run_readiness_batch(
                                without_ranked_candidate,
                                account_ids=(),
                            )
                        )
                    )
                )
                platform_budget_blocker = (
                    evidence_batch.readiness_budget_blockers_by_platform.get(
                        platform,
                        platform_budget_blocker,
                    )
                )
            except AccountScopedReadinessPhaseBudgetExhausted:
                platform_budget_blocker = (
                    ACCOUNT_SCOPED_READINESS_PLATFORM_EVALUATION_TIMEOUT_BLOCKER
                )
                platform_budget_exhausted = True
            except AccountScopedReadinessRiskQueryTimeout:
                platform_budget_blocker = (
                    ACCOUNT_SCOPED_READINESS_RISK_QUERY_TIMEOUT_BLOCKER
                )
                platform_budget_exhausted = True
            except Exception as exc:
                structured_log(
                    "error",
                    "account_readiness_platform_batch_load_failed",
                    platform=platform,
                    lottery_count=len(platform_lotteries),
                    error=str(exc),
                )
                for lottery in platform_lotteries:
                    results[int(lottery["id"])] = {
                        "account_id": None,
                        "readiness": failed_prefilter_readiness(),
                    }
                continue
            has_ready_account = bool(
                scoped_platform_candidates
            )
            for lottery in (
                () if platform_budget_exhausted
                else without_ranked_candidate
            ):
                lottery_id = int(lottery["id"])
                try:
                    readiness = await phase_budget.run(
                        lambda lottery=lottery: evaluate(
                            lottery,
                            None,
                            evidence_batch,
                        )
                    )
                except AccountScopedReadinessPhaseBudgetExhausted:
                    platform_budget_blocker = (
                        ACCOUNT_SCOPED_READINESS_PLATFORM_EVALUATION_TIMEOUT_BLOCKER
                    )
                    platform_budget_exhausted = True
                    break
                results[lottery_id] = {
                    "account_id": None,
                    "readiness": evidence_missing_readiness(
                        readiness,
                        platform=platform,
                        has_ready_account=(
                            has_ready_account
                            or int(lottery.get("safe_accounts") or 0) > 0
                        ),
                    ),
                }

        unresolved = [
            lottery
            for lottery in platform_lotteries
            if int(lottery["id"]) not in without_ranked_candidate_ids
        ]
        fallback_entries: dict[int, dict] = {}
        platform_batch_load_failed = False
        for platform_batch_index, batch_start in enumerate(
            range(
                0,
                len(candidates),
                ACCOUNT_SCOPED_READINESS_ACCOUNT_BATCH_SIZE,
            )
        ):
            if platform_budget_exhausted or not unresolved:
                break
            if (
                platform_batch_index
                >= ACCOUNT_SCOPED_READINESS_MAX_ACCOUNT_BATCHES_PER_PLATFORM
            ):
                platform_budget_blocker = (
                    ACCOUNT_SCOPED_READINESS_CANDIDATE_BUDGET_BLOCKER
                )
                break
            candidate_batch = candidates[
                batch_start : (
                    batch_start
                    + ACCOUNT_SCOPED_READINESS_ACCOUNT_BATCH_SIZE
                )
            ]
            unresolved_eligible_ids = frozenset(
                account_id
                for lottery in unresolved
                for account_id in eligible_ids_by_lottery[
                    int(lottery["id"])
                ]
            )
            candidate_batch = [
                candidate
                for candidate in candidate_batch
                if candidate[0] in unresolved_eligible_ids
            ]
            if not candidate_batch:
                continue
            try:
                evidence_batch = (
                    await phase_budget.run(
                        lambda: (
                            load_account_scoped_real_run_readiness_batch(
                                unresolved,
                                account_ids=[
                                    account_id
                                    for account_id, _candidate
                                    in candidate_batch
                                ],
                            )
                        )
                    )
                )
                batch_budget_blocker = (
                    evidence_batch.readiness_budget_blockers_by_platform.get(
                        platform
                    )
                )
                if batch_budget_blocker is not None:
                    platform_budget_blocker = batch_budget_blocker
            except AccountScopedReadinessPhaseBudgetExhausted:
                platform_budget_blocker = (
                    ACCOUNT_SCOPED_READINESS_PLATFORM_EVALUATION_TIMEOUT_BLOCKER
                )
                platform_budget_exhausted = True
                break
            except AccountScopedReadinessRiskQueryTimeout:
                platform_budget_blocker = (
                    ACCOUNT_SCOPED_READINESS_RISK_QUERY_TIMEOUT_BLOCKER
                )
                break
            except Exception as exc:
                structured_log(
                    "error",
                    "account_readiness_platform_batch_load_failed",
                    platform=platform,
                    lottery_count=len(platform_lotteries),
                    error=str(exc),
                )
                platform_batch_load_failed = True
                break
            still_unresolved: list[dict] = []
            for lottery in unresolved:
                lottery_id = int(lottery["id"])
                resolved = False
                for account_id, _candidate in candidate_batch:
                    if (
                        account_id
                        not in eligible_ids_by_lottery[lottery_id]
                    ):
                        continue
                    try:
                        readiness = await phase_budget.run(
                            lambda lottery=lottery, account_id=account_id: (
                                evaluate(
                                    lottery,
                                    account_id,
                                    evidence_batch,
                                )
                            )
                        )
                    except AccountScopedReadinessPhaseBudgetExhausted:
                        platform_budget_blocker = (
                            ACCOUNT_SCOPED_READINESS_PLATFORM_EVALUATION_TIMEOUT_BLOCKER
                        )
                        platform_budget_exhausted = True
                        break
                    entry = {
                        "account_id": account_id,
                        "readiness": readiness,
                    }
                    current_fallback = fallback_entries.get(lottery_id)
                    if (
                        current_fallback is None
                        or (
                            current_fallback["readiness"].get(
                                "capability_reason"
                            )
                            == "account_scoped_real_run_readiness_unavailable"
                            and readiness.get("capability_reason")
                            != "account_scoped_real_run_readiness_unavailable"
                        )
                    ):
                        # An unexpected validation failure is scoped to this
                        # account candidate. Keep searching because a later
                        # exact-evidence/OAuth candidate can still be
                        # authoritative. Prefer a normally evaluated fallback
                        # if no candidate ultimately becomes ready.
                        fallback_entries[lottery_id] = entry
                    if readiness.get("allowed") is True:
                        results[lottery_id] = entry
                        if evidence_batch.freshness_snapshot_at is not None:
                            freshness_contexts[lottery_id] = (
                                lottery,
                                account_id,
                                evidence_batch,
                            )
                        resolved = True
                        break
                    if readiness.get("capability_reason") in {
                        "platform_module_unavailable",
                        "platform_real_run_readiness_provider_missing",
                    }:
                        results[lottery_id] = entry
                        resolved = True
                        break
                if platform_budget_exhausted:
                    still_unresolved.append(lottery)
                    continue
                if not resolved:
                    still_unresolved.append(lottery)
            unresolved = still_unresolved
            if batch_budget_blocker is not None:
                break

        if platform_batch_load_failed:
            for lottery in platform_lotteries:
                results[int(lottery["id"])] = {
                    "account_id": None,
                    "readiness": failed_prefilter_readiness(),
                }
            continue

        if platform_budget_blocker is not None:
            for lottery in platform_lotteries:
                lottery_id = int(lottery["id"])
                existing = results.get(lottery_id)
                if (
                    existing is not None
                    and existing["readiness"].get("allowed") is True
                ):
                    continue
                results[lottery_id] = {
                    "account_id": None,
                    "readiness": budget_exhausted_readiness(
                        platform_budget_blocker
                    ),
                }
            continue

        remaining_unresolved = []
        for lottery in unresolved:
            lottery_id = int(lottery["id"])
            lottery_budget_blocker = (
                candidate_prefilter
                .budget_blockers_by_lottery
                .get(lottery_id)
            )
            if lottery_budget_blocker is None:
                remaining_unresolved.append(lottery)
                continue
            results[lottery_id] = {
                "account_id": None,
                "readiness": budget_exhausted_readiness(
                    lottery_budget_blocker
                ),
            }

        for lottery in remaining_unresolved:
            lottery_id = int(lottery["id"])
            fallback_entry = fallback_entries.get(lottery_id)
            if fallback_entry is None:
                structured_log(
                    "error",
                    "account_readiness_candidate_revalidation_incomplete",
                    platform=platform,
                    lottery_id=lottery_id,
                )
                fallback_entry = {
                    "account_id": None,
                    "readiness": failed_prefilter_readiness(),
                }
            results[lottery_id] = fallback_entry

    freshness_targets = {
        lottery_id: context
        for lottery_id, context in freshness_contexts.items()
        if results.get(lottery_id, {})
        .get("readiness", {})
        .get("allowed")
        is True
    }
    if freshness_targets:
        async def final_freshness_revalidation():
            account_ids_by_batch: dict[
                int,
                tuple[RealRunEvidenceBatch, set[int]],
            ] = {}
            selected_account_ids: set[int] = set()
            for _lottery_id, (_lottery, account_id, batch) in (
                freshness_targets.items()
            ):
                normalized_account_id = int(account_id)
                if normalized_account_id not in batch.account_ids:
                    raise RuntimeError(
                        "final mutable-state account is outside its batch"
                    )
                selected_account_ids.add(normalized_account_id)
                batch_entry = account_ids_by_batch.setdefault(
                    id(batch),
                    (batch, set()),
                )
                batch_entry[1].add(normalized_account_id)

            (
                final_state_now,
                refreshed_accounts,
                refreshed_risks,
            ) = await _load_final_account_mutable_state_snapshot(
                selected_account_ids
            )
            cutoff = final_state_now + timedelta(
                seconds=max(
                    float(
                        ACCOUNT_SCOPED_READINESS_RESPONSE_SAFETY_MARGIN_SECONDS
                    ),
                    0.0,
                )
            )
            for batch, batch_account_ids in account_ids_by_batch.values():
                for account_id in batch_account_ids:
                    previous = batch.accounts.pop(account_id, None)
                    refreshed = refreshed_accounts.get(account_id)
                    if refreshed is not None:
                        batch.accounts[account_id] = refreshed
                    if (
                        row_value(previous, "execution_revision")
                        != row_value(refreshed, "execution_revision")
                        or row_value(previous, "encrypted_credential")
                        != row_value(refreshed, "encrypted_credential")
                    ):
                        batch.weibo_oauth_credential_states.pop(
                            account_id,
                            None,
                        )
                    batch.account_risks[account_id] = (
                        refreshed_risks[account_id]
                    )
                batch.freshness_cutoff_at = cutoff
            updates = {}
            for lottery_id, (lottery, account_id, batch) in (
                freshness_targets.items()
            ):
                updates[lottery_id] = await evaluate(
                    lottery,
                    account_id,
                    batch,
                )
            return updates

        try:
            freshness_updates = await asyncio.wait_for(
                final_freshness_revalidation(),
                timeout=max(
                    float(
                        ACCOUNT_SCOPED_READINESS_FRESHNESS_RECHECK_TIMEOUT_SECONDS
                    ),
                    0.001,
                ),
            )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError as exc:
            structured_log(
                "warning",
                "account_readiness_freshness_recheck_timeout",
                lottery_count=len(freshness_targets),
                error=str(exc),
            )
            for lottery_id in freshness_targets:
                existing = results[lottery_id]
                results[lottery_id] = {
                    "account_id": existing.get("account_id"),
                    "readiness": freshness_recheck_failed_readiness(
                        existing["readiness"],
                        (
                            ACCOUNT_SCOPED_READINESS_FRESHNESS_RECHECK_TIMEOUT_BLOCKER
                        ),
                    ),
                }
        except Exception as exc:
            structured_log(
                "warning",
                "account_readiness_freshness_recheck_failed",
                lottery_count=len(freshness_targets),
                error=str(exc),
            )
            for lottery_id in freshness_targets:
                existing = results[lottery_id]
                results[lottery_id] = {
                    "account_id": existing.get("account_id"),
                    "readiness": freshness_recheck_failed_readiness(
                        existing["readiness"],
                        (
                            ACCOUNT_SCOPED_READINESS_FRESHNESS_RECHECK_FAILED_BLOCKER
                        ),
                    ),
                }
        else:
            for lottery_id, readiness in freshness_updates.items():
                results[lottery_id] = {
                    "account_id": freshness_targets[lottery_id][1],
                    "readiness": readiness,
                }
    return results


def _safe_readiness_blocker_codes(readiness: dict) -> list[str]:
    raw_blockers = readiness.get("blockers")
    if not isinstance(raw_blockers, list):
        return ["account_scoped_real_run_readiness_unavailable"]
    result = []
    allowed_prefixes = (
        "account_",
        "action_plan_",
        "api_",
        "authoritative_",
        "autopilot_",
        "bilibili_",
        "capability_",
        "douyin_",
        "dry_",
        "exact_",
        "execution_",
        "global_",
        "invalid_",
        "lottery_",
        "manual_",
        "no_",
        "oauth_",
        "platform_",
        "real_",
        "recent_",
        "selector_",
        "shadow_",
        "strategy_",
        "target_",
        "weibo_",
        "xiaohongshu_",
    )
    for raw_code in raw_blockers:
        code = str(raw_code or "").strip().casefold()
        if (
            not code
            or len(code) > 128
            or not code.startswith(allowed_prefixes)
            or not all(
                character.isalnum() or character in {"_", "-", "."}
                for character in code
            )
        ):
            code = "account_scoped_real_run_readiness_unavailable"
        if code not in result:
            result.append(code)
    return result


async def evaluate_exact_real_candidate_observation(
    lotteries,
    *,
    observation_limit: int = MAX_ACCOUNT_SCOPED_READINESS_LOTTERIES,
    source_observation_truncated: bool = False,
    failure_limit: int = 3,
) -> dict:
    """Observe an exact, lease-free real-run candidate without authorizing it.

    The returned projection is intentionally payload-free.  It proves only
    that a bounded snapshot contains one target/account pair accepted by the
    same account-scoped evidence validator used by the strategy queue.  Locked
    dispatch still performs its own fresh authoritative validation.
    """

    bounded_limit = min(
        max(int(observation_limit or 1), 1),
        MAX_ACCOUNT_SCOPED_READINESS_LOTTERIES,
    )
    bounded_failure_limit = max(int(failure_limit or 1), 1)
    inspected = [
        dict(row)
        for row in islice(iter(lotteries), bounded_limit + 1)
    ]
    input_truncated = len(inspected) > bounded_limit
    rows = inspected[:bounded_limit]
    base = {
        "available": True,
        "ready": False,
        "blocker_code": "autopilot_exact_real_candidate_required",
        "candidate_count": 0,
        "candidate": None,
        "observed_targets": len(rows),
        "observation_limit": bounded_limit,
        "observation_truncated": bool(
            source_observation_truncated or input_truncated
        ),
        "account_candidate_truncated_platforms": [],
        "blocker_counts": {},
    }
    if not rows:
        if base["observation_truncated"]:
            base["blocker_code"] = (
                "autopilot_exact_candidate_observation_truncated"
            )
        return base

    platforms = tuple(
        dict.fromkeys(
            str(row.get("platform") or "").strip().casefold()
            for row in rows
            if str(row.get("platform") or "").strip().casefold()
            in PLATFORM_IDS
        )
    )
    candidates = await load_account_scoped_readiness_account_candidates(
        platforms,
        exclude_active_leases=True,
    )
    candidate_prefilter = (
        await load_account_scoped_readiness_candidate_prefilter(rows)
    )
    readiness_by_lottery = (
        await evaluate_account_scoped_real_run_readiness_batch(
            rows,
            account_candidates=candidates,
            candidate_prefilter=candidate_prefilter,
            recommendation_blockers_by_platform=(
                candidates.blockers_by_platform
            ),
        )
    )

    blocker_counts: dict[str, int] = {}
    exact_candidates = []
    observation_unavailable = bool(
        candidates.blockers_by_platform
        or candidate_prefilter.failed_platforms
        or candidate_prefilter.budget_blockers_by_platform
        or candidate_prefilter.budget_blockers_by_lottery
    )
    for row in rows:
        try:
            lottery_id = int(row.get("id"))
        except (TypeError, ValueError):
            continue
        platform = str(row.get("platform") or "").strip().casefold()
        entry = readiness_by_lottery.get(lottery_id) or {}
        readiness = entry.get("readiness")
        readiness = readiness if isinstance(readiness, dict) else {}
        blocker_codes = _safe_readiness_blocker_codes(readiness)
        for code in blocker_codes:
            blocker_counts[code] = blocker_counts.get(code, 0) + 1
            if (
                "timeout" in code
                or "unavailable" in code
                or "query_failed" in code
                or "budget_exhausted" in code
            ):
                observation_unavailable = True

        try:
            account_id = int(entry.get("account_id"))
        except (TypeError, ValueError):
            account_id = 0
        try:
            platform_module = get_platform_module(platform)
            target = validate_lottery_identity(
                platform,
                row.get("raw_url"),
                row.get("canonical_url"),
            )
            target_valid = bool(
                platform_module
                and platform_module.strategy_target_is_real_valid(target)
            )
        except (
            ImportError,
            PlatformCapabilityError,
            PlatformModuleUnavailableError,
            TypeError,
            ValueError,
        ):
            target_valid = False
        active_runs = max(int(row.get("active_runs") or 0), 0)
        dry_success = max(int(row.get("dry_success") or 0), 0)
        shadow_success = max(int(row.get("shadow_success") or 0), 0)
        failed_runs = max(int(row.get("failed_runs") or 0), 0)
        target_state_valid = (
            str(row.get("status") or "").strip().casefold()
            in {"pending", "claimed"}
        )
        if not target_valid:
            blocker_counts["invalid_lottery_target"] = (
                blocker_counts.get("invalid_lottery_target", 0) + 1
            )
        if active_runs > 0:
            blocker_counts["active_run_in_progress"] = (
                blocker_counts.get("active_run_in_progress", 0) + 1
            )
        if dry_success <= 0:
            blocker_counts["dry_validation_needed"] = (
                blocker_counts.get("dry_validation_needed", 0) + 1
            )
        if shadow_success <= 0:
            blocker_counts["shadow_validation_needed"] = (
                blocker_counts.get("shadow_validation_needed", 0) + 1
            )
        if failed_runs >= bounded_failure_limit:
            blocker_counts["autopilot_failure_limit_reached"] = (
                blocker_counts.get("autopilot_failure_limit_reached", 0) + 1
            )
        if not target_state_valid:
            blocker_counts["lottery_not_pending"] = (
                blocker_counts.get("lottery_not_pending", 0) + 1
            )
        if not (
            readiness.get("allowed") is True
            and account_id > 0
            and target_valid
            and target_state_valid
            and active_runs == 0
            and dry_success > 0
            and shadow_success > 0
            and failed_runs < bounded_failure_limit
        ):
            continue
        exact_candidates.append({
            "lottery_id": lottery_id,
            "platform": platform,
            "account_id": account_id,
            "target_valid": True,
            "account_lease_available": True,
            "execution_readiness_allowed": True,
            "execution_readiness_blockers": [],
            "action_plan_ready": bool(
                readiness.get("action_plan_ready")
            ),
            "rule_snapshot_ready": bool(
                readiness.get("rule_snapshot_ready")
            ),
            "execution_evidence_bound": bool(
                readiness.get("execution_evidence_bound")
            ),
            "probe_ready": bool(readiness.get("probe_ready")),
            "shadow_ready": bool(readiness.get("shadow_ready")),
            "oauth_dry_run_ready": bool(
                readiness.get("oauth_dry_run_ready")
            ),
            "dry_success": dry_success,
            "shadow_success": shadow_success,
            "active_runs": active_runs,
            "failed_runs": failed_runs,
            "failure_limit": bounded_failure_limit,
        })

    account_candidates_truncated = bool(candidates.truncated_platforms)
    observation_truncated = bool(
        base["observation_truncated"] or account_candidates_truncated
    )
    # The strategy queue re-ranks the same bounded account source by historical
    # reputation. If its 65th overflow sentinel exists, this lightweight
    # observer cannot prove that the exact account remains inside the queue's
    # first 64 after that ranking. Stay fail-closed instead of manufacturing a
    # candidate the actual policy queue might not project.
    ready = bool(
        exact_candidates
        and not observation_unavailable
        and not account_candidates_truncated
    )
    blocker_code = None if ready else "autopilot_exact_real_candidate_required"
    if observation_unavailable:
        blocker_code = "autopilot_exact_candidate_observation_unavailable"
    elif account_candidates_truncated:
        blocker_code = "autopilot_exact_candidate_observation_truncated"
    elif observation_truncated and not exact_candidates:
        blocker_code = "autopilot_exact_candidate_observation_truncated"
    return {
        **base,
        "available": not observation_unavailable,
        "ready": ready,
        "blocker_code": blocker_code,
        "candidate_count": len(exact_candidates),
        "candidate": exact_candidates[0] if exact_candidates else None,
        "observation_truncated": observation_truncated,
        "account_candidate_truncated_platforms": sorted(
            candidates.truncated_platforms
        ),
        "blocker_counts": dict(sorted(blocker_counts.items())),
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
    if "unsupported_platform" in blockers:
        return "blocked"
    if (
        XIAOHONGSHU_MANUAL_EXECUTION_BLOCKER in blockers
        or XIAOHONGSHU_NO_OFFICIAL_API_BLOCKER in blockers
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


def unavailable_platform_real_run_gate(
    lottery,
    *,
    real_run_enabled: bool,
    blocker: str,
    target_kind: str | None = None,
    target_error: str | None = None,
) -> dict:
    """Build the fail-closed read projection for one unavailable platform."""

    return {
        "lottery_id": lottery["id"],
        "platform": lottery["platform"],
        "status": lottery["status"],
        "raw_url": lottery["raw_url"],
        "target_valid": False,
        "target_kind": target_kind,
        "target_error": target_error or blocker,
        "allowed": False,
        "blockers": [blocker],
        "next_action": "blocked",
        "real_run_enabled": real_run_enabled,
        "adapter_enabled": False,
        "adapter_kind": "none",
        "selector_ready": False,
        "api_adapter_ready": False,
        "oauth_adapter_ready": False,
        "safe_accounts": 0,
        "risk_clear_accounts": 0,
        "account_risk": None,
        "probe_ready": False,
        "shadow_ready": False,
        "execution_preflight_ready": False,
        "oauth_dry_run_ready": False,
        "oauth_dry_run_task_id": None,
        "action_plan_ready": False,
        "rule_snapshot_ready": False,
        "execution_evidence_bound": False,
        "execution_evidence_id": None,
        "execution_evidence": None,
        "execution_path_id": None,
        "execution_mode": None,
        "real_run_supported": False,
        "capability_reason": blocker,
        "oauth_capability_denied_actions": [],
        "manual_shadow_supported": False,
        "selector_observation_complete": False,
        "manual_confirmation_required": False,
        "action_plan": parse_json_field(dict(lottery).get("action_plan")),
    }


async def real_run_gate_status(
    lottery,
    *,
    selector_config: dict,
    real_run_enabled: bool,
    account_id: int | None = None,
    execution_required_actions: tuple[str, ...] | None = None,
    account_summary: dict | None = None,
    evidence_batch: RealRunEvidenceBatch | None = None,
) -> dict:
    platform = lottery["platform"]
    try:
        platform_module = get_platform_module(str(platform))
    except (ImportError, PlatformModuleUnavailableError):
        # This is a cross-platform read projection, not dispatch authority.
        # Keep one broken optional runtime inside its own lottery entry while
        # preserving the exception in validate_real_run_evidence(), whose
        # for_update path is used to authorize a single target.
        return unavailable_platform_real_run_gate(
            lottery,
            real_run_enabled=real_run_enabled,
            blocker="platform_module_unavailable",
        )
    if platform_module is None:
        # Historical databases can contain rows created before the current
        # four-platform registry existed.  Evidence is a batch/read endpoint:
        # one unsupported legacy row must fail closed locally rather than
        # raising a registry KeyError that turns the whole response into 500.
        target = validate_lottery_identity(
            platform,
            lottery["raw_url"],
            dict(lottery).get("canonical_url"),
        )
        return unavailable_platform_real_run_gate(
            lottery,
            real_run_enabled=real_run_enabled,
            blocker="unsupported_platform",
            target_kind=target.kind,
            target_error=target.reason,
        )
    if account_summary is None:
        account_summary = await real_run_account_risk_summary(platform)
    try:
        cfg = get_platform(platform) or {}
        target = validate_lottery_identity(
            platform,
            lottery["raw_url"],
            dict(lottery).get("canonical_url"),
        )
        real_run_target_valid = (
            platform_module.strategy_target_is_real_valid(target)
        )
        selector_ready = platform_selectors_complete(
            selector_config,
            platform,
        )
        adapter_kind = platform_real_adapter_kind(selector_config, platform)
        adapter_enabled = bool(
            cfg.get("action_adapter")
        ) or platform_has_runtime_real_adapter(selector_config, platform)
        evidence = await validate_real_run_evidence(
            lottery,
            account_id=account_id,
            execution_required_actions=execution_required_actions,
            evidence_batch=evidence_batch,
        )
    except (ImportError, PlatformModuleUnavailableError):
        return unavailable_platform_real_run_gate(
            lottery,
            real_run_enabled=real_run_enabled,
            blocker="platform_module_unavailable",
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
        else platform_module.strategy_target_error(target),
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
        "account_risk": evidence["account_risk"]
        or account_summary.get("controlling_active_risk")
        or account_summary["latest_recent_risk"],
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
