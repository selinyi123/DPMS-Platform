from app.adapters.selector_flow import SelectorFlowAdapter


FOLLOW_TEXT = "关注"
LIKE_TEXT = "点赞"
COMMENT_TEXT = "评论"
SEND_TEXT = "发送"
SHARE_TEXT = "分享"


class XiaohongshuAdapter(SelectorFlowAdapter):
    PLATFORM = "xiaohongshu"
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
        "reposted": [
            "[class*='share-wrapper']",
            f"button:has-text('{SHARE_TEXT}')",
            "[class*='share']",
        ],
    }
