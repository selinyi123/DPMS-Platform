import re
from datetime import datetime, timedelta
from urllib.parse import unquote

from app.db import database
from app.utils.canonicalizer import BilibiliCanonicalizer, GenericCanonicalizer
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


async def run_discovery():
    stats = {"sources": 0, "scanned": 0, "found": 0, "inserted": 0, "expired": 0, "failed": 0}
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
        urls = await fetch_urls_for_source(source)
        stats["found"] += len(urls)

        for raw_url in urls:
            try:
                canonical = await canonicalize_url(source["platform"], raw_url)
                inserted = await insert_lottery_if_new(source, raw_url, canonical, score_lottery(source, raw_url))
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


async def fetch_urls_for_source(source) -> list[str]:
    if source["source_type"] == "url_list":
        return extract_urls(source["source_value"])
    if source["platform"] == "bilibili" and source["source_type"] == "up":
        return await fetch_up_dynamics(source["source_value"])
    return []


async def fetch_up_dynamics(up_uid: str):
    structured_log("warning", "bilibili_up_discovery_not_configured", source_id=up_uid)
    return []


def extract_urls(value: str) -> list[str]:
    urls = re.findall(r"https?://[^\s,\uFF0C]+", value or "")
    return list(dict.fromkeys(url.strip() for url in urls))


async def canonicalize_url(platform: str, raw_url: str) -> str:
    if platform == "bilibili":
        try:
            return (await BilibiliCanonicalizer.canonicalize(raw_url)).to_uri()
        except Exception:
            pass
    return await GenericCanonicalizer.canonicalize(platform, raw_url)


def score_lottery(source, raw_url: str) -> int:
    text = unquote(f"{source['source_value']} {raw_url}").lower()
    score = 20
    if source["source_type"] == "url_list":
        score += 10
    if any(keyword.lower() in text for keyword in LOTTERY_KEYWORDS):
        score += 50
    if "?" in raw_url:
        score += 5
    if source["platform"] == "bilibili":
        score += 5
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


async def insert_lottery_if_new(source, raw_url: str, canonical_url: str, value_score: int) -> bool:
    try:
        existing = await database.fetch_one(
            "SELECT id FROM lotteries WHERE canonical_url = :canonical_url",
            {"canonical_url": canonical_url},
        )
        if existing:
            return False

        await database.execute(
            """INSERT INTO lotteries
               (platform, source_type, source_id, raw_url, canonical_url, value_score, expires_at, status)
               VALUES
               (:platform, :source_type, :source_id, :raw_url, :canonical_url, :value_score, :expires_at, 'pending')""",
            {
                "platform": source["platform"],
                "source_type": source["source_type"],
                "source_id": source["source_value"],
                "raw_url": raw_url,
                "canonical_url": canonical_url,
                "value_score": value_score,
                "expires_at": datetime.now() + timedelta(days=DEFAULT_EXPIRES_DAYS),
            },
        )
        return True
    except Exception as e:
        structured_log("error", "insert_lottery_failed", raw_url=raw_url, exception=e)
        return False
