from app.adapters.selector_flow import SelectorFlowAdapter


FOLLOW_TEXT = "关注"
LIKE_TEXT = "赞"
COMMENT_TEXT = "评论"
PUBLISH_TEXT = "发布"
SHARE_TEXT = "分享"


class DouyinAdapter(SelectorFlowAdapter):
    PLATFORM = "douyin"
    DEFAULT_SELECTOR_PROBES = {
        "followed": [
            f"button:has-text('{FOLLOW_TEXT}')",
            "[data-e2e='follow-icon']",
            f"[class*='follow']:has-text('{FOLLOW_TEXT}')",
        ],
        "liked": [
            "[data-e2e='like-icon']",
            f"[aria-label*='{LIKE_TEXT}']",
            "[class*='like']",
        ],
        "commented": [
            "[data-e2e='comment-input']",
            "[contenteditable='true']",
            f"[placeholder*='{COMMENT_TEXT}']",
            f"button:has-text('{PUBLISH_TEXT}')",
        ],
        "reposted": [
            "[data-e2e='share-icon']",
            f"button:has-text('{SHARE_TEXT}')",
            "[class*='share']",
        ],
    }
