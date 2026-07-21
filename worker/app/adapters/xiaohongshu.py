from app.adapters.base import UnsupportedPlatformAction
from app.adapters.selector_flow import SelectorFlowAdapter


FOLLOW_TEXT = "关注"
LIKE_TEXT = "点赞"
COMMENT_TEXT = "评论"
SEND_TEXT = "发送"
FAVORITE_TEXT = "收藏"


class XiaohongshuAdapter(SelectorFlowAdapter):
    PLATFORM = "xiaohongshu"
    ACTIONS = ("followed", "liked", "commented", "favorited")
    STATUS = "manual_only"
    CAPABILITY_BLOCK_REASON = "xiaohongshu_no_official_interaction_api"
    MANUAL_CONFIRMATION_REQUIRED = True
    OFFICIAL_INTERACTION_API_AVAILABLE = False
    DEFAULT_SELECTOR_PROBES = {
        "followed": [
            f"button:has-text('{FOLLOW_TEXT}')",
            f"[class*='follow']:has-text('{FOLLOW_TEXT}')",
        ],
        "liked": [
            "[class*='like-wrapper']",
            f"[aria-label*='{LIKE_TEXT}']",
            "[class*='like']",
        ],
        "commented": [
            "[contenteditable='true']",
            f"[placeholder*='{COMMENT_TEXT}']",
            "textarea",
            f"button:has-text('{SEND_TEXT}')",
        ],
        "favorited": [
            "[class*='collect-wrapper']",
            "[class*='favorite']",
            f"[aria-label*='{FAVORITE_TEXT}']",
            f"button:has-text('{FAVORITE_TEXT}')",
        ],
    }

    def __init__(self, selector_config: dict | None = None):
        super().__init__(selector_config=selector_config)
        # A complete selector set is observation metadata only. It cannot
        # upgrade this adapter into a supported real-action implementation.
        self.REAL_ACTIONS = False
        self.STATUS = "manual_only"

    def _unsupported_interaction(self, action: str) -> UnsupportedPlatformAction:
        return UnsupportedPlatformAction(
            f"{self.CAPABILITY_BLOCK_REASON}:{action}"
        )

    async def _follow(self, page):
        raise self._unsupported_interaction("followed")

    async def _like(self, page):
        raise self._unsupported_interaction("liked")

    async def _comment(self, page):
        raise self._unsupported_interaction("commented")

    async def _favorite(self, page):
        raise self._unsupported_interaction("favorited")

    async def _repost(self, page):
        # Legacy callers must not fall back to SelectorFlowAdapter's click path.
        raise self._unsupported_interaction("reposted")
