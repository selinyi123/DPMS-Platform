from dataclasses import dataclass
from html import unescape
from urllib.parse import parse_qs, urlparse

GENERATE_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
    "Referer": "https://passport.bilibili.com/login",
}
BILIBILI_LOGIN_COOKIE_NAMES = (
    "SESSDATA",
    "bili_jct",
    "DedeUserID",
    "DedeUserID__ckMd5",
    "sid",
)
BILIBILI_REQUIRED_LOGIN_COOKIES = frozenset({"SESSDATA", "DedeUserID"})


@dataclass(frozen=True)
class BilibiliQrPollResult:
    status: str
    message: str
    cookies: list[dict] | None = None


def provider_qr_controls_expiry(platform: str, provider_key: str | None) -> bool:
    """Return whether provider polling, rather than local time, is terminal."""

    return (
        str(platform or "").strip().casefold() == "bilibili"
        and bool(str(provider_key or "").strip())
    )


async def generate_bilibili_qr() -> tuple[str, str]:
    import httpx

    async with httpx.AsyncClient(timeout=10, headers=REQUEST_HEADERS) as client:
        response = await client.get(GENERATE_URL)
        response.raise_for_status()
    payload = response.json()
    if payload.get("code") != 0:
        raise ValueError(payload.get("message") or "bilibili_qr_generate_failed")
    data = payload.get("data") or {}
    qr_url = str(data.get("url") or "")
    qr_key = str(data.get("qrcode_key") or "")
    if not qr_url or not qr_key:
        raise ValueError("bilibili_qr_generate_invalid_response")
    return qr_url, qr_key


async def poll_bilibili_qr(qr_key: str) -> BilibiliQrPollResult:
    import httpx

    async with httpx.AsyncClient(timeout=10, headers=REQUEST_HEADERS) as client:
        response = await client.get(POLL_URL, params={"qrcode_key": qr_key})
        response.raise_for_status()
    payload = response.json()
    if payload.get("code") != 0:
        raise ValueError(payload.get("message") or "bilibili_qr_poll_failed")
    data = payload.get("data") or {}
    code = int(data.get("code", -1))
    message = str(data.get("message") or "")
    if code == 86101:
        return BilibiliQrPollResult("waiting_scan", message)
    if code == 86090:
        return BilibiliQrPollResult("scanned", message)
    if code == 86038:
        return BilibiliQrPollResult("expired", message)
    if code != 0:
        raise ValueError(f"bilibili_qr_unknown_status:{code}:{message}")

    cookies = cookies_from_poll_response(
        str(data.get("url") or ""),
        response.cookies.jar,
    )
    return BilibiliQrPollResult("confirmed", message, cookies)


def cookies_from_login_url(login_url: str) -> list[dict]:
    cookies = _cookies_from_login_url(login_url)
    _validate_login_cookies(cookies)
    return cookies


def cookies_from_poll_response(login_url: str, response_cookie_jar) -> list[dict]:
    """Extract a successful QR credential from both provider channels.

    Bilibili has historically returned login cookies in the redirect URL,
    while current responses can also deliver them through ``Set-Cookie``.
    The response cookie is authoritative because it preserves the exact value
    and attributes, including percent-encoding in ``SESSDATA``.
    """

    by_name = {
        cookie["name"]: cookie
        for cookie in _cookies_from_login_url(login_url)
    }
    response_values: dict[str, str] = {}
    for provider_cookie in response_cookie_jar or ():
        name = str(getattr(provider_cookie, "name", "") or "")
        if name not in BILIBILI_LOGIN_COOKIE_NAMES:
            continue
        value = str(getattr(provider_cookie, "value", "") or "")
        if not value:
            continue
        previous = response_values.get(name)
        if previous is not None and previous != value:
            raise ValueError(f"bilibili_qr_conflicting_cookie:{name}")
        response_values[name] = value

        domain = str(
            getattr(provider_cookie, "domain", "") or ".bilibili.com"
        )
        normalized_domain = domain.lstrip(".").casefold()
        if normalized_domain != "bilibili.com" and not normalized_domain.endswith(
            ".bilibili.com"
        ):
            continue
        path = str(getattr(provider_cookie, "path", "") or "/")
        cookie = {
            "name": name,
            "value": value,
            "domain": domain,
            "path": path,
            "secure": bool(getattr(provider_cookie, "secure", True)),
            "httpOnly": _cookie_has_http_only(provider_cookie),
        }
        expires = getattr(provider_cookie, "expires", None)
        if expires is not None:
            try:
                cookie["expires"] = int(expires)
            except (TypeError, ValueError):
                pass
        # Do not create duplicate-name credentials when the redirect query is
        # decoded differently from the provider's Set-Cookie representation.
        by_name[name] = cookie

    cookies = [
        by_name[name]
        for name in BILIBILI_LOGIN_COOKIE_NAMES
        if name in by_name
    ]
    _validate_login_cookies(cookies)
    return cookies


def _cookies_from_login_url(login_url: str) -> list[dict]:
    query = parse_qs(
        urlparse(unescape(str(login_url or ""))).query,
        keep_blank_values=True,
    )
    cookies = []
    for name in BILIBILI_LOGIN_COOKIE_NAMES:
        values = query.get(name)
        if not values or values[0] == "":
            continue
        cookies.append(
            {
                "name": name,
                "value": values[0],
                "domain": ".bilibili.com",
                "path": "/",
                "secure": True,
                "httpOnly": name == "SESSDATA",
            }
        )
    return cookies


def _cookie_has_http_only(cookie) -> bool:
    has_nonstandard_attr = getattr(cookie, "has_nonstandard_attr", None)
    if callable(has_nonstandard_attr):
        try:
            if has_nonstandard_attr("HttpOnly"):
                return True
        except (KeyError, TypeError, ValueError):
            pass
    return "HttpOnly" in (getattr(cookie, "_rest", {}) or {})


def _validate_login_cookies(cookies: list[dict]) -> None:
    present = {cookie["name"] for cookie in cookies}
    missing = sorted(BILIBILI_REQUIRED_LOGIN_COOKIES.difference(present))
    if missing:
        raise ValueError(f"bilibili_qr_missing_cookies:{','.join(missing)}")
