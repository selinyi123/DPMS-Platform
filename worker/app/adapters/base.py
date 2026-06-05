class AdapterError(Exception):
    pass


class UnsupportedPlatformAction(AdapterError):
    pass


class BaseAdapter:
    PLATFORM = "unknown"
    ACTIONS = ("followed", "liked", "commented", "reposted")
    REAL_ACTIONS = False
    STATUS = "unsupported"
    SELECTOR_PROBES: dict[str, list[str]] = {}

    async def _follow(self, page):
        raise UnsupportedPlatformAction(f"{self.PLATFORM} follow action is not implemented")

    async def _like(self, page):
        raise UnsupportedPlatformAction(f"{self.PLATFORM} like action is not implemented")

    async def _comment(self, page):
        raise UnsupportedPlatformAction(f"{self.PLATFORM} comment action is not implemented")

    async def _repost(self, page):
        raise UnsupportedPlatformAction(f"{self.PLATFORM} repost action is not implemented")
