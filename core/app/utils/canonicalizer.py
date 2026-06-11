
import hashlib

from urllib.parse import urlparse, urlunparse

from dataclasses import dataclass



@dataclass(frozen=True)

class CanonicalURL:

    platform: str

    resource_type: str

    resource_id: str



    def to_uri(self) -> str:

        return f"canonical://{self.platform}/{self.resource_type}/{self.resource_id}"



    def to_sha256(self) -> str:

        return hashlib.sha256(self.to_uri().encode()).hexdigest()



class BilibiliCanonicalizer:

    @staticmethod

    async def canonicalize(raw_url: str) -> CanonicalURL:

        if 'b23.tv' in raw_url:

            import httpx

            async with httpx.AsyncClient() as client:

                resp = await client.head(raw_url, follow_redirects=True)

                raw_url = str(resp.url)

        parsed = urlparse(raw_url)

        clean_url = urlunparse((parsed.scheme, parsed.netloc, parsed.path, '', '', ''))

        if '/video/' in clean_url:

            bv = clean_url.split('/video/')[1].split('/')[0].split('?')[0].split('#')[0]

            return CanonicalURL("bilibili", "video", bv)

        if parsed.netloc == 't.bilibili.com' and parsed.path.startswith('/opus/'):

            return CanonicalURL("bilibili", "dynamic", "opus_" + parsed.path.split('/')[-1])

        if parsed.netloc == 't.bilibili.com':

            did = parsed.path.strip('/')

            return CanonicalURL("bilibili", "dynamic", did)

        if '/opus/' in clean_url:

            opus_id = clean_url.split('/opus/')[-1].split('?')[0].split('#')[0]

            return CanonicalURL("bilibili", "dynamic", "opus_" + opus_id)

        if '/read/' in clean_url:

            cvid = clean_url.split('/read/')[1].split('/')[0].split('?')[0].split('#')[0]

            return CanonicalURL("bilibili", "article", cvid)

        raise ValueError(f"Cannot canonicalize: {raw_url}")


class WeiboCanonicalizer:
    @staticmethod
    async def canonicalize(raw_url: str) -> CanonicalURL:
        raw_url = await resolve_short_link(raw_url, "t.cn")
        parsed = urlparse(raw_url)
        host = parsed.netloc.lower().split(":", 1)[0]
        path_parts = [part for part in parsed.path.split("/") if part]

        if host == "m.weibo.cn" and len(path_parts) == 2 and path_parts[0] in {"status", "detail"}:
            return CanonicalURL("weibo", "status", path_parts[1])
        if host in {"weibo.com", "www.weibo.com"}:
            if len(path_parts) == 2 and path_parts[0] == "detail":
                return CanonicalURL("weibo", "status", path_parts[1])
            if len(path_parts) == 2 and path_parts[0].isdigit():
                return CanonicalURL("weibo", "status", path_parts[1])
        raise ValueError(f"Cannot canonicalize: {raw_url}")


class XiaohongshuCanonicalizer:
    @staticmethod
    async def canonicalize(raw_url: str) -> CanonicalURL:
        raw_url = await resolve_short_link(raw_url, "xhslink.com")
        parsed = urlparse(raw_url)
        host = parsed.netloc.lower().split(":", 1)[0]
        path_parts = [part for part in parsed.path.split("/") if part]

        if host in {"xiaohongshu.com", "www.xiaohongshu.com"}:
            if len(path_parts) == 2 and path_parts[0] == "explore":
                return CanonicalURL("xiaohongshu", "note", path_parts[1].lower())
            if len(path_parts) == 3 and path_parts[0] == "discovery" and path_parts[1] == "item":
                return CanonicalURL("xiaohongshu", "note", path_parts[2].lower())
        raise ValueError(f"Cannot canonicalize: {raw_url}")


class DouyinCanonicalizer:
    @staticmethod
    async def canonicalize(raw_url: str) -> CanonicalURL:
        raw_url = await resolve_short_link(raw_url, "v.douyin.com")
        parsed = urlparse(raw_url)
        host = parsed.netloc.lower().split(":", 1)[0]
        path_parts = [part for part in parsed.path.split("/") if part]

        if host in {"douyin.com", "www.douyin.com"}:
            if len(path_parts) == 2 and path_parts[0] == "video" and path_parts[1].isdigit():
                return CanonicalURL("douyin", "video", path_parts[1])
        if host == "www.iesdouyin.com":
            if len(path_parts) == 3 and path_parts[0] == "share" and path_parts[1] == "video" and path_parts[2].isdigit():
                return CanonicalURL("douyin", "video", path_parts[2])
        raise ValueError(f"Cannot canonicalize: {raw_url}")


async def resolve_short_link(raw_url: str, short_host: str) -> str:
    host = urlparse(raw_url).netloc.lower().split(":", 1)[0]
    if host != short_host:
        return raw_url
    import httpx

    async with httpx.AsyncClient() as client:
        resp = await client.head(raw_url, follow_redirects=True)
        return str(resp.url)


class GenericCanonicalizer:
    @staticmethod
    async def canonicalize(platform: str, raw_url: str) -> str:
        parsed = urlparse(raw_url.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"Invalid URL: {raw_url}")
        clean_url = urlunparse((parsed.scheme, parsed.netloc.lower(), parsed.path.rstrip("/") or "/", "", parsed.query, ""))
        return f"canonical://{platform}/url/{hashlib.sha256(clean_url.encode()).hexdigest()}"


PLATFORM_CANONICALIZERS = {
    "bilibili": BilibiliCanonicalizer,
    "weibo": WeiboCanonicalizer,
    "xiaohongshu": XiaohongshuCanonicalizer,
    "douyin": DouyinCanonicalizer,
}


async def canonicalize_platform_url(platform: str, raw_url: str) -> str:
    canonicalizer = PLATFORM_CANONICALIZERS.get(platform)
    if canonicalizer:
        return (await canonicalizer.canonicalize(raw_url)).to_uri()
    return await GenericCanonicalizer.canonicalize(platform, raw_url)
