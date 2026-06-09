from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

GENERATE_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
    "Referer": "https://passport.bilibili.com/login",
}


@dataclass(frozen=True)
class BilibiliQrPollResult:
    status: str
    message: str
    cookies: list[dict] | None = None


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

    cookies = cookies_from_login_url(str(data.get("url") or ""))
    return BilibiliQrPollResult("confirmed", message, cookies)


def cookies_from_login_url(login_url: str) -> list[dict]:
    query = parse_qs(urlparse(login_url).query, keep_blank_values=True)
    cookie_names = (
        "SESSDATA",
        "bili_jct",
        "DedeUserID",
        "DedeUserID__ckMd5",
        "sid",
    )
    cookies = []
    for name in cookie_names:
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
    required = {"SESSDATA", "DedeUserID"}
    present = {cookie["name"] for cookie in cookies}
    missing = sorted(required.difference(present))
    if missing:
        raise ValueError(f"bilibili_qr_missing_cookies:{','.join(missing)}")
    return cookies
