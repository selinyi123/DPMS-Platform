from app.adapters.base import UnsupportedPlatformAction
from app.adapters.selector_flow import SelectorFlowAdapter


FOLLOW_TEXT = "关注"
LIKE_TEXT = "赞"
COMMENT_TEXT = "评论"
FORWARD_TEXT = "转发"
PUBLISH_TEXT = "发布"


class WeiboAdapter(SelectorFlowAdapter):
    PLATFORM = "weibo"
    ACTIONS = ("followed", "liked", "commented", "favorited", "reposted")
    STATUS = "oauth_capability_required"
    CAPABILITY_BLOCK_REASON = "weibo_selector_observation_only"
    MANUAL_CONFIRMATION_REQUIRED = True
    OFFICIAL_INTERACTION_API_AVAILABLE = True
    DEFAULT_SELECTOR_PROBES = {
        "followed": [
            f"button:has-text('{FOLLOW_TEXT}')",
            f"[class*='follow']:has-text('{FOLLOW_TEXT}')",
            f"[title*='{FOLLOW_TEXT}']",
        ],
        "liked": [
            f"[title*='{LIKE_TEXT}']",
            "button[class*='like']",
            "[class*='woo-like']",
        ],
        "commented": [
            f"textarea[placeholder*='{COMMENT_TEXT}']",
            "textarea",
            f"button:has-text('{COMMENT_TEXT}')",
            f"button:has-text('{PUBLISH_TEXT}')",
        ],
        # Collection and repost are separate requirements. A generic share or
        # overflow-menu selector cannot prove either state, so only reviewed
        # selector configuration may add observation candidates for collection.
        "favorited": [],
        "reposted": [
            f"button:has-text('{FORWARD_TEXT}')",
            f"[title*='{FORWARD_TEXT}']",
            "[class*='retweet']",
        ],
    }

    def __init__(self, selector_config: dict | None = None):
        super().__init__(selector_config=selector_config)
        # A selector snapshot is read-only compatibility metadata. Official
        # mutations use the separate OAuth provider and capability attestation;
        # browser configuration can never enable click automation.
        self.REAL_ACTIONS = False
        self.STATUS = "oauth_capability_required"

    def _unsupported_interaction(self, action: str) -> UnsupportedPlatformAction:
        return UnsupportedPlatformAction(
            f"weibo_selector_mutations_disabled:{action}"
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
        raise self._unsupported_interaction("reposted")
