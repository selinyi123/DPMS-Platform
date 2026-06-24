import re
import json
from datetime import datetime, timedelta
from urllib.parse import unquote

from app.db import database
from app.services.bilibili_discovery import fetch_bilibili_space_dynamics
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
BILIBILI_COLLECTION_SOURCE_LIMIT = 80
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


async def run_discovery():
    stats = {"sources": 0, "scanned": 0, "found": 0, "inserted": 0, "expanded_sources": 0, "expired": 0, "failed": 0}
    stats["expired"] = await expire_old_lotteries()
    sources = await database.fetch_all(
        """SELECT id, platform, source_type, source_value, last_scan_at, scan_interval_minutes
           FROM tracked_sources WHERE active = 1"""
    )

    for source in sources:
        stats["sources"] += 1
        if not should_scan(source):
            continue

        stats["scanned"] += 1
        structured_log("info", "discovery_scan", source_id=source["id"], source_type=source["source_type"])
        try:
            candidates = await fetch_candidates_for_source(source)
        except Exception as e:
            stats["failed"] += 1
            structured_log("error", "discovery_source_failed", source_id=source["id"], exception=e)
            await database.execute("UPDATE tracked_sources SET last_scan_at = NOW() WHERE id = :id", {"id": source["id"]})
            continue
        stats["found"] += len(candidates)
        stats["expanded_sources"] += await expand_bilibili_collection_sources(source, candidates)

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

    return stats


def should_scan(source) -> bool:
    if source["last_scan_at"] is None:
        return True
    interval = timedelta(minutes=source["scan_interval_minutes"])
    return datetime.now() - source["last_scan_at"] > interval


async def fetch_candidates_for_source(source) -> list[dict]:
    if source["source_type"] == "url_list":
        return [{"raw_url": url} for url in extract_urls(source["source_value"])]
    if source["platform"] == "bilibili" and source["source_type"] == "up":
        return await fetch_up_dynamics(source["source_value"])
    return []


async def expand_bilibili_collection_sources(source, candidates: list[dict]) -> int:
    if source["platform"] != "bilibili":
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
            if len(refs) >= BILIBILI_COLLECTION_SOURCE_LIMIT:
                break
        if len(refs) >= BILIBILI_COLLECTION_SOURCE_LIMIT:
            break

    inserted = 0
    for ref in refs:
        existing = await database.fetch_one(
            """SELECT id FROM tracked_sources
               WHERE platform = 'bilibili' AND source_type = 'up' AND source_value = :uid""",
            {"uid": ref["uid"]},
        )
        if existing:
            continue
        await database.execute(
            """INSERT INTO tracked_sources (platform, source_type, source_value, scan_interval_minutes, active)
               VALUES ('bilibili', 'up', :uid, :scan_interval_minutes, 1)""",
            {"uid": ref["uid"], "scan_interval_minutes": source["scan_interval_minutes"] or 30},
        )
        inserted += 1
        structured_log("info", "bilibili_collection_source_expanded", uid=ref["uid"], name=ref["name"], parent_source_id=source["id"])
    return inserted


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


async def expire_old_lotteries() -> int:
    result = await database.execute(
        """UPDATE lotteries
           SET status = 'expired'
           WHERE status IN ('pending', 'claimed')
             AND expires_at IS NOT NULL
             AND expires_at < NOW()"""
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
    action_plan_json = (
        json.dumps(candidate.get("action_plan"), ensure_ascii=False)
        if candidate.get("action_plan")
        else None
    )
    try:
        existing = await database.fetch_one(
            "SELECT id FROM lotteries WHERE canonical_url = :canonical_url",
            {"canonical_url": canonical_url},
        )
        if existing:
            await database.execute(
                """UPDATE lotteries
                   SET title = COALESCE(:title, title),
                       rule_text = COALESCE(:rule_text, rule_text),
                       action_plan = COALESCE(:action_plan, action_plan),
                       published_at = COALESCE(:published_at, published_at),
                       value_score = GREATEST(value_score, :value_score)
                   WHERE id = :id""",
                {
                    "id": existing["id"],
                    "title": candidate.get("title"),
                    "rule_text": candidate.get("rule_text"),
                    "action_plan": action_plan_json,
                    "published_at": candidate.get("published_at"),
                    "value_score": value_score,
                },
            )
            return False

        await database.execute(
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
        return True
    except Exception as e:
        structured_log("error", "insert_lottery_failed", raw_url=raw_url, exception=e)
        return False
