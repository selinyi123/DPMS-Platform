from app.adapters.base import UnsupportedPlatformAction
from app.adapters.selector_flow import SelectorFlowAdapter, selector_list


FOLLOW_TEXT = "关注"
LIKE_TEXT = "点赞"
COMMENT_TEXT = "评论"
PUBLISH_TEXT = "发布"


class DouyinAdapter(SelectorFlowAdapter):
    PLATFORM = "douyin"
    ACTIONS = ("followed", "liked", "commented", "favorited", "reposted")
    STATUS = "manual_only"
    CAPABILITY_BLOCK_REASON = "douyin_no_official_interaction_api"
    MANUAL_CONFIRMATION_REQUIRED = True
    OFFICIAL_INTERACTION_API_AVAILABLE = False
    DEFAULT_SELECTOR_PROBES = {
        "followed": [
            "[data-e2e='follow-icon']",
            f"button:has-text('{FOLLOW_TEXT}')",
        ],
        "liked": [
            "[data-e2e='like-icon']",
            f"[aria-label*='{LIKE_TEXT}']",
        ],
        "commented": [
            "[data-e2e='comment-input']",
            "[contenteditable='true'][role='textbox']",
            f"[placeholder*='{COMMENT_TEXT}']",
            f"button:has-text('{PUBLISH_TEXT}')",
        ],
        # There is deliberately no generic ``share-icon`` probe here. Seeing
        # a share entry point does not prove either collection or repost, and
        # treating it as either would make a Shadow observation semantically
        # false. Reviewed ``done`` selectors may observe the two states below
        # independently without clicking anything.
        "favorited": [],
        "reposted": [],
    }

    def __init__(self, selector_config: dict | None = None):
        super().__init__(selector_config=selector_config)
        # Selector presence is observation metadata only. It can never turn
        # an unsupported browser automation path into an executable API.
        self.REAL_ACTIONS = False
        self.STATUS = "manual_only"

        # For collection and repost, only an explicit read-back state is safe
        # to probe. Click/share controls are not interchangeable evidence.
        for phase in ("favorited", "reposted"):
            config = self.configured_selectors.get(phase)
            if isinstance(config, dict):
                self.SELECTOR_PROBES[phase] = selector_list(
                    config.get("done") or config.get("success")
                )
            else:
                self.SELECTOR_PROBES[phase] = []

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
        raise self._unsupported_interaction("reposted")
