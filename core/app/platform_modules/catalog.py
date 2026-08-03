"""Boot-safe catalog for installed lottery platform modules.

Only immutable identifiers and compatibility constants live here.  Runtime
business handlers are imported by :mod:`app.platform_modules.registry` on
demand, so a broken optional platform module cannot prevent Core from
starting or serving the other platforms.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from shared.xiaohongshu_browser_contract import (
    XIAOHONGSHU_BROWSER_ACTION_ORDER,
    XIAOHONGSHU_BROWSER_EXECUTION_PATH,
)
from shared.douyin_device_contract import (
    DOUYIN_DEVICE_ACTION_ORDER,
    DOUYIN_DEVICE_EXECUTION_PATH,
)


@dataclass(frozen=True)
class PlatformModuleSpec:
    platform_id: str
    module_name: str
    export_name: str
    canonical_hosts: frozenset[str]
    discovery_source_types: frozenset[str]
    action_order: tuple[str, ...]
    default_execution_path_id: str
    real_adapter_kinds: frozenset[str]
    configuration_kind: str
    real_run_supported: bool
    real_run_blocker: str | None = None


BILIBILI_ACTION_ORDER = ("followed", "liked", "commented", "reposted")
BILIBILI_API_EXECUTION_PATH = "bilibili_api_v2"
BILIBILI_COLLECTION_RUN_BUDGET = 80
BILIBILI_KEYWORD_SEARCH_CALL_RUN_BUDGET = 40
BILIBILI_KEYWORD_SOURCE_QUERY_LIMIT = 8
BILIBILI_KEYWORD_QUERY_MAX_CHARS = 64
BILIBILI_DYNAMIC_ID_PATTERN = re.compile(r"[0-9]{1,20}", re.ASCII)
BILIBILI_VIDEO_ID_PATTERN = re.compile(
    r"(?:BV[0-9A-Za-z]+|av[0-9]+)",
    re.IGNORECASE | re.ASCII,
)

WEIBO_ACTION_ORDER = (
    "followed",
    "liked",
    "commented",
    "favorited",
    "reposted",
)
WEIBO_OAUTH_EXECUTION_PATH = "weibo_oauth_v1"
WEIBO_MANUAL_EXECUTION_PATH = "weibo_manual_v1"
WEIBO_MANUAL_EXECUTION_BLOCKER = "weibo_manual_execution_selected"
WEIBO_OAUTH_CAPABILITY_CONTRACT_VERSION = 1
WEIBO_MBLOGID_PATTERN = re.compile(r"(?=.*[A-Za-z])[A-Za-z0-9]{6,16}")
WEIBO_MID_PATTERN = re.compile(r"[1-9][0-9]{0,18}")
WEIBO_UID_PATTERN = re.compile(r"[0-9]{1,20}", re.ASCII)
WEIBO_MID_MAX = 2**63 - 1
WEIBO_ACTION_CAPABILITY_REQUIREMENTS = MappingProxyType(
    {
        "followed": MappingProxyType(
            {
                "endpoint": "friendships/create",
                "permission": "advanced",
                "client_type": "weibo",
            }
        ),
        "liked": MappingProxyType(
            {"endpoint": "attitudes/create", "permission": "advanced"}
        ),
        "commented": MappingProxyType(
            {"endpoint": "comments/create", "permission": "standard"}
        ),
        "favorited": MappingProxyType(
            {"endpoint": "favorites/create", "permission": "standard"}
        ),
        "reposted": MappingProxyType(
            {"endpoint": "statuses/repost", "permission": "standard"}
        ),
    }
)

XIAOHONGSHU_ACTION_ORDER = XIAOHONGSHU_BROWSER_ACTION_ORDER
XIAOHONGSHU_MANUAL_EXECUTION_PATH = "xiaohongshu_manual_v1"
XIAOHONGSHU_NO_OFFICIAL_API_BLOCKER = (
    "xiaohongshu_no_official_interaction_api"
)
XIAOHONGSHU_MANUAL_EXECUTION_BLOCKER = (
    "xiaohongshu_manual_execution_selected"
)
XIAOHONGSHU_BROWSER_EVIDENCE_BLOCKER = (
    "xiaohongshu_exact_browser_evidence_required"
)
XIAOHONGSHU_NOTE_PATTERN = re.compile(r"[0-9a-fA-F]{24}")

DOUYIN_ACTION_ORDER = DOUYIN_DEVICE_ACTION_ORDER
DOUYIN_MANUAL_EXECUTION_PATH = "douyin_manual_v1"
DOUYIN_NO_OFFICIAL_API_BLOCKER = "douyin_no_official_interaction_api"
DOUYIN_DEVICE_EVIDENCE_BLOCKER = "douyin_exact_device_evidence_required"
DOUYIN_VIDEO_ID_PATTERN = re.compile(r"[0-9]{8,32}", re.ASCII)
DOUYIN_NOTE_ID_PATTERN = re.compile(r"[0-9]{19}", re.ASCII)


def is_weibo_status_id(value: str) -> bool:
    if WEIBO_MBLOGID_PATTERN.fullmatch(value):
        return True
    if not WEIBO_MID_PATTERN.fullmatch(value):
        return False
    return int(value) <= WEIBO_MID_MAX


def bilibili_keyword_tokens(value: str) -> list[str]:
    return [
        part.strip()
        for part in re.split(r"[\n,\uFF0C;\uFF1B]+", str(value or ""))
        if part.strip()
    ]


def split_bilibili_keywords(value: str) -> list[str]:
    """Parse the persisted Bilibili keyword contract without loading runtime code."""

    keywords: list[str] = []
    seen: set[str] = set()
    for keyword in bilibili_keyword_tokens(value):
        if not keyword or len(keyword) > BILIBILI_KEYWORD_QUERY_MAX_CHARS:
            continue
        dedupe_key = keyword.casefold()
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        keywords.append(keyword)
        if len(keywords) >= BILIBILI_KEYWORD_SOURCE_QUERY_LIMIT:
            break
    return keywords


class AttemptBudget:
    """A per-run counter that charges failed as well as successful attempts."""

    def __init__(self, limit: int):
        self.limit = max(0, int(limit))
        self.consumed = 0

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.consumed)

    def consume(self) -> bool:
        if self.remaining <= 0:
            return False
        self.consumed += 1
        return True


class ExpansionBudget(AttemptBudget):
    """Bound untrusted collection-source expansion in one discovery run."""


class KeywordSearchCallBudget(AttemptBudget):
    """Bound Bilibili remote keyword-search calls in one discovery run."""


PLATFORM_MODULE_SPECS: Mapping[str, PlatformModuleSpec] = MappingProxyType(
    {
        "bilibili": PlatformModuleSpec(
            platform_id="bilibili",
            module_name="app.platform_modules.bilibili",
            export_name="BILIBILI_PLATFORM",
            canonical_hosts=frozenset(
                {
                    "b23.tv",
                    "t.bilibili.com",
                    "bilibili.com",
                    "www.bilibili.com",
                    "m.bilibili.com",
                }
            ),
            discovery_source_types=frozenset({"url_list", "keyword", "up"}),
            action_order=BILIBILI_ACTION_ORDER,
            default_execution_path_id=BILIBILI_API_EXECUTION_PATH,
            real_adapter_kinds=frozenset({"api"}),
            configuration_kind="execution",
            real_run_supported=True,
        ),
        "weibo": PlatformModuleSpec(
            platform_id="weibo",
            module_name="app.platform_modules.weibo",
            export_name="WEIBO_PLATFORM",
            canonical_hosts=frozenset(
                {"t.cn", "m.weibo.cn", "weibo.com", "www.weibo.com"}
            ),
            discovery_source_types=frozenset({"url_list"}),
            action_order=WEIBO_ACTION_ORDER,
            default_execution_path_id=WEIBO_OAUTH_EXECUTION_PATH,
            real_adapter_kinds=frozenset({"oauth"}),
            configuration_kind="observation",
            real_run_supported=True,
            real_run_blocker="weibo_oauth_capability_evidence_required",
        ),
        "douyin": PlatformModuleSpec(
            platform_id="douyin",
            module_name="app.platform_modules.douyin",
            export_name="DOUYIN_PLATFORM",
            canonical_hosts=frozenset(
                {
                    "v.douyin.com",
                    "douyin.com",
                    "www.douyin.com",
                    "www.iesdouyin.com",
                }
            ),
            discovery_source_types=frozenset({"url_list"}),
            action_order=DOUYIN_ACTION_ORDER,
            default_execution_path_id=DOUYIN_DEVICE_EXECUTION_PATH,
            real_adapter_kinds=frozenset({"device_agent"}),
            configuration_kind="execution",
            real_run_supported=True,
            real_run_blocker=DOUYIN_DEVICE_EVIDENCE_BLOCKER,
        ),
        "xiaohongshu": PlatformModuleSpec(
            platform_id="xiaohongshu",
            module_name="app.platform_modules.xiaohongshu",
            export_name="XIAOHONGSHU_PLATFORM",
            canonical_hosts=frozenset(
                {
                    "xhslink.com",
                    "xiaohongshu.com",
                    "www.xiaohongshu.com",
                }
            ),
            discovery_source_types=frozenset({"url_list"}),
            action_order=XIAOHONGSHU_ACTION_ORDER,
            default_execution_path_id=XIAOHONGSHU_BROWSER_EXECUTION_PATH,
            real_adapter_kinds=frozenset({"selector"}),
            configuration_kind="execution",
            real_run_supported=True,
            real_run_blocker=XIAOHONGSHU_BROWSER_EVIDENCE_BLOCKER,
        ),
    }
)


def platform_module_spec(platform: str) -> PlatformModuleSpec | None:
    return PLATFORM_MODULE_SPECS.get(str(platform or "").strip().casefold())


def registered_platforms() -> tuple[str, ...]:
    return tuple(PLATFORM_MODULE_SPECS)
