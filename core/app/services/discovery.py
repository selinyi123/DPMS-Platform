import asyncio
import re
import json
from datetime import datetime, timedelta
from itertools import islice
from urllib.parse import unquote

from app.db import database, execute_affected_rows
from app.services.bilibili_discovery import fetch_bilibili_keyword_search, fetch_bilibili_space_dynamics
from app.services.rule_provenance import ensure_rule_snapshot
from app.utils.cookies import parse_cookie_payload
from app.utils.crypto import CREDENTIAL_AAD, cookie_vault
from app.utils.canonicalizer import canonicalize_platform_url
from app.utils.lottery_targets import validate_lottery_target
from app.utils.log import structured_log


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
BILIBILI_COLLECTION_SOURCE_LIMIT = 80
BILIBILI_COLLECTION_RUN_BUDGET = 80
BILIBILI_KEYWORD_SOURCE_QUERY_LIMIT = 8
BILIBILI_KEYWORD_QUERY_MAX_CHARS = 64
BILIBILI_KEYWORD_SEARCH_PAGES_PER_CALL = 2
BILIBILI_KEYWORD_SEARCH_RESULT_LIMIT = 30
BILIBILI_KEYWORD_SOURCE_CANDIDATE_LIMIT = 80
BILIBILI_KEYWORD_SEARCH_CALL_RUN_BUDGET = 40
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


class AttemptBudget:
    """A counter that bounds attempted work, including failed attempts."""

    def __init__(self, limit: int):
        self.limit = max(0, int(limit))
        self.consumed = 0

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.consumed)

    def consume(self) -> bool:
        if self.remaining <= 0:
            return False
        self.consumed += 1
        return True


class ExpansionBudget(AttemptBudget):
    """A per-discovery-run budget for untrusted source expansion work."""


class KeywordSearchCallBudget(AttemptBudget):
    """A per-discovery-run budget for remote keyword-search calls."""


_discovery_run_task: asyncio.Task | None = None


async def run_discovery():
    """Coalesce concurrent in-process scans onto one authoritative run."""

    global _discovery_run_task
    task = _discovery_run_task
    if task is None or task.done():
        # There is no await between observing and publishing the task, so this
        # assignment is atomic for the single asyncio loop used by uvicorn.
        task = asyncio.create_task(_run_discovery_once(), name="dpms-discovery-scan")
        _discovery_run_task = task
        task.add_done_callback(_finish_discovery_singleflight)

    # A cancelled HTTP caller must not cancel the shared scheduler/API scan.
    # Return a copy so one caller cannot mutate another caller's result.
    return dict(await asyncio.shield(task))


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


async def _run_discovery_once():
    stats = {"sources": 0, "scanned": 0, "found": 0, "inserted": 0, "expanded_sources": 0, "expired": 0, "failed": 0}
    stats["expired"] = await expire_old_lotteries()
    source_count = await database.fetch_one(
        "SELECT COUNT(*) AS total FROM tracked_sources WHERE active = 1"
    )
    stats["sources"] = int(source_count["total"] or 0) if source_count else 0
    sources = await database.fetch_all(
        """SELECT id, platform, source_type, source_value, last_scan_at, scan_interval_minutes
           FROM tracked_sources
           WHERE active = 1
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
        {"source_limit": DISCOVERY_ACTIVE_SOURCE_SCAN_LIMIT},
    )
    # Keep the hard safety bound even when a test double or alternate database
    # backend does not enforce the SQL LIMIT as expected.
    sources = list(sources)[:DISCOVERY_ACTIVE_SOURCE_SCAN_LIMIT]
    expansion_budget = ExpansionBudget(BILIBILI_COLLECTION_RUN_BUDGET)
    keyword_search_budget = KeywordSearchCallBudget(BILIBILI_KEYWORD_SEARCH_CALL_RUN_BUDGET)

    for source in sources:
        if not should_scan(source):
            continue
        if (
            source["platform"] == "bilibili"
            and source["source_type"] == "keyword"
            and keyword_search_budget.remaining <= 0
        ):
            # Leave deferred keyword sources due for the next oldest-first
            # scan. Marking them scanned without issuing a search would let
            # early rows consume every future budget and starve later rows.
            continue

        stats["scanned"] += 1
        structured_log("info", "discovery_scan", source_id=source["id"], source_type=source["source_type"])
        try:
            candidates = await fetch_candidates_for_source(
                source,
                keyword_search_budget=keyword_search_budget,
            )
        except Exception as e:
            stats["failed"] += 1
            structured_log("error", "discovery_source_failed", source_id=source["id"], exception=e)
            await database.execute("UPDATE tracked_sources SET last_scan_at = NOW() WHERE id = :id", {"id": source["id"]})
            continue
        stats["found"] += len(candidates)
        try:
            stats["expanded_sources"] += await expand_bilibili_collection_sources(
                source,
                candidates,
                budget=expansion_budget,
            )
        except Exception as e:
            # Source expansion is auxiliary discovery work. A database or
            # parsing failure here must not discard valid candidates or abort
            # every remaining source in the bounded scan.
            stats["failed"] += 1
            structured_log(
                "error",
                "bilibili_collection_source_expansion_failed",
                source_id=source["id"],
                exception=e,
            )

        for candidate in candidates:
            raw_url = candidate["raw_url"]
            try:
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
            except Exception as e:
                stats["failed"] += 1
                structured_log("error", "discovery_url_failed", raw_url=raw_url, exception=e)

        await database.execute("UPDATE tracked_sources SET last_scan_at = NOW() WHERE id = :id", {"id": source["id"]})

    if expansion_budget.remaining == 0:
        structured_log(
            "warning",
            "bilibili_collection_expansion_budget_exhausted",
            budget=BILIBILI_COLLECTION_RUN_BUDGET,
        )
    if keyword_search_budget.remaining == 0:
        structured_log(
            "warning",
            "bilibili_keyword_search_call_budget_exhausted",
            budget=BILIBILI_KEYWORD_SEARCH_CALL_RUN_BUDGET,
        )

    return stats


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
) -> list[dict]:
    if source["source_type"] == "url_list":
        return [{"raw_url": url} for url in extract_urls(source["source_value"])]
    if source["platform"] == "bilibili" and source["source_type"] == "up":
        return await fetch_up_dynamics(source["source_value"])
    if source["platform"] == "bilibili" and source["source_type"] == "keyword":
        return await fetch_keyword_dynamics(
            source["source_value"],
            search_budget=keyword_search_budget,
        )
    return []


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
    parts = re.split(r"[\n,，;；]+", str(value or ""))
    keywords: list[str] = []
    seen: set[str] = set()
    for part in parts:
        keyword = part.strip()
        # Do not silently truncate: that could broaden or otherwise change the
        # operator's query. Oversized values are rejected before any request.
        if not keyword or len(keyword) > BILIBILI_KEYWORD_QUERY_MAX_CHARS:
            continue
        dedupe_key = keyword.casefold()
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        keywords.append(keyword)
        if len(keywords) >= BILIBILI_KEYWORD_SOURCE_QUERY_LIMIT:
            break
    return keywords


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
    urls = re.findall(r"https?://[^\s,\uFF0C]+", value or "")
    return list(dict.fromkeys(url.strip() for url in urls))


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
    if source["platform"] == "bilibili":
        score += 5
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


async def expire_old_lotteries() -> int:
    result = await execute_affected_rows(
        """UPDATE lotteries
           SET status = 'expired'
           WHERE status = 'pending'
             AND execution_lock IS NULL
             AND expires_at IS NOT NULL
             AND expires_at < NOW()""",
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
        async with database.transaction():
            existing = await database.fetch_one(
                """SELECT id, rule_text, action_plan, status, execution_lock
                   FROM lotteries
                   WHERE url_hash = SHA2(:canonical_url, 256)
                     AND canonical_url = :canonical_url
                   FOR UPDATE""",
                {"canonical_url": canonical_url},
            )
            if existing:
                active_execution = bool(str(existing["execution_lock"] or "").strip()) or str(
                    existing["status"] or ""
                ).strip().lower() in {"claimed", "running"}
                if active_execution:
                    # Discovery must not change the authoritative rule or plan
                    # beneath a claimed task. Metadata can still be refreshed;
                    # a later discovery pass will revisit the deferred rule.
                    rule_text_update, action_plan_update = None, None
                    if candidate.get("rule_text") or candidate.get("action_plan"):
                        structured_log(
                            "warning",
                            "discovery_rule_refresh_deferred_active_execution",
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
                            "source_id": source["source_value"],
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
                    "source_id": source["source_value"],
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
                        "source_id": source["source_value"],
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
