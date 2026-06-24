from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.services.lottery_rules import parse_lottery_rule, repair_mojibake


BILIBILI_SPACE_FEED_URL = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
REQUEST_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"
    ),
}


@dataclass(frozen=True)
class BilibiliDynamicCandidate:
    dynamic_id: str
    url: str
    title: str
    rule_text: str
    published_at: datetime | None
    action_plan: dict[str, Any]


async def fetch_bilibili_space_dynamics(
    up_uid: str,
    limit: int = 20,
    cookie_header: str | None = None,
) -> list[BilibiliDynamicCandidate]:
    import httpx

    uid = str(up_uid or "").strip()
    if not uid.isdigit():
        raise ValueError("Bilibili UP UID must be numeric")

    params = {"host_mid": uid, "offset": "", "timezone_offset": "-480", "web_location": "333.999"}
    headers = {
        **REQUEST_HEADERS,
        "Referer": f"https://space.bilibili.com/{uid}/dynamic",
        "Origin": "https://space.bilibili.com",
    }
    if cookie_header:
        headers["Cookie"] = cookie_header
    timeout = httpx.Timeout(10.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
        response = await client.get(BILIBILI_SPACE_FEED_URL, params=params)
        response.raise_for_status()
        payload = response.json()

    if payload.get("code") != 0:
        raise RuntimeError(f"Bilibili dynamic feed rejected request: {payload.get('message') or payload.get('code')}")

    items = ((payload.get("data") or {}).get("items") or [])[: max(1, min(int(limit), 50))]
    candidates = []
    for item in items:
        candidate = parse_dynamic_item(item)
        if candidate and candidate.action_plan["is_lottery"]:
            candidates.append(candidate)
    return candidates


def parse_dynamic_item(item: dict[str, Any]) -> BilibiliDynamicCandidate | None:
    dynamic_id = str(item.get("id_str") or item.get("id") or "").strip()
    if not dynamic_id.isdigit():
        return None

    modules = item.get("modules") or {}
    dynamic = modules.get("module_dynamic") or {}
    author = modules.get("module_author") or {}
    rule_text = extract_dynamic_text(dynamic)
    if not rule_text:
        return None

    action_plan = parse_lottery_rule(rule_text, "bilibili")
    title = first_nonempty_line(rule_text)[:160]
    published_at = timestamp_to_datetime(author.get("pub_ts"))
    return BilibiliDynamicCandidate(
        dynamic_id=dynamic_id,
        url=f"https://t.bilibili.com/{dynamic_id}",
        title=title,
        rule_text=rule_text,
        published_at=published_at,
        action_plan=action_plan,
    )


def extract_dynamic_text(dynamic: dict[str, Any]) -> str:
    chunks: list[str] = []
    desc = dynamic.get("desc") or {}
    append_text(chunks, desc.get("text"))

    major = dynamic.get("major") or {}
    for key in ("opus", "archive", "article", "draw", "common"):
        block = major.get(key) or {}
        append_text(chunks, block.get("title"))
        append_text(chunks, block.get("desc"))
        append_text(chunks, block.get("summary", {}).get("text") if isinstance(block.get("summary"), dict) else None)

    additional = dynamic.get("additional") or {}
    for block in additional.values():
        if isinstance(block, dict):
            append_text(chunks, block.get("title"))
            append_text(chunks, block.get("desc_first"))
            append_text(chunks, block.get("desc_second"))

    return "\n".join(dict.fromkeys(chunk for chunk in chunks if chunk)).strip()


def append_text(chunks: list[str], value: Any) -> None:
    text = repair_mojibake(str(value or "")).strip()
    if text:
        chunks.append(text)


def first_nonempty_line(value: str) -> str:
    for line in str(value or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def timestamp_to_datetime(value: Any) -> datetime | None:
    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(timestamp) if timestamp > 0 else None
