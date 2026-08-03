import asyncio
import re
import json
from datetime import datetime, timedelta
from itertools import islice
from urllib.parse import unquote

from app.db import database, execute_affected_rows
from app.models.schemas import LOTTERY_RAW_URL_MAX_LENGTH, LOTTERY_SOURCE_ID_MAX_LENGTH
from app.platform_modules import (
    PlatformCapabilityError,
    PlatformDiscoverySession,
    PlatformModuleUnavailableError,
    get_platform_module,
    registered_platforms,
)
from app.platform_modules.catalog import (
    BILIBILI_COLLECTION_RUN_BUDGET,
    BILIBILI_KEYWORD_QUERY_MAX_CHARS,
    BILIBILI_KEYWORD_SEARCH_CALL_RUN_BUDGET,
    BILIBILI_KEYWORD_SOURCE_QUERY_LIMIT,
    AttemptBudget,
    ExpansionBudget,
    KeywordSearchCallBudget,
    split_bilibili_keywords,
)
from app.platform_modules.base import extract_discovery_urls
from app.services.rule_provenance import ensure_rule_snapshot
from app.utils.cookies import parse_cookie_payload
from app.utils.crypto import CREDENTIAL_AAD, cookie_vault
from app.utils.canonicalizer import canonicalize_platform_url
from app.utils.lottery_targets import validate_lottery_target
from app.utils.log import structured_log
from shared.platform_scope import normalize_platform_scope


LOTTERY_KEYWORDS = (
    "\u62bd\u5956",
    "\u8f6c\u53d1\u62bd",
    "\u62bd\u9001",
    "\u798f\u5229",
    "\u5956\u54c1",
    "\u4e2d\u5956",
    "lottery",
    "giveaway",
    "prize",
)
DEFAULT_EXPIRES_DAYS = 14
DISCOVERY_ACTIVE_SOURCE_SCAN_LIMIT = 100
# Each platform gets an independent oldest-first candidate window. A fair
# round-robin then selects at most the existing global limit, so one busy
# platform cannot hide every due source from the other three while total
# external discovery work remains bounded at 100 per run.
DISCOVERY_PLATFORM_SOURCE_QUERY_LIMIT = DISCOVERY_ACTIVE_SOURCE_SCAN_LIMIT
# Database source lookup and provider work have separate platform-local
# deadlines. They are intentionally constants (rather than operator supplied
# request values) so callers cannot turn the scheduler into an unbounded wait.
DISCOVERY_PLATFORM_SOURCE_QUERY_TIMEOUT_SECONDS = 10.0
DISCOVERY_PLATFORM_SCAN_TIMEOUT_SECONDS = 120.0
DISCOVERY_PLATFORM_FINALIZE_TIMEOUT_SECONDS = 5.0
BILIBILI_COLLECTION_SOURCE_LIMIT = 80
BILIBILI_KEYWORD_SEARCH_PAGES_PER_CALL = 2
BILIBILI_KEYWORD_SEARCH_RESULT_LIMIT = 30
BILIBILI_KEYWORD_SOURCE_CANDIDATE_LIMIT = 80
BILIBILI_COLLECTION_TRUNCATE_MARKERS = (
    "\u5145\u7535\u611f\u8c22\u540d\u5355",
    "\u672c\u6708\u5145\u7535",
    "\u4e0a\u6708\u5145\u7535",
    "\u611f\u8c22\u60a8\u5bf9",
)
BILIBILI_UP_REF_RE = re.compile(
    r"[\u3010\[](?P<name>[^\u3010\u3011\[\]]{1,80})[\u3011\]][\u3001,，\s]*"
    r"[\u3010\[](?P<uid>\d{4,16})[\u3011\]]"
)


_discovery_run_task: asyncio.Task | None = None
_scoped_discovery_run_tasks: dict[tuple[str, ...], asyncio.Task] = {}
_platform_discovery_tasks: dict[str, asyncio.Task] = {}


async def fetch_bilibili_space_dynamics(*args, **kwargs):
    """Compatibility seam that loads the Bilibili provider on first use."""

    from app.services.bilibili_discovery import (
        fetch_bilibili_space_dynamics as provider,
    )

    return await provider(*args, **kwargs)


async def fetch_bilibili_keyword_search(*args, **kwargs):
    """Compatibility seam that loads the Bilibili provider on first use."""

    from app.services.bilibili_discovery import (
        fetch_bilibili_keyword_search as provider,
    )

    return await provider(*args, **kwargs)


async def run_discovery(*, platforms=None):
    """Coalesce concurrent in-process scans onto one authoritative run."""

    global _discovery_run_task
    selected_platforms = normalize_platform_scope(
        "all" if platforms is None else platforms
    )
    default_platforms = tuple(registered_platforms())
    if selected_platforms == default_platforms:
        task = _discovery_run_task
    else:
        task = _scoped_discovery_run_tasks.get(selected_platforms)
    if task is None or task.done():
        # There is no await between observing and publishing the task, so this
        # assignment is atomic for the single asyncio loop used by uvicorn.
        task = asyncio.create_task(
            _run_discovery_once(platforms=selected_platforms),
            name=(
                "dpms-discovery-scan:"
                + ",".join(selected_platforms)
            ),
        )
        if selected_platforms == default_platforms:
            _discovery_run_task = task
            task.add_done_callback(_finish_discovery_singleflight)
        else:
            _scoped_discovery_run_tasks[selected_platforms] = task
            task.add_done_callback(
                lambda completed, scope=selected_platforms: (
                    _finish_scoped_discovery_singleflight(
                        scope,
                        completed,
                    )
                )
            )

    # A cancelled HTTP caller must not cancel the shared scheduler/API scan.
    # Return nested copies so one caller cannot mutate another caller's
    # per-platform result from the shared singleflight task.
    return _copy_discovery_stats(await asyncio.shield(task))


def _copy_discovery_stats(stats: dict) -> dict:
    copied = dict(stats)
    by_platform = stats.get("by_platform")
    if isinstance(by_platform, dict):
        copied["by_platform"] = {
            platform: dict(platform_stats)
            for platform, platform_stats in by_platform.items()
        }
    return copied


def _finish_discovery_singleflight(task: asyncio.Task) -> None:
    global _discovery_run_task
    if _discovery_run_task is task:
        _discovery_run_task = None
    if task.cancelled():
        return
    # Retrieve an exception even when every waiter was cancelled. Awaiters
    # still receive the same exception, while orphaned task warnings are
    # avoided.
    task.exception()


def _finish_scoped_discovery_singleflight(
    scope: tuple[str, ...],
    task: asyncio.Task,
) -> None:
    if _scoped_discovery_run_tasks.get(scope) is task:
        _scoped_discovery_run_tasks.pop(scope, None)
    if not task.cancelled():
        task.exception()


async def _run_discovery_once(*, platforms=None):
    selected_platforms = frozenset(
        normalize_platform_scope(
            "all" if platforms is None else platforms
        )
    )
    platform_ids = tuple(
        platform
        for platform in registered_platforms()
        if platform in selected_platforms
    )
    platform_modules = {}
    unavailable_platforms: set[str] = set()
    for platform in platform_ids:
        try:
            module = get_platform_module(platform)
        except PlatformModuleUnavailableError as exc:
            unavailable_platforms.add(platform)
            structured_log(
                "error",
                "platform_discovery_module_unavailable",
                platform=platform,
                exception=exc,
            )
            continue
        if module is not None:
            platform_modules[platform] = module
    stats = {
        "sources": 0,
        "scanned": 0,
        "found": 0,
        "inserted": 0,
        "expanded_sources": 0,
        "expired": 0,
        "failed": 0,
        "by_platform": {
            platform: _empty_platform_discovery_stats()
            for platform in platform_ids
        },
    }
    for platform in unavailable_platforms:
        stats["by_platform"][platform]["failed"] += 1
    stats["expired"] = await expire_old_lotteries(
        platforms=platform_ids
    )
    source_filter = ", ".join(
        f":source_platform_{index}"
        for index in range(len(platform_ids))
    )
    source_values = {
        f"source_platform_{index}": platform
        for index, platform in enumerate(platform_ids)
    }
    source_count = await database.fetch_one(
        f"""SELECT COUNT(*) AS total
            FROM tracked_sources
            WHERE active = 1
              AND platform IN ({source_filter})""",
        source_values,
    )
    stats["sources"] = int(source_count["total"] or 0) if source_count else 0
    # Source windows are independent database operations. Querying them
    # concurrently prevents a wedged platform/backend lane from serially
    # delaying every sibling. The query helper applies its own deadline and
    # validates historical rows and advances malformed legacy rows so they
    # cannot permanently occupy the oldest bounded query window.
    source_batches = {platform: [] for platform in platform_modules}
    query_tasks = [
        asyncio.create_task(
            _load_platform_source_batch(platform, platform_module),
            name=f"dpms-discovery-source-query-{platform}",
        )
        for platform, platform_module in platform_modules.items()
    ]
    scan_tasks: dict[str, asyncio.Task] = {}
    continuation_futures: dict[str, asyncio.Future] = {}
    eager_prefix_lengths: dict[str, int] = {}
    guaranteed_prefix = (
        DISCOVERY_ACTIVE_SOURCE_SCAN_LIMIT // max(1, len(platform_modules))
    )

    try:
        # A platform can safely start its first floor(global_limit/platforms)
        # oldest rows before the other queries finish: fair round-robin always
        # includes that prefix. Any unused capacity is assigned after every
        # bounded query has resolved, preserving the historical global budget.
        for completed_query in asyncio.as_completed(query_tasks):
            platform, rows, validation_failures = await completed_query
            stats["by_platform"][platform]["failed"] += validation_failures

            existing_task = _active_platform_discovery_task(platform)
            if existing_task is not None:
                # Public callers already share the run-level singleflight.
                # This extra platform guard protects internal/scheduler races
                # and service cancellation from starting a second same-platform
                # provider session.
                scan_tasks[platform] = existing_task
                continue

            source_batches[platform] = rows
            initial_sources = rows[:guaranteed_prefix]
            if not initial_sources:
                continue

            continuation = asyncio.get_running_loop().create_future()
            task, created = _start_platform_discovery_task(
                platform,
                initial_sources,
                platform_modules[platform],
                continuation=continuation,
            )
            scan_tasks[platform] = task
            if created:
                continuation_futures[platform] = continuation
                eager_prefix_lengths[platform] = len(initial_sources)

        selected = fair_round_robin_sources(
            source_batches,
            limit=DISCOVERY_ACTIVE_SOURCE_SCAN_LIMIT,
        )
        selected_by_platform = {platform: [] for platform in platform_modules}
        for source in selected:
            platform = str(source["platform"] or "").strip().casefold()
            if platform in selected_by_platform:
                selected_by_platform[platform].append(source)

        for platform, continuation in continuation_futures.items():
            initial_count = eager_prefix_lengths[platform]
            continuation.set_result(selected_by_platform[platform][initial_count:])

        # `guaranteed_prefix` is zero only when the global limit is smaller
        # than the number of registered platforms. Start those selected lanes
        # now that the exact fair allocation is known.
        for platform, selected_sources in selected_by_platform.items():
            if not selected_sources or platform in scan_tasks:
                continue
            task, _created = _start_platform_discovery_task(
                platform,
                selected_sources,
                platform_modules[platform],
            )
            scan_tasks[platform] = task
    finally:
        for continuation in continuation_futures.values():
            if not continuation.done():
                continuation.set_result([])
        for query_task in query_tasks:
            if not query_task.done():
                query_task.cancel()
        # Retrieving cancelled/error results prevents orphaned task warnings
        # during service shutdown or an unexpected coordinator failure.
        await asyncio.gather(*query_tasks, return_exceptions=True)

    platforms_with_tasks = list(scan_tasks)
    # Shield each shared lane: cancellation of an API/scheduler waiter must not
    # cancel provider/DB work already owned by that platform's singleflight.
    platform_results = await asyncio.gather(
        *(
            _await_shared_platform_task(scan_tasks[platform])
            for platform in platforms_with_tasks
        ),
        return_exceptions=True,
    )
    for platform, result in zip(platforms_with_tasks, platform_results):
        if isinstance(result, BaseException):
            stats["by_platform"][platform]["failed"] += 1
            structured_log(
                "error",
                "platform_discovery_scan_failed",
                platform=platform,
                exception=result,
            )
            continue
        for key, value in result.items():
            stats["by_platform"][platform][key] += value

    for platform_stats in stats["by_platform"].values():
        for key in (
            "scanned",
            "found",
            "inserted",
            "expanded_sources",
            "failed",
        ):
            stats[key] += platform_stats[key]
    return stats


async def _load_platform_source_batch(
    platform: str,
    platform_module,
) -> tuple[str, list, int]:
    """Load and validate one platform's oldest-first due-source window."""

    allowed_source_types = tuple(sorted(platform_module.discovery_source_types))
    source_type_predicate = " OR ".join(
        f"source_type = :source_type_{index}"
        for index, _source_type in enumerate(allowed_source_types)
    )
    source_query_values = {
        "platform": platform,
        "source_limit": DISCOVERY_PLATFORM_SOURCE_QUERY_LIMIT,
        **{
            f"source_type_{index}": source_type
            for index, source_type in enumerate(allowed_source_types)
        },
    }
    try:
        rows = await asyncio.wait_for(
            database.fetch_all(
                f"""SELECT id, platform, source_type, source_value, last_scan_at, scan_interval_minutes
                   FROM tracked_sources
                   WHERE active = 1
                     AND platform = :platform
                     AND ({source_type_predicate})
                     AND (
                       last_scan_at IS NULL
                       OR TIMESTAMPADD(
                            MINUTE,
                            GREATEST(COALESCE(scan_interval_minutes, 30), 1),
                            last_scan_at
                          ) < NOW()
                   )
                   ORDER BY (last_scan_at IS NULL) DESC, last_scan_at ASC, id ASC
                   LIMIT :source_limit""",
                source_query_values,
            ),
            timeout=DISCOVERY_PLATFORM_SOURCE_QUERY_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError as exc:
        structured_log(
            "error",
            "platform_discovery_source_batch_timeout",
            platform=platform,
            timeout_seconds=DISCOVERY_PLATFORM_SOURCE_QUERY_TIMEOUT_SECONDS,
            exception=exc,
        )
        return platform, [], 1
    except Exception as exc:
        structured_log(
            "error",
            "platform_discovery_source_batch_failed",
            platform=platform,
            exception=exc,
        )
        return platform, [], 1

    valid_rows = []
    invalid_rows = []
    validation_failures = 0
    # Preserve the SQL bound even for test doubles/alternate backends that
    # ignore LIMIT. Historical invalid rows are fail-closed and have their
    # scan timestamp advanced with an exact value binding below; otherwise the
    # same oldest 100 malformed rows can starve every valid row forever.
    for row in list(rows)[:DISCOVERY_PLATFORM_SOURCE_QUERY_LIMIT]:
        try:
            row_platform = str(row["platform"] or "").strip().casefold()
            if row_platform != platform:
                raise PlatformCapabilityError(
                    "platform_discovery_source_platform_mismatch",
                    platform=platform,
                    capability=row_platform or "missing",
                )
            platform_module.validate_discovery_source_config(
                str(row["source_type"] or ""),
                str(row["source_value"] or ""),
            )
        except Exception as exc:
            validation_failures += 1
            invalid_rows.append(row)
            structured_log(
                "warning",
                "platform_discovery_source_invalid",
                platform=platform,
                source_id=row["id"],
                exception=exc,
            )
            continue
        valid_rows.append(row)
    if invalid_rows:
        await _advance_invalid_discovery_sources(platform, invalid_rows)
    return platform, valid_rows, validation_failures


async def _advance_invalid_discovery_sources(platform: str, rows: list) -> None:
    """Move an exact malformed source snapshot behind the current due window.

    The source value/type predicates avoid deferring a row that an operator or
    another transaction repaired after our SELECT. One bounded UPDATE avoids
    turning 100 bad historical rows into 100 sequential database round trips.
    A database failure is logged and retried on the next run; it never makes a
    malformed row actionable.
    """

    clauses = []
    values = {"invalid_platform": platform}
    for index, row in enumerate(rows[:DISCOVERY_PLATFORM_SOURCE_QUERY_LIMIT]):
        clauses.append(
            "(id = :invalid_id_{index} "
            "AND source_type = :invalid_type_{index} "
            "AND source_value = :invalid_value_{index})".format(index=index)
        )
        values[f"invalid_id_{index}"] = row["id"]
        values[f"invalid_type_{index}"] = row["source_type"]
        values[f"invalid_value_{index}"] = row["source_value"]
    if not clauses:
        return
    try:
        await asyncio.wait_for(
            database.execute(
                """UPDATE tracked_sources
                      SET last_scan_at = NOW()
                    WHERE active = 1
                      AND platform = :invalid_platform
                      AND ("""
                + " OR ".join(clauses)
                + ")",
                values,
            ),
            timeout=DISCOVERY_PLATFORM_SOURCE_QUERY_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError as exc:
        structured_log(
            "error",
            "platform_discovery_invalid_source_advance_timeout",
            platform=platform,
            source_count=len(clauses),
            exception=exc,
        )
    except Exception as exc:
        structured_log(
            "error",
            "platform_discovery_invalid_source_advance_failed",
            platform=platform,
            source_count=len(clauses),
            exception=exc,
        )


def _active_platform_discovery_task(platform: str) -> asyncio.Task | None:
    task = _platform_discovery_tasks.get(platform)
    if task is None or task.done():
        return None
    return task


def _start_platform_discovery_task(
    platform: str,
    sources: list,
    platform_module,
    *,
    continuation: asyncio.Future | None = None,
) -> tuple[asyncio.Task, bool]:
    existing = _active_platform_discovery_task(platform)
    if existing is not None:
        return existing, False
    task = asyncio.create_task(
        _scan_platform_sources(
            platform,
            sources,
            platform_module,
            continuation=continuation,
        ),
        name=f"dpms-discovery-platform-{platform}",
    )
    _platform_discovery_tasks[platform] = task
    task.add_done_callback(
        lambda completed, platform=platform: _finish_platform_discovery_task(
            platform,
            completed,
        )
    )
    return task, True


def _finish_platform_discovery_task(
    platform: str,
    task: asyncio.Task,
) -> None:
    if _platform_discovery_tasks.get(platform) is task:
        _platform_discovery_tasks.pop(platform, None)
    if not task.cancelled():
        task.exception()


async def _await_shared_platform_task(task: asyncio.Task):
    return await asyncio.shield(task)


def _empty_platform_discovery_stats() -> dict[str, int]:
    return {
        "scheduled": 0,
        "scanned": 0,
        "found": 0,
        "inserted": 0,
        "expanded_sources": 0,
        "failed": 0,
    }


def fair_round_robin_sources(source_batches, *, limit: int) -> list:
    """Fairly fill one bounded run while preserving per-platform age order."""

    safe_limit = max(0, int(limit))
    batches = {platform: list(rows) for platform, rows in source_batches.items()}
    positions = {platform: 0 for platform in batches}
    selected = []
    while len(selected) < safe_limit:
        advanced = False
        for platform, rows in batches.items():
            position = positions[platform]
            if position >= len(rows):
                continue
            selected.append(rows[position])
            positions[platform] = position + 1
            advanced = True
            if len(selected) >= safe_limit:
                break
        if not advanced:
            break
    return selected


async def _scan_platform_sources(
    platform: str,
    sources: list,
    platform_module,
    *,
    continuation: asyncio.Future | None = None,
) -> dict[str, int]:
    """Run one bounded platform lane while preserving serial source order."""

    stats = _empty_platform_discovery_stats()
    stats["scheduled"] = len(sources)
    try:
        discovery_session = platform_module.create_discovery_session()
    except Exception as exc:
        stats["failed"] += 1
        structured_log(
            "error",
            "platform_discovery_session_create_failed",
            platform=platform,
            exception=exc,
        )
        return stats
    try:
        async def scan_lane() -> None:
            await _scan_platform_source_rows(
                platform,
                sources,
                discovery_session,
                stats,
            )
            if continuation is not None:
                # The future carries fair-share capacity left after all bounded
                # source queries settle. Shield it so an internal lane timeout
                # does not mutate the coordinator's shared allocation state.
                additional_sources = await asyncio.shield(continuation)
                stats["scheduled"] += len(additional_sources)
                await _scan_platform_source_rows(
                    platform,
                    additional_sources,
                    discovery_session,
                    stats,
                )

        await asyncio.wait_for(
            scan_lane(),
            timeout=DISCOVERY_PLATFORM_SCAN_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError as exc:
        stats["failed"] += 1
        structured_log(
            "error",
            "platform_discovery_scan_timeout",
            platform=platform,
            timeout_seconds=DISCOVERY_PLATFORM_SCAN_TIMEOUT_SECONDS,
            exception=exc,
        )
    except Exception as exc:
        # Contain an unexpected infrastructure/session error to this platform;
        # sibling platform coroutines continue independently.
        stats["failed"] += 1
        structured_log(
            "error",
            "platform_discovery_scan_failed",
            platform=platform,
            exception=exc,
        )
    finally:
        try:
            await asyncio.wait_for(
                discovery_session.finalize(),
                timeout=DISCOVERY_PLATFORM_FINALIZE_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as exc:
            stats["failed"] += 1
            structured_log(
                "error",
                "platform_discovery_finalize_timeout",
                platform=platform,
                timeout_seconds=DISCOVERY_PLATFORM_FINALIZE_TIMEOUT_SECONDS,
                exception=exc,
            )
        except Exception as exc:
            stats["failed"] += 1
            structured_log(
                "error",
                "platform_discovery_finalize_failed",
                platform=platform,
                exception=exc,
            )
    return stats


async def _scan_platform_source_rows(
    platform: str,
    sources: list,
    discovery_session: PlatformDiscoverySession,
    stats: dict[str, int],
) -> None:
    for source in sources:
        if not should_scan(source):
            continue
        if await discovery_session.should_defer(source):
            # Leave deferred keyword sources due for the next oldest-first
            # scan rather than spending their opportunity without a call.
            continue

        stats["scanned"] += 1
        structured_log(
            "info",
            "discovery_scan",
            source_id=source["id"],
            source_type=source["source_type"],
            platform=platform,
        )
        try:
            candidates = await fetch_candidates_for_source(
                source,
                discovery_session=discovery_session,
            )
            if not isinstance(candidates, list):
                raise ValueError("platform_discovery_candidates_invalid")
        except Exception as exc:
            stats["failed"] += 1
            structured_log(
                "error",
                "discovery_source_failed",
                source_id=source["id"],
                platform=platform,
                exception=exc,
            )
            await database.execute(
                "UPDATE tracked_sources SET last_scan_at = NOW() WHERE id = :id",
                {"id": source["id"]},
            )
            continue

        stats["found"] += len(candidates)
        try:
            stats[
                "expanded_sources"
            ] += await discovery_session.after_candidates(source, candidates)
        except Exception as exc:
            stats["failed"] += 1
            structured_log(
                "error",
                "platform_discovery_after_candidates_failed",
                source_id=source["id"],
                platform=platform,
                exception=exc,
            )

        for candidate in candidates:
            raw_url = None
            try:
                raw_url = candidate["raw_url"]
                if not isinstance(raw_url, str) or not raw_url.strip():
                    raise ValueError("discovery_candidate_raw_url_invalid")
                target = validate_lottery_target(source["platform"], raw_url)
                if not target.valid:
                    raise ValueError(target.reason)
                canonical = await canonicalize_url(source["platform"], raw_url)
                inserted = await insert_lottery_if_new(
                    source,
                    raw_url,
                    canonical,
                    score_lottery(source, raw_url, candidate),
                    candidate,
                )
                if inserted:
                    stats["inserted"] += 1
            except Exception as exc:
                stats["failed"] += 1
                structured_log(
                    "error",
                    "discovery_url_failed",
                    raw_url=raw_url or "",
                    platform=platform,
                    exception=exc,
                )

        await database.execute(
            "UPDATE tracked_sources SET last_scan_at = NOW() WHERE id = :id",
            {"id": source["id"]},
        )


def should_scan(source) -> bool:
    if source["last_scan_at"] is None:
        return True
    interval_minutes = max(1, int(source["scan_interval_minutes"] or 30))
    interval = timedelta(minutes=interval_minutes)
    return datetime.now() - source["last_scan_at"] > interval


async def fetch_candidates_for_source(
    source,
    *,
    keyword_search_budget: KeywordSearchCallBudget | None = None,
    discovery_session: PlatformDiscoverySession | None = None,
) -> list[dict]:
    platform = str(source["platform"] or "").strip().casefold()
    # ``databases`` rows support indexed access and ``dict(row)`` but do not
    # expose ``Mapping.get``. Platform-module discovery contracts do use
    # ``get``, so pass them a plain snapshot instead of a driver Record.
    source_snapshot = dict(source)
    platform_module = get_platform_module(platform)
    if platform_module is None:
        raise PlatformCapabilityError(
            "unsupported_platform",
            platform=platform,
            capability="discovery",
        )
    if discovery_session is not None:
        if discovery_session.platform_module.platform_id != platform:
            raise PlatformCapabilityError(
                "platform_discovery_session_mismatch",
                platform=platform,
                capability=discovery_session.platform_module.platform_id,
            )
        return await discovery_session.fetch_candidates(source_snapshot)
    return await platform_module.fetch_discovery_candidates(
        source_snapshot,
        keyword_search_budget=keyword_search_budget,
    )


async def expand_bilibili_collection_sources(
    source,
    candidates: list[dict],
    *,
    budget: ExpansionBudget | None = None,
) -> int:
    if source["platform"] != "bilibili":
        return 0

    ref_limit = BILIBILI_COLLECTION_SOURCE_LIMIT
    if budget is not None:
        ref_limit = min(ref_limit, budget.remaining)
    if ref_limit <= 0:
        return 0

    exclude_uids = {str(source["source_value"] or "").strip()}
    refs: list[dict[str, str]] = []
    seen: set[str] = set()
    for candidate in candidates:
        for ref in extract_bilibili_up_refs(candidate.get("rule_text") or "", exclude_uids=exclude_uids):
            if ref["uid"] in seen:
                continue
            refs.append(ref)
            seen.add(ref["uid"])
            if len(refs) >= ref_limit:
                break
        if len(refs) >= ref_limit:
            break

    inserted = 0
    for ref in refs:
        # Consume attempts, not just successful inserts. Otherwise a source
        # containing mostly duplicate UIDs could bypass the global DB-work
        # budget and force an unbounded number of lookups.
        if budget is not None and not budget.consume():
            break
        was_inserted = await upsert_discovered_bilibili_source(
            ref["uid"],
            source["scan_interval_minutes"] or 30,
        )
        if not was_inserted:
            continue
        inserted += 1
        structured_log(
            "info",
            "bilibili_collection_source_expanded",
            uid=ref["uid"],
            name=ref["name"],
            parent_source_id=source["id"],
            active=False,
        )
    return inserted


async def upsert_discovered_bilibili_source(uid: str, scan_interval_minutes: int) -> bool:
    """Insert one inactive candidate atomically; return whether it was new."""

    values = {
        "uid": str(uid),
        "scan_interval_minutes": max(1, int(scan_interval_minutes or 30)),
    }
    try:
        async with database.transaction():
            await database.execute(
                """INSERT INTO tracked_sources
                     (platform, source_type, source_value, scan_interval_minutes, active)
                   VALUES ('bilibili', 'up', :uid, :scan_interval_minutes, 0)
                   ON DUPLICATE KEY UPDATE id = LAST_INSERT_ID(id)""",
                values,
            )
            # ROW_COUNT is connection-local, so read it inside the transaction
            # that pins this task to the same databases connection. The no-op
            # duplicate branch reports 0; a new row reports 1.
            affected = await database.fetch_one("SELECT ROW_COUNT() AS affected")
            return bool(affected and int(affected["affected"] or 0) == 1)
    except Exception as exc:
        # Defence in depth for backends/drivers that can still surface a
        # duplicate race. Suppress only when the unique row is now provably
        # present; connection/deadlock/other write failures remain visible.
        try:
            existing = await database.fetch_one(
                """SELECT id FROM tracked_sources
                   WHERE platform = 'bilibili'
                     AND source_type = 'up'
                     AND source_value = :uid""",
                {"uid": values["uid"]},
            )
        except Exception as lookup_exc:
            raise exc from lookup_exc
        if existing:
            return False
        raise


def extract_bilibili_up_refs(text: str, *, exclude_uids: set[str] | None = None, limit: int = BILIBILI_COLLECTION_SOURCE_LIMIT) -> list[dict[str, str]]:
    exclude_uids = exclude_uids or set()
    body = truncate_collection_footer(str(text or ""))
    refs = []
    seen: set[str] = set()
    for match in BILIBILI_UP_REF_RE.finditer(body):
        name = re.sub(r"\s+", " ", match.group("name")).strip()
        uid = match.group("uid").strip()
        if uid in exclude_uids or uid in seen:
            continue
        if not likely_bilibili_lottery_source_name(name):
            continue
        refs.append({"name": name, "uid": uid})
        seen.add(uid)
        if len(refs) >= limit:
            break
    return refs


def truncate_collection_footer(text: str) -> str:
    cutoff = len(text)
    for marker in BILIBILI_COLLECTION_TRUNCATE_MARKERS:
        idx = text.find(marker)
        if idx >= 0:
            cutoff = min(cutoff, idx)
    return text[:cutoff]


def likely_bilibili_lottery_source_name(name: str) -> bool:
    blocked = ("\u4f1a\u5458", "\u5145\u7535", "\u7c89\u4e1d", "\u540d\u5355")
    if any(part in name for part in blocked):
        return False
    return bool(re.search(r"[\u4e00-\u9fffA-Za-z0-9]", name))


async def fetch_up_dynamics(up_uid: str):
    cookie_header = await load_bilibili_discovery_cookie_header()
    items = await fetch_bilibili_space_dynamics(up_uid, cookie_header=cookie_header)
    return [
        {
            "raw_url": item.url,
            "title": item.title,
            "rule_text": item.rule_text,
            "published_at": item.published_at,
            "action_plan": item.action_plan,
        }
        for item in items
    ]


async def fetch_keyword_dynamics(
    source_value: str,
    *,
    search_budget: KeywordSearchCallBudget | None = None,
) -> list[dict]:
    keywords = split_keywords(source_value)
    if not keywords or (search_budget is not None and search_budget.remaining <= 0):
        return []

    cookie_header = await try_load_bilibili_discovery_cookie_header()
    candidates: dict[str, dict] = {}
    for keyword in keywords:
        # Charge attempts before the remote call. A failing or rejected search
        # must not let an untrusted source bypass the per-run call budget.
        if search_budget is not None and not search_budget.consume():
            break
        items = await fetch_bilibili_keyword_search(
            keyword,
            pages=BILIBILI_KEYWORD_SEARCH_PAGES_PER_CALL,
            limit=BILIBILI_KEYWORD_SEARCH_RESULT_LIMIT,
            cookie_header=cookie_header,
        )
        # The provider helper currently returns at most `limit` rows, but keep
        # a local bound so a future helper change cannot amplify this caller.
        for item in islice(items, BILIBILI_KEYWORD_SEARCH_RESULT_LIMIT):
            candidate = {
                "raw_url": item.url,
                "title": item.title,
                "rule_text": item.rule_text,
                "published_at": item.published_at,
                "action_plan": item.action_plan,
            }
            candidates.setdefault(candidate["raw_url"], candidate)
            if len(candidates) >= BILIBILI_KEYWORD_SOURCE_CANDIDATE_LIMIT:
                return list(candidates.values())
    return list(candidates.values())


def split_keywords(value: str) -> list[str]:
    # Compatibility facade; the Bilibili platform module owns query syntax and
    # limits so changes cannot alter another platform's discovery contract.
    return split_bilibili_keywords(value)


async def try_load_bilibili_discovery_cookie_header() -> str | None:
    try:
        return await load_bilibili_discovery_cookie_header()
    except Exception as exc:
        structured_log("warning", "bilibili_keyword_search_without_cookie", exception=exc)
        return None


async def load_bilibili_discovery_cookie_header() -> str:
    row = await database.fetch_one(
        """SELECT encrypted_credential
           FROM accounts
           WHERE platform = 'bilibili'
             AND status = 'ready'
             AND OCTET_LENGTH(encrypted_credential) > 0
           ORDER BY last_active_at DESC, id ASC
           LIMIT 1"""
    )
    if not row:
        raise RuntimeError("Bilibili UP discovery requires a calibrated ready account")
    credential = cookie_vault.decrypt(row["encrypted_credential"], aad=CREDENTIAL_AAD)
    cookies = parse_cookie_payload("bilibili", credential)
    return "; ".join(f"{cookie['name']}={cookie['value']}" for cookie in cookies)


def extract_urls(value: str) -> list[str]:
    return extract_discovery_urls(value)


async def canonicalize_url(platform: str, raw_url: str) -> str:
    return await canonicalize_platform_url(platform, raw_url)


def score_lottery(source, raw_url: str, candidate: dict | None = None) -> int:
    candidate = candidate or {}
    text = unquote(f"{source['source_value']} {raw_url} {candidate.get('rule_text', '')}").lower()
    score = 20
    if source["source_type"] == "url_list":
        score += 10
    if any(keyword.lower() in text for keyword in LOTTERY_KEYWORDS):
        score += 50
    if "?" in raw_url:
        score += 5
    platform_module = get_platform_module(str(source["platform"] or ""))
    if platform_module:
        score += platform_module.discovery_score_bonus
    action_plan = candidate.get("action_plan") or {}
    if action_plan.get("is_lottery"):
        score += 10
    if action_plan.get("review_required"):
        score -= 15
    return min(score, 100)


def prepare_discovery_rule_refresh(
    existing_rule_text,
    existing_action_plan,
    candidate_rule_text,
    candidate_action_plan,
) -> tuple[str | None, dict | None]:
    """Return the rule/action-plan fields discovery is allowed to update.

    A reviewed action plan is only valid for the exact rule text it was
    reviewed against.  Discovery may populate an empty legacy record, but a
    later source-text change must invalidate the old review instead of
    silently carrying it forward.
    """
    incoming_rule = str(candidate_rule_text or "")
    if not incoming_rule.strip():
        return None, None

    current_rule = str(existing_rule_text or "")
    incoming_plan = discovery_draft_action_plan(candidate_action_plan)

    if not current_rule.strip():
        return incoming_rule, incoming_plan or None

    if incoming_rule == current_rule:
        if _has_meaningful_action_plan(existing_action_plan):
            return None, None
        return None, incoming_plan or None

    invalidated_plan = incoming_plan
    invalidated_plan["review_required"] = True
    invalidated_plan["source"] = "discovery_rule_changed"
    return incoming_rule, invalidated_plan


def discovery_draft_action_plan(value) -> dict:
    """Discovery output is a suggestion, never an execution authorisation."""

    plan = dict(value) if isinstance(value, dict) else {}
    plan["review_required"] = True
    plan["executable"] = False
    plan["source"] = "discovery_unattested"
    # A provider must not be able to smuggle old authority/hash bindings into
    # a new candidate. Core creates those only after an operator attestation.
    for field in (
        "rule_snapshot_id",
        "rule_hash",
        "plan_hash",
        "reviewed_by",
        "rule_complete_confirmed",
    ):
        plan.pop(field, None)
    return plan


def _has_meaningful_action_plan(value) -> bool:
    if isinstance(value, dict):
        return bool(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return False
        try:
            decoded = json.loads(text)
        except (TypeError, ValueError):
            # Preserve unknown non-empty data rather than overwriting it during
            # an otherwise unchanged discovery refresh.
            return True
        return bool(decoded) if isinstance(decoded, dict) else True
    return value is not None


async def expire_old_lotteries(*, platforms=None) -> int:
    selected_platforms = normalize_platform_scope(
        "all" if platforms is None else platforms
    )
    platform_filter = ", ".join(
        f":expire_platform_{index}"
        for index in range(len(selected_platforms))
    )
    values = {
        f"expire_platform_{index}": platform
        for index, platform in enumerate(selected_platforms)
    }
    result = await execute_affected_rows(
        f"""UPDATE lotteries
           SET status = 'expired'
           WHERE status = 'pending'
              AND platform IN ({platform_filter})
              AND execution_lock IS NULL
              AND expires_at IS NOT NULL
              AND expires_at < NOW()""",
        values,
        db=database,
    )
    return int(result or 0)


async def insert_lottery_if_new(
    source,
    raw_url: str,
    canonical_url: str,
    value_score: int,
    candidate: dict | None = None,
) -> bool:
    candidate = candidate or {}
    insert_attempted = False
    try:
        if len(raw_url) > LOTTERY_RAW_URL_MAX_LENGTH:
            raise ValueError("lottery_raw_url_too_long")
        if len(canonical_url) > LOTTERY_RAW_URL_MAX_LENGTH:
            raise ValueError("lottery_canonical_url_too_long")
        source_locator_id = tracked_source_lottery_source_id(source)
        async with database.transaction():
            existing = await database.fetch_one(
                """SELECT l.id, l.rule_text, l.action_plan, l.status,
                          l.execution_lock,
                          EXISTS (
                            SELECT 1
                            FROM lottery_execution_intents intent_root
                            WHERE intent_root.lottery_id = l.id
                          ) AS has_execution_intent
                   FROM lotteries AS l
                   WHERE l.url_hash = SHA2(:canonical_url, 256)
                     AND l.canonical_url = :canonical_url
                   FOR UPDATE""",
                {"canonical_url": canonical_url},
            )
            if existing:
                active_execution = bool(str(existing["execution_lock"] or "").strip()) or str(
                    existing["status"] or ""
                ).strip().lower() in {"claimed", "running"}
                # Discovery is an untrusted observation source, not an
                # authority that may supersede an immutable execution intent.
                # Once any full intent exists, even a zero-effect generation
                # must be reviewed explicitly before its mutable source rule
                # or plan changes. This also preserves the exact frozen
                # binding needed to Repair partially confirmed effects.
                intent_frozen = bool(
                    int(dict(existing).get("has_execution_intent") or 0)
                )
                if active_execution or intent_frozen:
                    # Discovery must not change the authoritative rule or plan
                    # beneath a claimed task or immutable intent. Metadata can
                    # still be refreshed; an operator can explicitly review a
                    # new generation when the completion authority is settled.
                    rule_text_update, action_plan_update = None, None
                    if candidate.get("rule_text") or candidate.get("action_plan"):
                        structured_log(
                            "warning",
                            (
                                "discovery_rule_refresh_deferred_active_execution"
                                if active_execution
                                else "discovery_rule_refresh_deferred_frozen_intent"
                            ),
                            lottery_id=existing["id"],
                            canonical_url=canonical_url,
                        )
                else:
                    rule_text_update, action_plan_update = prepare_discovery_rule_refresh(
                        existing["rule_text"],
                        existing["action_plan"],
                        candidate.get("rule_text"),
                        candidate.get("action_plan"),
                    )
                action_plan_json = (
                    json.dumps(action_plan_update, ensure_ascii=False)
                    if action_plan_update
                    else None
                )
                rule_changed = bool(
                    rule_text_update is not None
                    and str(rule_text_update) != str(existing["rule_text"] or "")
                )
                action_plan_changed = action_plan_update is not None
                await database.execute(
                    """UPDATE lotteries
                       SET title = COALESCE(:title, title),
                           rule_text = COALESCE(:rule_text, rule_text),
                           action_plan = COALESCE(:action_plan, action_plan),
                           authoritative_rule_snapshot_id = IF(
                             :rule_changed = 1, NULL, authoritative_rule_snapshot_id
                           ),
                           rule_hash = IF(:rule_changed = 1, NULL, rule_hash),
                           action_plan_hash = IF(
                             :action_plan_changed = 1, NULL, action_plan_hash
                           ),
                           published_at = COALESCE(:published_at, published_at),
                           value_score = GREATEST(value_score, :value_score)
                       WHERE id = :id""",
                    {
                        "id": existing["id"],
                        "title": candidate.get("title"),
                        "rule_text": rule_text_update,
                        "action_plan": action_plan_json,
                        "rule_changed": int(rule_changed),
                        "action_plan_changed": int(action_plan_changed),
                        "published_at": candidate.get("published_at"),
                        "value_score": value_score,
                    },
                )
                observed_rule = str(candidate.get("rule_text") or "")
                if observed_rule.strip():
                    await ensure_rule_snapshot(
                        {
                            "id": existing["id"],
                            "platform": source["platform"],
                            "source_type": source["source_type"],
                            "source_id": source_locator_id,
                            "raw_url": raw_url,
                            "canonical_url": canonical_url,
                        },
                        observed_rule,
                        complete=False,
                        source_kind=source["source_type"],
                        source_locator=raw_url,
                        fetch_method=f"discovery_{source['source_type']}",
                        allow_existing_complete=False,
                        db=database,
                    )
                return False

            discovery_plan = discovery_draft_action_plan(candidate.get("action_plan"))
            action_plan_json = (
                json.dumps(discovery_plan, ensure_ascii=False)
                if candidate.get("action_plan") or candidate.get("rule_text")
                else None
            )
            insert_attempted = True
            inserted_id = await database.execute(
                """INSERT INTO lotteries
                   (platform, source_type, source_id, raw_url, canonical_url, title, rule_text, action_plan,
                    published_at, value_score, expires_at, status)
                   VALUES
                   (:platform, :source_type, :source_id, :raw_url, :canonical_url, :title, :rule_text, :action_plan,
                    :published_at, :value_score, :expires_at, 'pending')""",
                {
                    "platform": source["platform"],
                    "source_type": source["source_type"],
                    "source_id": source_locator_id,
                    "raw_url": raw_url,
                    "canonical_url": canonical_url,
                    "title": candidate.get("title"),
                    "rule_text": candidate.get("rule_text"),
                    "action_plan": action_plan_json,
                    "published_at": candidate.get("published_at"),
                    "value_score": value_score,
                    "expires_at": datetime.now() + timedelta(days=DEFAULT_EXPIRES_DAYS),
                },
            )
            try:
                lottery_id = int(inserted_id)
            except (TypeError, ValueError):
                inserted_row = await database.fetch_one(
                    """SELECT id FROM lotteries
                       WHERE url_hash = SHA2(:canonical_url, 256)
                         AND canonical_url = :canonical_url""",
                    {"canonical_url": canonical_url},
                )
                lottery_id = int(inserted_row["id"]) if inserted_row else 0
            observed_rule = str(candidate.get("rule_text") or "")
            if lottery_id <= 0:
                raise RuntimeError("lottery_insert_returned_no_id")
            if observed_rule.strip():
                await ensure_rule_snapshot(
                    {
                        "id": lottery_id,
                        "platform": source["platform"],
                        "source_type": source["source_type"],
                        "source_id": source_locator_id,
                        "raw_url": raw_url,
                        "canonical_url": canonical_url,
                    },
                    observed_rule,
                    complete=False,
                    source_kind=source["source_type"],
                    source_locator=raw_url,
                    fetch_method=f"discovery_{source['source_type']}",
                    allow_existing_complete=False,
                    db=database,
                )
            return True
    except Exception as e:
        structured_log("error", "insert_lottery_failed", raw_url=raw_url, exception=e)
        if insert_attempted:
            # A concurrent discovery transaction may have won the unique
            # canonical-URL race after our FOR UPDATE lookup found no row.
            # Only collapse the exception into "already exists" when that row
            # can now be proven; deadlocks, connection errors and failed
            # updates must remain visible to the caller and discovery stats.
            try:
                existing = await database.fetch_one(
                    """SELECT id FROM lotteries
                       WHERE url_hash = SHA2(:canonical_url, 256)
                         AND canonical_url = :canonical_url""",
                    {"canonical_url": canonical_url},
                )
            except Exception as lookup_exc:
                raise e from lookup_exc
            if existing:
                return False
        raise


def tracked_source_lottery_source_id(source) -> str:
    """Fit discovery provenance into lotteries.source_id without truncation.

    Existing short identifiers (UP ids, keywords, and short URL lists) retain
    their historical value. Longer URL-list locators use the stable tracked
    source primary key; the exact candidate URL remains in raw_url and rule
    snapshot source_locator.
    """

    source_value = str(source["source_value"] or "").strip()
    if len(source_value) <= LOTTERY_SOURCE_ID_MAX_LENGTH:
        return source_value
    source_pk = int(source["id"])
    if source_pk <= 0:
        raise ValueError("tracked_source_id_invalid")
    locator = f"tracked_source:{source_pk}"
    if len(locator) > LOTTERY_SOURCE_ID_MAX_LENGTH:
        raise ValueError("tracked_source_locator_too_long")
    return locator
