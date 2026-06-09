import json
from http.cookies import SimpleCookie


PLATFORM_COOKIE_DOMAINS = {
    "bilibili": ".bilibili.com",
    "weibo": ".weibo.com",
    "douyin": ".douyin.com",
    "xiaohongshu": ".xiaohongshu.com",
}


def normalize_cookie_payload(platform: str, payload: str) -> str:
    cookies = parse_cookie_payload(platform, payload)
    return json.dumps(cookies, ensure_ascii=False, separators=(",", ":"))


def validate_required_cookies(cookies: list[dict], required_names: list[str]) -> None:
    present = {cookie.get("name") for cookie in cookies}
    missing = sorted(set(required_names).difference(present))
    if missing:
        raise ValueError(f"缺少平台必需 Cookie: {', '.join(missing)}")


def parse_cookie_payload(platform: str, payload: str) -> list[dict]:
    text = normalize_raw_cookie_text(payload)
    if not text:
        raise ValueError("Cookie 不能为空")

    if text.startswith("[") or text.startswith("{"):
        data = json.loads(text)
        if isinstance(data, dict):
            data = data.get("cookies", [])
        if not isinstance(data, list):
            raise ValueError("JSON Cookie 必须是数组，或包含 cookies 数组")
        cookies = [_normalize_cookie_item(platform, item) for item in data]
    else:
        parsed = SimpleCookie()
        parsed.load(text)
        cookies = [
            _normalize_cookie_item(platform, {"name": key, "value": morsel.value})
            for key, morsel in parsed.items()
        ]

    cookies = [cookie for cookie in cookies if cookie.get("name") and cookie.get("value") is not None]
    if not cookies:
        raise ValueError("未识别到有效 Cookie")
    return cookies


def normalize_raw_cookie_text(payload: str) -> str:
    text = (payload or "").strip()
    if not text:
        return ""
    if text.startswith("[") or text.startswith("{"):
        return text

    lines = []
    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        if line.lower().startswith("cookie:"):
            line = line.split(":", 1)[1].strip()
        if line.lower().startswith("set-cookie:"):
            line = line.split(":", 1)[1].strip()
        lines.append(line)
    return "; ".join(lines)


def _normalize_cookie_item(platform: str, item: dict) -> dict:
    if not isinstance(item, dict):
        raise ValueError("Cookie 项必须是对象")
    domain = item.get("domain") or PLATFORM_COOKIE_DOMAINS.get(platform, ".bilibili.com")
    path = item.get("path") or "/"
    cookie = {
        "name": str(item.get("name", "")).strip(),
        "value": str(item.get("value", "")),
        "domain": domain,
        "path": path,
        "httpOnly": bool(item.get("httpOnly", False)),
        "secure": bool(item.get("secure", True)),
    }
    if item.get("expires") not in (None, "", -1):
        try:
            cookie["expires"] = int(float(item["expires"]))
        except (TypeError, ValueError):
            pass
    if item.get("sameSite") in {"Strict", "Lax", "None"}:
        cookie["sameSite"] = item["sameSite"]
    return cookie
