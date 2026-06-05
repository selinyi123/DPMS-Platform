
import hashlib

from urllib.parse import urlparse, urlunparse

from dataclasses import dataclass

import httpx



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


class GenericCanonicalizer:
    @staticmethod
    async def canonicalize(platform: str, raw_url: str) -> str:
        parsed = urlparse(raw_url.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"Invalid URL: {raw_url}")
        clean_url = urlunparse((parsed.scheme, parsed.netloc.lower(), parsed.path.rstrip("/") or "/", "", parsed.query, ""))
        return f"canonical://{platform}/url/{hashlib.sha256(clean_url.encode()).hexdigest()}"
