import json

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
