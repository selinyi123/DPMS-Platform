"""Xiaohongshu Action Plan policy authority."""

from .base import PlatformActionPlanContract
from .catalog import (
    XIAOHONGSHU_ACTION_ORDER,
    XIAOHONGSHU_BROWSER_EXECUTION_PATH,
    XIAOHONGSHU_MANUAL_EXECUTION_PATH,
    XIAOHONGSHU_NO_OFFICIAL_API_BLOCKER,
    XIAOHONGSHU_REQUIRED_ACTIONS,
)

XIAOHONGSHU_ACTION_PLAN_CONTRACT = PlatformActionPlanContract(
    platform_id="xiaohongshu",
    action_order=XIAOHONGSHU_ACTION_ORDER,
    default_execution_path=XIAOHONGSHU_BROWSER_EXECUTION_PATH,
    allowed_execution_paths=frozenset(
        {
            XIAOHONGSHU_MANUAL_EXECUTION_PATH,
            XIAOHONGSHU_BROWSER_EXECUTION_PATH,
        }
    ),
    execution_path_error="xiaohongshu_execution_path_not_supported",
    plan_must_be_non_executable=False,
    non_executable_paths=frozenset({XIAOHONGSHU_MANUAL_EXECUTION_PATH}),
    requires_empty_repost_content=True,
    empty_repost_content_error="xiaohongshu_repost_content_not_supported",
)
