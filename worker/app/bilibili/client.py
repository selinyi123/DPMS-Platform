"""Async Bilibili web-API client.

Direct HTTP client (httpx) reproducing the *endpoints and request shapes* that
LotteryAutoScript uses, re-implemented in Python with two deliberate additions
the reference lacks: wbi signing on the GETs that now require it, and a real
exponential-ish HTTP retry. State-changing calls return a classified
:class:`~worker.app.bilibili.errors.CodeResult`; reads return parsed JSON.

The client is transport-injectable (``transport=httpx.MockTransport(...)``) so
request construction — cookie/csrf injection, wbi params, POST bodies — is
verifiable offline without touching the network or a real account.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from http.cookies import SimpleCookie
from typing import Any, Optional

import httpx

from . import wbi
from .config import BiliEngineConfig
from .errors import BiliApiError, CodeResult, classify

# Hosts/paths.
_NAV = "https://api.bilibili.com/x/web-interface/nav"
_SPACE_MYINFO = "https://api.bilibili.com/x/space/myinfo"
_FEED_SPACE = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
_DYN_DETAIL = "https://api.bilibili.com/x/polymer/web-dynamic/v1/detail"
_LOTTERY_NOTICE = "https://api.vc.bilibili.com/lottery_svr/v1/lottery_svr/lottery_notice"
_RELATION_MODIFY = "https://api.bilibili.com/x/relation/modify"
_FEED_SETUSERFOLLOW = "https://api.vc.bilibili.com/feed/v1/feed/SetUserFollow"
_RELATION_BATCH_MODIFY = "https://api.bilibili.com/x/relation/batch/modify"
_DYNAMIC_LIKE = "https://api.vc.bilibili.com/dynamic_like/v1/dynamic_like/thumb"
_DYNAMIC_REPOST = "https://api.vc.bilibili.com/dynamic_repost/v1/dynamic_repost/repost"
_REPLY_ADD = "https://api.bilibili.com/x/v2/reply/add"
_RESERVE = "https://api.vc.bilibili.com/dynamic_mix/v1/dynamic_mix/reserve_attach_card_button"

_WBI_TTL_SECONDS = 3600


def parse_cookie(cookie: str) -> dict[str, str]:
    """Parse a raw Cookie header into a name->value dict."""
    jar: dict[str, str] = {}
    sc = SimpleCookie()
    # SimpleCookie chokes on some values; fall back to manual split.
    try:
        sc.load(cookie)
        jar = {k: m.value for k, m in sc.items()}
    except Exception:
        jar = {}
    if not jar:
        for part in cookie.split(";"):
            if "=" in part:
                k, _, v = part.partition("=")
                jar[k.strip()] = v.strip()
    return jar


def _random_buvid3() -> str:
    return f"{secrets.token_hex(16).upper()}{secrets.randbelow(100000):05d}infoc"


class BilibiliApiClient:
    """A single-account Bilibili web-API client.

    Not a context manager by accident: call :meth:`aclose` (or use
    ``async with``) so the underlying httpx client is released.
    """

    def __init__(
        self,
        cookie: str,
        *,
        config: BiliEngineConfig | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config or BiliEngineConfig()
        jar = parse_cookie(cookie)
        if "buvid3" not in jar:
            jar["buvid3"] = _random_buvid3()
        self.uid = int(jar.get("DedeUserID") or 0)
        self.csrf = jar.get("bili_jct", "")
        self._cookie_header = "; ".join(f"{k}={v}" for k, v in jar.items())

        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=self.config.timeout_seconds,
            follow_redirects=False,
            headers={
                "User-Agent": self.config.user_agent,
                "Accept": "application/json, text/plain, */*",
                "Referer": "https://www.bilibili.com/",
                "Origin": "https://www.bilibili.com",
                "Cookie": self._cookie_header,
            },
        )
        self._wbi_keys: Optional[tuple[str, str]] = None
        self._wbi_fetched_at: float = 0.0

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "BilibiliApiClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ----- low-level transport with retry -----

    async def _request(self, method: str, url: str, **kw: Any) -> httpx.Response:
        last: Exception | None = None
        for attempt in range(self.config.max_http_retries + 1):
            try:
                resp = await self._client.request(method, url, **kw)
                if resp.status_code >= 500:
                    raise BiliApiError(f"HTTP {resp.status_code} from {url}")
                return resp
            except (httpx.TransportError, BiliApiError) as exc:
                last = exc
                if attempt >= self.config.max_http_retries:
                    break
                await asyncio.sleep(self.config.http_retry_wait)
        raise BiliApiError(f"request failed after retries: {url}") from last

    async def _get_json(self, url: str, params: dict | None = None, *, wbi_sign: bool = False) -> dict:
        params = dict(params or {})
        if wbi_sign:
            img_key, sub_key = await self._ensure_wbi_keys()
            params = wbi.sign(params, img_key, sub_key)
        resp = await self._request("GET", url, params=params)
        try:
            return resp.json()
        except Exception as exc:  # noqa: BLE001 - surface non-JSON as transport error
            raise BiliApiError(f"non-JSON response from {url}: {resp.text[:200]}") from exc

    async def _post_action(self, action: str, url: str, data: dict) -> CodeResult:
        body = dict(data)
        body.setdefault("csrf", self.csrf)
        resp = await self._request(
            "POST",
            url,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise BiliApiError(f"non-JSON response from {url}: {resp.text[:200]}") from exc
        code = int(payload.get("code", -1))
        result = classify(action, code)
        # carry the captcha url through for the (future) OCR path
        if code == 12015:
            url_field = (payload.get("data") or {}).get("url")
            if url_field:
                object.__setattr__(result, "message", f"需要验证码: {url_field}")
        return result

    # ----- wbi keys -----

    async def _ensure_wbi_keys(self) -> tuple[str, str]:
        now = time.time()
        if self._wbi_keys and (now - self._wbi_fetched_at) < _WBI_TTL_SECONDS:
            return self._wbi_keys
        data = await self._get_json(_NAV)
        wbi_img = (data.get("data") or {}).get("wbi_img") or {}
        img_key = wbi.key_from_url(wbi_img.get("img_url", ""))
        sub_key = wbi.key_from_url(wbi_img.get("sub_url", ""))
        if not img_key or not sub_key:
            raise BiliApiError("could not obtain wbi keys from nav")
        self._wbi_keys = (img_key, sub_key)
        self._wbi_fetched_at = now
        return self._wbi_keys

    # ----- reads -----

    async def check_login(self) -> bool:
        """True iff the cookie is a logged-in session (nav.isLogin)."""
        data = await self._get_json(_NAV)
        return bool((data.get("data") or {}).get("isLogin"))

    async def get_myinfo(self) -> dict:
        return await self._get_json(_SPACE_MYINFO)

    async def get_space_dynamics(self, host_mid: int | str, offset: str = "") -> dict:
        params = {"host_mid": host_mid, "features": "itemOpusStyle"}
        if offset:
            params["offset"] = offset
        return await self._get_json(_FEED_SPACE, params, wbi_sign=True)

    async def get_dynamic_detail(self, dynamic_id: str) -> dict:
        return await self._get_json(
            _DYN_DETAIL, {"id": dynamic_id, "features": "itemOpusStyle"}, wbi_sign=True
        )

    async def get_lottery_notice_ts(self, dynamic_id: str) -> int:
        """Return the lottery draw timestamp, ``-9999`` if withdrawn, ``-1`` on error."""
        data = await self._get_json(_LOTTERY_NOTICE, {"dynamic_id": dynamic_id})
        code = int(data.get("code", -1))
        if code == 0:
            return int((data.get("data") or {}).get("lottery_time", -1))
        if code == -9999:
            return -9999
        return -1

    # ----- actions -----

    async def follow(self, uid: int) -> CodeResult:
        """Follow ``uid`` with three-route failover (relation -> feed -> batch)."""
        routes = (
            (_RELATION_MODIFY, {"fid": uid, "act": 1, "re_src": 0}),
            (_FEED_SETUSERFOLLOW, {"type": 1, "follow": uid}),
            (_RELATION_BATCH_MODIFY, {"fids": uid, "act": 1, "re_src": 1}),
        )
        result: CodeResult | None = None
        for url, body in routes:
            result = await self._post_action("follow", url, body)
            if result.outcome.value != "fatal":
                return result
        return result  # type: ignore[return-value]

    async def unfollow(self, uid: int) -> CodeResult:
        return await self._post_action("unfollow", _RELATION_MODIFY, {"fid": uid, "act": 2, "re_src": 0})

    async def like(self, dynamic_id: str) -> CodeResult:
        return await self._post_action(
            "like", _DYNAMIC_LIKE, {"uid": self.uid, "dynamic_id": dynamic_id, "up": 1}
        )

    async def repost(self, dynamic_id: str, content: str = "转发动态", ctrl: str = "[]") -> CodeResult:
        content = content[:233]
        return await self._post_action(
            "repost",
            _DYNAMIC_REPOST,
            {"uid": str(self.uid), "dynamic_id": dynamic_id, "content": content, "ctrl": ctrl},
        )

    async def comment(self, oid: str, chat_type: int, message: str, code: str = "") -> CodeResult:
        return await self._post_action(
            "comment", _REPLY_ADD, {"oid": oid, "type": chat_type, "message": message, "code": code}
        )

    async def reserve(self, reserve_id: int) -> CodeResult:
        return await self._post_action(
            "reserve", _RESERVE, {"cur_btn_status": "1", "reserve_id": reserve_id}
        )
