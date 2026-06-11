from app.adapters.base import BaseAdapter
from app.adapters.bilibili import BilibiliAdapter
from app.adapters.douyin import DouyinAdapter
from app.adapters.weibo import WeiboAdapter
from app.adapters.xiaohongshu import XiaohongshuAdapter


class UnsupportedAdapter(BaseAdapter):
    def __init__(self, platform: str):
        self.PLATFORM = platform


ADAPTERS = {
    "bilibili": BilibiliAdapter,
    "weibo": WeiboAdapter,
    "douyin": DouyinAdapter,
    "xiaohongshu": XiaohongshuAdapter,
}


def get_adapter(platform: str, selector_config: dict | None = None):
    adapter_cls = ADAPTERS.get(platform)
    if adapter_cls:
        try:
            return adapter_cls(selector_config=selector_config)
        except TypeError:
            return adapter_cls()
    return UnsupportedAdapter(platform)
