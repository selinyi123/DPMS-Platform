
import hashlib
import re
from types import MappingProxyType

from urllib.parse import urljoin, urlparse, urlunparse

from dataclasses import dataclass

from app.platform_modules.catalog import (
    BILIBILI_DYNAMIC_ID_PATTERN,
    BILIBILI_VIDEO_ID_PATTERN,
    DOUYIN_NOTE_ID_PATTERN,
    DOUYIN_VIDEO_ID_PATTERN,
    PLATFORM_MODULE_SPECS,
    WEIBO_UID_PATTERN,
    XIAOHONGSHU_NOTE_PATTERN,
    is_weibo_status_id,
)


class CanonicalizationError(ValueError):
    """A stable, non-secret target canonicalization failure."""

    def __init__(self, code: str, *, retryable: bool = False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


REDIRECT_STATUS_CODES = frozenset({301, 302, 303, 307, 308})



@dataclass(frozen=True)

class CanonicalURL:

    platform: str

    resource_type: str

    resource_id: str



    def to_uri(self) -> str:

        return f"canonical://{self.platform}/{self.resource_type}/{self.resource_id}"



    def to_sha256(self) -> str:

        return hashlib.sha256(self.to_uri().encode()).hexdigest()


PLATFORM_CANONICAL_HOSTS = MappingProxyType(
    {
        platform: spec.canonical_hosts
        for platform, spec in PLATFORM_MODULE_SPECS.items()
    }
)


def validated_platform_https_url(platform: str, raw_url: str) -> str:
    platform_key = str(platform or "").strip().lower()
    target = str(raw_url or "").strip()
    parsed = urlparse(target)
    if parsed.scheme.lower() != "https" or parsed.username is not None or parsed.password is not None:
        raise ValueError("canonicalization_target_not_allowed")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("canonicalization_target_not_allowed") from exc
    host = (parsed.hostname or "").rstrip(".").lower()
    if port not in (None, 443) or host not in PLATFORM_CANONICAL_HOSTS.get(platform_key, set()):
        raise ValueError("canonicalization_target_not_allowed")
    return target


async def resolve_platform_short_link(raw_url: str, platform: str, short_host: str) -> str:
    """Resolve a platform short URL without ever following an unchecked hop."""
    import httpx

    current = validated_platform_https_url(platform, raw_url)
    async with httpx.AsyncClient(timeout=5.0, max_redirects=0) as client:
        for _ in range(5):
            parsed = urlparse(current)
            host = (parsed.hostname or "").rstrip(".").lower()
            if host != short_host:
                return current
            head_error = None
            try:
                response = await client.head(current, follow_redirects=False)
                location = (
                    response.headers.get("location")
                    if response.status_code in REDIRECT_STATUS_CODES
                    else None
                )
            except httpx.RequestError as exc:
                head_error = exc
                location = None

            if not location:
                # b23.tv can answer HEAD with 200 even though a regular GET
                # redirects. Always make one bounded, non-following GET when
                # HEAD did not produce a usable Location. The Range header
                # keeps compliant responses to one byte.
                try:
                    async with client.stream(
                        "GET",
                        current,
                        follow_redirects=False,
                        headers={"Range": "bytes=0-0"},
                    ) as fallback:
                        fallback_location = (
                            fallback.headers.get("location")
                            if fallback.status_code in REDIRECT_STATUS_CODES
                            else None
                        )
                except httpx.TimeoutException as exc:
                    raise CanonicalizationError(
                        "canonicalization_short_link_timeout",
                        retryable=True,
                    ) from exc
                except httpx.RequestError as exc:
                    raise CanonicalizationError(
                        "canonicalization_short_link_unavailable",
                        retryable=True,
                    ) from exc
                if fallback_location:
                    location = fallback_location
                elif head_error is not None:
                    raise CanonicalizationError(
                        "canonicalization_short_link_unavailable",
                        retryable=True,
                    ) from head_error
                else:
                    raise CanonicalizationError(
                        "canonicalization_short_link_unresolved"
                    )
            current = validated_platform_https_url(platform, urljoin(current, location))
    raise ValueError("canonicalization_redirect_limit_exceeded")



class BilibiliCanonicalizer:

    @staticmethod

    async def canonicalize(raw_url: str) -> CanonicalURL:

        raw_url = validated_platform_https_url("bilibili", raw_url)
        parsed = urlparse(raw_url)
        host = (parsed.hostname or "").rstrip(".").lower()

        if host == 'b23.tv':

            raw_url = await resolve_platform_short_link(raw_url, "bilibili", "b23.tv")

        parsed = urlparse(raw_url)
        host = (parsed.hostname or "").rstrip(".").lower()

        clean_url = urlunparse((parsed.scheme, parsed.netloc, parsed.path, '', '', ''))

        if host in {"bilibili.com", "www.bilibili.com", "m.bilibili.com"} and '/video/' in clean_url:

            bv = clean_url.split('/video/')[1].split('/')[0].split('?')[0].split('#')[0]

            if BILIBILI_VIDEO_ID_PATTERN.fullmatch(bv):
                return CanonicalURL("bilibili", "video", bv)

        path_parts = [part for part in parsed.path.split("/") if part]
        if host == 't.bilibili.com' and len(path_parts) == 2 and path_parts[0] == 'opus' and BILIBILI_DYNAMIC_ID_PATTERN.fullmatch(path_parts[1]):

            return CanonicalURL("bilibili", "dynamic", "opus_" + path_parts[1])

        if host == 't.bilibili.com' and len(path_parts) == 1 and BILIBILI_DYNAMIC_ID_PATTERN.fullmatch(path_parts[0]):

            return CanonicalURL("bilibili", "dynamic", path_parts[0])

        if host in {"bilibili.com", "www.bilibili.com", "m.bilibili.com"} and '/opus/' in clean_url:

            opus_id = clean_url.split('/opus/')[-1].split('?')[0].split('#')[0]

            if BILIBILI_DYNAMIC_ID_PATTERN.fullmatch(opus_id):
                return CanonicalURL("bilibili", "dynamic", "opus_" + opus_id)

        if (
            host in {"bilibili.com", "www.bilibili.com", "m.bilibili.com"}
            and len(path_parts) == 2
            and path_parts[0] == "read"
            and re.fullmatch(r"cv[0-9]+", path_parts[1])
        ):
            return CanonicalURL("bilibili", "article", path_parts[1])

        raise ValueError(f"Cannot canonicalize: {raw_url}")


class WeiboCanonicalizer:
    @staticmethod
    async def canonicalize(raw_url: str) -> CanonicalURL:
        raw_url = await resolve_short_link(raw_url, "t.cn", platform="weibo")
        parsed = urlparse(raw_url)
        host = (parsed.hostname or "").rstrip(".").lower()
        path_parts = [part for part in parsed.path.split("/") if part]

        if (
            host == "m.weibo.cn"
            and len(path_parts) == 2
            and path_parts[0] in {"status", "detail"}
            and is_weibo_status_id(path_parts[1])
        ):
            return CanonicalURL("weibo", "status", path_parts[1])
        if host in {"weibo.com", "www.weibo.com"}:
            if (
                len(path_parts) == 2
                and path_parts[0] == "detail"
                and is_weibo_status_id(path_parts[1])
            ):
                return CanonicalURL("weibo", "status", path_parts[1])
            if (
                len(path_parts) == 2
                and WEIBO_UID_PATTERN.fullmatch(path_parts[0])
                and is_weibo_status_id(path_parts[1])
            ):
                return CanonicalURL("weibo", "status", path_parts[1])
        raise ValueError(f"Cannot canonicalize: {raw_url}")


class XiaohongshuCanonicalizer:
    @staticmethod
    async def canonicalize(raw_url: str) -> CanonicalURL:
        raw_url = await resolve_short_link(raw_url, "xhslink.com", platform="xiaohongshu")
        parsed = urlparse(raw_url)
        host = (parsed.hostname or "").rstrip(".").lower()
        path_parts = [part for part in parsed.path.split("/") if part]

        if host in {"xiaohongshu.com", "www.xiaohongshu.com"}:
            if (
                len(path_parts) == 2
                and path_parts[0] == "explore"
                and XIAOHONGSHU_NOTE_PATTERN.fullmatch(path_parts[1])
            ):
                return CanonicalURL("xiaohongshu", "note", path_parts[1].lower())
            if (
                len(path_parts) == 3
                and path_parts[0] == "discovery"
                and path_parts[1] == "item"
                and XIAOHONGSHU_NOTE_PATTERN.fullmatch(path_parts[2])
            ):
                return CanonicalURL("xiaohongshu", "note", path_parts[2].lower())
        raise ValueError(f"Cannot canonicalize: {raw_url}")


class DouyinCanonicalizer:
    @staticmethod
    async def canonicalize(raw_url: str) -> CanonicalURL:
        raw_url = await resolve_short_link(raw_url, "v.douyin.com", platform="douyin")
        parsed = urlparse(raw_url)
        host = (parsed.hostname or "").rstrip(".").lower()
        path_parts = [part for part in parsed.path.split("/") if part]

        if host in {"douyin.com", "www.douyin.com"}:
            if (
                len(path_parts) == 2
                and path_parts[0] == "video"
                and DOUYIN_VIDEO_ID_PATTERN.fullmatch(path_parts[1])
            ):
                return CanonicalURL("douyin", "video", path_parts[1])
            if (
                host == "www.douyin.com"
                and len(path_parts) == 2
                and path_parts[0] == "note"
                and DOUYIN_NOTE_ID_PATTERN.fullmatch(path_parts[1])
            ):
                return CanonicalURL("douyin", "note", path_parts[1])
        if host == "www.iesdouyin.com":
            if (
                len(path_parts) == 3
                and path_parts[0] == "share"
                and path_parts[1] == "video"
                and DOUYIN_VIDEO_ID_PATTERN.fullmatch(path_parts[2])
            ):
                return CanonicalURL("douyin", "video", path_parts[2])
        raise ValueError(f"Cannot canonicalize: {raw_url}")


async def resolve_short_link(raw_url: str, short_host: str, *, platform: str) -> str:
    raw_url = validated_platform_https_url(platform, raw_url)
    host = (urlparse(raw_url).hostname or "").rstrip(".").lower()
    if host != short_host:
        return raw_url
    return await resolve_platform_short_link(raw_url, platform, short_host)


class GenericCanonicalizer:
    @staticmethod
    async def canonicalize(platform: str, raw_url: str) -> str:
        parsed = urlparse(raw_url.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"Invalid URL: {raw_url}")
        clean_url = urlunparse((parsed.scheme, parsed.netloc.lower(), parsed.path.rstrip("/") or "/", "", parsed.query, ""))
        return f"canonical://{platform}/url/{hashlib.sha256(clean_url.encode()).hexdigest()}"


PLATFORM_CANONICALIZERS = MappingProxyType({
    "bilibili": BilibiliCanonicalizer,
    "weibo": WeiboCanonicalizer,
    "xiaohongshu": XiaohongshuCanonicalizer,
    "douyin": DouyinCanonicalizer,
})


async def canonicalize_platform_url(platform: str, raw_url: str) -> str:
    platform_key = str(platform or "").strip().lower()
    # Import locally so individual platform modules can expose this utility as
    # their normalization implementation without a registry import cycle.
    from app.platform_modules import get_platform_module

    platform_module = get_platform_module(platform_key)
    if platform_module is not None:
        return await platform_module.canonicalize_target(raw_url)
    return await GenericCanonicalizer.canonicalize(platform, raw_url)
