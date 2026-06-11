from app.adapters.selector_flow import SelectorFlowAdapter


FOLLOW_TEXT = "关注"
LIKE_TEXT = "赞"
COMMENT_TEXT = "评论"
FORWARD_TEXT = "转发"
PUBLISH_TEXT = "发布"


class WeiboAdapter(SelectorFlowAdapter):
    PLATFORM = "weibo"
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
        "reposted": [
            f"button:has-text('{FORWARD_TEXT}')",
            f"[title*='{FORWARD_TEXT}']",
            "[class*='retweet']",
        ],
    }
