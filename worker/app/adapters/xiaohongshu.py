from app.adapters.base import UnsupportedPlatformAction
from collections.abc import Iterable

from app.adapters.selector_flow import SelectorFlowAdapter, selector_list


FOLLOW_TEXT = "关注"
LIKE_TEXT = "点赞"
COMMENT_TEXT = "评论"
SEND_TEXT = "发送"
FAVORITE_TEXT = "收藏"


class XiaohongshuAdapter(SelectorFlowAdapter):
    PLATFORM = "xiaohongshu"
    ACTIONS = ("followed", "liked", "commented", "favorited")
    STATUS = "calibration_required"
    CAPABILITY_BLOCK_REASON = "xiaohongshu_no_official_interaction_api"
    MANUAL_CONFIRMATION_REQUIRED = True
    OFFICIAL_INTERACTION_API_AVAILABLE = False
    DURABLE_INTENTS_REQUIRED = True
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
        self._reviewed_comment_text: str | None = None
        self.REAL_ACTIONS = self.supports_actions(self.ACTIONS)
        self.STATUS = "configured" if self.REAL_ACTIONS else "calibration_required"

    def supports_actions(self, actions: Iterable[str]) -> bool:
        """Require explicit mutation and read-back selectors for every action."""

        selected = tuple(actions)
        if not selected or any(action not in self.ACTIONS for action in selected):
            return False
        return all(self._action_selectors_complete(action) for action in selected)

    def _action_selectors_complete(self, action: str) -> bool:
        config = self.configured_selectors.get(action)
        if not isinstance(config, dict):
            return False
        done = selector_list(config.get("done"))
        if action == "commented":
            return bool(
                selector_list(config.get("input"))
                and selector_list(config.get("submit"))
                and done
            )
        return bool(selector_list(config.get("click")) and done)

    def bind_reviewed_comment_text(self, text: str) -> None:
        """Bind the exact reviewed payload before any browser interaction."""

        if not isinstance(text, str) or not text.strip():
            raise UnsupportedPlatformAction(
                "xiaohongshu_reviewed_comment_text_required"
            )
        configured = self.configured_selectors.get("commented")
        if isinstance(configured, dict) and "text" in configured:
            if configured.get("text") != text:
                raise UnsupportedPlatformAction(
                    "xiaohongshu_comment_text_binding_mismatch"
                )
        self._reviewed_comment_text = text

    def _comment_text(self, config: dict) -> str:
        text = self._reviewed_comment_text
        if text is None:
            raise UnsupportedPlatformAction(
                "xiaohongshu_reviewed_comment_text_required"
            )
        if "text" in config and config.get("text") != text:
            raise UnsupportedPlatformAction(
                "xiaohongshu_comment_text_binding_mismatch"
            )
        return text

    def _unsupported_interaction(self, action: str) -> UnsupportedPlatformAction:
        return UnsupportedPlatformAction(
            f"{self.CAPABILITY_BLOCK_REASON}:{action}"
        )

    async def _repost(self, page):
        # Xiaohongshu's reviewed contract never permits repost/share.
        raise self._unsupported_interaction("reposted")
