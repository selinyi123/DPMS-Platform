import json
from http.cookies import SimpleCookie

from app.platforms import get_platform


async def inject_account_cookies(context, platform: str, credential: str):
    cookies = json.loads(credential)
    if not isinstance(cookies, list) or not cookies:
        raise ValueError("Account cookie is empty or invalid")

    await context.add_cookies([normalize_cookie(platform, cookie) for cookie in cookies])


def normalize_cookie(platform: str, cookie: dict) -> dict:
    domain = cookie.get("domain") or get_platform(platform).get("cookie_domain") or ".bilibili.com"
    normalized = {
        "name": str(cookie["name"]),
        "value": str(cookie["value"]),
        "domain": domain,
        "path": cookie.get("path") or "/",
        "httpOnly": bool(cookie.get("httpOnly", False)),
        "secure": bool(cookie.get("secure", True)),
    }
    if cookie.get("expires") not in (None, "", -1):
        normalized["expires"] = int(cookie["expires"])
    if cookie.get("sameSite") in {"Strict", "Lax", "None"}:
        normalized["sameSite"] = cookie["sameSite"]
    return normalized


def serialize_cookies(platform: str, cookies: list[dict]) -> str:
    normalized = [
        normalize_cookie(platform, cookie)
        for cookie in cookies
        if cookie.get("name") and cookie.get("value") is not None
    ]
    return json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))


def credential_to_cookie_header(credential: str) -> str:
    """Convert a stored account credential into a raw Cookie header."""
    text = (credential or "").strip()
    if not text:
        return ""
    if text.startswith("[") or text.startswith("{"):
        data = json.loads(text)
        if isinstance(data, dict):
            data = data.get("cookies", [])
        if not isinstance(data, list):
            raise ValueError("JSON credential must be a cookie list")
        pairs = []
        for item in data:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name or item.get("value") is None:
                continue
            pairs.append(f"{name}={item.get('value')}")
        return "; ".join(pairs)

    lines = []
    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        if line.lower().startswith("cookie:"):
            line = line.split(":", 1)[1].strip()
        if line.lower().startswith("set-cookie:"):
            cookie = SimpleCookie()
            cookie.load(line.split(":", 1)[1].strip())
            lines.extend(f"{key}={morsel.value}" for key, morsel in cookie.items())
            continue
        lines.append(line)
    return "; ".join(lines)
