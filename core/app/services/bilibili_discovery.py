from dataclasses import dataclass
from datetime import datetime
import hashlib
import re
import time
from typing import Any
import urllib.parse

from app.services.lottery_rules import parse_lottery_rule, repair_mojibake
from app.utils.lottery_targets import validate_lottery_target


BILIBILI_SPACE_FEED_URL = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
BILIBILI_NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
BILIBILI_SEARCH_TYPE_URL = "https://api.bilibili.com/x/web-interface/search/type"
REQUEST_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"
    ),
}
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]
WBI_FILTER_CHARS = "!'()*"
SEARCH_TEXT_FIELDS = (
    "title",
    "description",
    "desc",
    "content",
    "name",
    "author",
    "uname",
    "typename",
)


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


async def fetch_bilibili_keyword_search(
    keyword: str,
    *,
    pages: int = 2,
    limit: int = 30,
    cookie_header: str | None = None,
) -> list[BilibiliDynamicCandidate]:
    import httpx

    query = str(keyword or "").strip()
    if not query:
        return []

    headers = {
        **REQUEST_HEADERS,
        "Referer": "https://search.bilibili.com/",
        "Origin": "https://search.bilibili.com",
    }
    if cookie_header:
        headers["Cookie"] = cookie_header
    timeout = httpx.Timeout(12.0, connect=5.0)
    candidates: list[BilibiliDynamicCandidate] = []
    seen: set[str] = set()
    async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
        img_key, sub_key = await fetch_wbi_keys(client)
        for page in range(1, max(1, min(int(pages), 5)) + 1):
            params = sign_wbi_params(
                {
                    "keyword": query,
                    "search_type": "dynamic",
                    "page": page,
                    "order": "pubdate",
                },
                img_key,
                sub_key,
            )
            response = await client.get(BILIBILI_SEARCH_TYPE_URL, params=params)
            response.raise_for_status()
            payload = response.json()
            if payload.get("code") != 0:
                raise RuntimeError(f"Bilibili keyword search rejected request: {payload.get('message') or payload.get('code')}")
            rows = (payload.get("data") or {}).get("result") or []
            for item in rows:
                candidate = parse_search_item(item)
                if not candidate or not candidate.action_plan["is_lottery"]:
                    continue
                if candidate.url in seen:
                    continue
                seen.add(candidate.url)
                candidates.append(candidate)
                if len(candidates) >= max(1, min(int(limit), 100)):
                    return candidates
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


def parse_search_item(item: dict[str, Any]) -> BilibiliDynamicCandidate | None:
    if not isinstance(item, dict):
        return None

    rule_text = extract_search_text(item)
    if not rule_text:
        return None

    url = search_item_url(item, rule_text)
    if not url or not validate_lottery_target("bilibili", url).valid:
        return None

    action_plan = parse_lottery_rule(rule_text, "bilibili")
    return BilibiliDynamicCandidate(
        dynamic_id=extract_dynamic_id_from_url(url),
        url=url,
        title=first_nonempty_line(rule_text)[:160],
        rule_text=rule_text,
        published_at=timestamp_to_datetime(item.get("pubdate") or item.get("pub_ts") or item.get("ctime")),
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


def extract_search_text(item: dict[str, Any]) -> str:
    chunks: list[str] = []
    for field in SEARCH_TEXT_FIELDS:
        append_text(chunks, strip_html(item.get(field)))
    for field in ("tag", "tags"):
        value = item.get(field)
        if isinstance(value, list):
            append_text(chunks, " ".join(str(part) for part in value))
        else:
            append_text(chunks, strip_html(value))
    return "\n".join(dict.fromkeys(chunk for chunk in chunks if chunk)).strip()


def search_item_url(item: dict[str, Any], text: str = "") -> str:
    for field in ("arcurl", "url", "uri"):
        url = normalize_bilibili_url(item.get(field))
        if url:
            return url
    for field in ("dynamic_id", "dynamic_id_str", "id_str"):
        dynamic_id = str(item.get(field) or "").strip()
        if dynamic_id.isdigit():
            return f"https://t.bilibili.com/{dynamic_id}"
    bvid = str(item.get("bvid") or "").strip()
    if bvid:
        return f"https://www.bilibili.com/video/{bvid}"
    urls = re.findall(r"https?://[^\s,，]+", str(text or ""))
    for url in urls:
        normalized = normalize_bilibili_url(url)
        if normalized:
            return normalized
    return ""


def normalize_bilibili_url(value: Any) -> str:
    url = str(value or "").strip()
    if not url:
        return ""
    url = url.replace("\\/", "/")
    if url.startswith("//"):
        url = "https:" + url
    if not url.startswith("http"):
        return ""
    if "bilibili.com" not in url:
        return ""
    return url.split("#", 1)[0]


def extract_dynamic_id_from_url(url: str) -> str:
    match = re.search(r"(?:t\.bilibili\.com|/dynamic/|/opus/)/?(\d+)", url)
    return match.group(1) if match else ""


def strip_html(value: Any) -> str:
    return re.sub(r"<[^>]+>", "", str(value or ""))


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


async def fetch_wbi_keys(client) -> tuple[str, str]:
    response = await client.get(BILIBILI_NAV_URL)
    response.raise_for_status()
    data = response.json()
    wbi_img = (data.get("data") or {}).get("wbi_img") or {}
    img_key = key_from_url(wbi_img.get("img_url", ""))
    sub_key = key_from_url(wbi_img.get("sub_url", ""))
    if not img_key or not sub_key:
        raise RuntimeError("could not obtain Bilibili wbi keys")
    return img_key, sub_key


def key_from_url(url: str) -> str:
    return str(url or "").rsplit("/", 1)[-1].split(".", 1)[0]


def get_mixin_key(img_key: str, sub_key: str) -> str:
    raw = img_key + sub_key
    return "".join(raw[i] for i in MIXIN_KEY_ENC_TAB)[:32]


def sign_wbi_params(params: dict[str, Any], img_key: str, sub_key: str) -> dict[str, Any]:
    signed = {str(k): v for k, v in params.items()}
    signed["wts"] = int(time.time())
    items = []
    for key in sorted(signed):
        value = "".join(char for char in str(signed[key]) if char not in WBI_FILTER_CHARS)
        items.append((key, value))
    query = urllib.parse.urlencode(items)
    signed["w_rid"] = hashlib.md5((query + get_mixin_key(img_key, sub_key)).encode("utf-8")).hexdigest()
    return signed
