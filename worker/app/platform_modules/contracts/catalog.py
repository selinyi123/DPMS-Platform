"""Boot-safe metadata for Worker Action Plan contracts.

The executable validation policies remain in one module per platform.  This
catalog contains only immutable identifiers/constants so importing the shared
Worker shell cannot execute every platform contract.
"""

from __future__ import annotations

from shared.douyin_device_contract import (
    DOUYIN_DEVICE_ACTION_ORDER,
    DOUYIN_DEVICE_EXECUTION_PATH,
)


LEGACY_ACTION_ORDER = ("followed", "liked", "commented", "reposted")
ACTION_ORDER = ("followed", "liked", "commented", "favorited", "reposted")

BILIBILI_ACTION_ORDER = LEGACY_ACTION_ORDER
BILIBILI_API_EXECUTION_PATH = "bilibili_api_v2"
BILIBILI_API_PREFLIGHT_CONTRACT_VERSION = 1
BILIBILI_PREFLIGHT_CONTRACT_VERSION = (
    BILIBILI_API_PREFLIGHT_CONTRACT_VERSION
)

WEIBO_ACTION_ORDER = (
    "followed",
    "liked",
    "commented",
    "favorited",
    "reposted",
)
WEIBO_MAX_UNIQUE_HANDLES = 32
WEIBO_OAUTH_EXECUTION_PATH = "weibo_oauth_v1"
WEIBO_MANUAL_EXECUTION_PATH = "weibo_manual_v1"
WEIBO_MANUAL_EXECUTION_BLOCKER = "weibo_manual_execution_selected"
WEIBO_OAUTH_CAPABILITY_CONTRACT_VERSION = 1
WEIBO_ACTION_CAPABILITY_REQUIREMENTS = {
    "followed": {
        "endpoint": "friendships/create",
        "permission": "advanced",
        "client_type": "weibo",
    },
    "liked": {"endpoint": "attitudes/create", "permission": "advanced"},
    "commented": {"endpoint": "comments/create", "permission": "standard"},
    "favorited": {"endpoint": "favorites/create", "permission": "standard"},
    "reposted": {"endpoint": "statuses/repost", "permission": "standard"},
}
WEIBO_RIP_ACTIONS = frozenset({"followed", "commented", "reposted"})

XIAOHONGSHU_ACTION_ORDER = (
    "followed",
    "liked",
    "commented",
    "favorited",
)
XIAOHONGSHU_REQUIRED_ACTIONS = XIAOHONGSHU_ACTION_ORDER
XIAOHONGSHU_MANUAL_EXECUTION_PATH = "xiaohongshu_manual_v1"
XIAOHONGSHU_BROWSER_EXECUTION_PATH = "xiaohongshu_browser_v1"
XIAOHONGSHU_NO_OFFICIAL_API_BLOCKER = (
    "xiaohongshu_no_official_interaction_api"
)

DOUYIN_ACTION_ORDER = DOUYIN_DEVICE_ACTION_ORDER
DOUYIN_MANUAL_EXECUTION_PATH = "douyin_manual_v1"
DOUYIN_NO_OFFICIAL_API_BLOCKER = "douyin_no_official_interaction_api"


def weibo_runtime_capability_requirements(
    required_actions: tuple[str, ...],
) -> dict:
    selected = set(required_actions)
    return {
        "contract_version": WEIBO_OAUTH_CAPABILITY_CONTRACT_VERSION,
        "actions": {
            action: dict(WEIBO_ACTION_CAPABILITY_REQUIREMENTS[action])
            for action in WEIBO_ACTION_ORDER
            if action in selected
        },
    }
