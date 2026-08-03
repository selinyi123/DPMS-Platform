"""Douyin Action Plan policy authority."""

from .base import PlatformActionPlanContract
from .catalog import (
    DOUYIN_ACTION_ORDER,
    DOUYIN_DEVICE_EXECUTION_PATH,
    DOUYIN_MANUAL_EXECUTION_PATH,
)

DOUYIN_ACTION_PLAN_CONTRACT = PlatformActionPlanContract(
    platform_id="douyin",
    action_order=DOUYIN_ACTION_ORDER,
    default_execution_path=DOUYIN_DEVICE_EXECUTION_PATH,
    allowed_execution_paths=frozenset(
        {DOUYIN_MANUAL_EXECUTION_PATH, DOUYIN_DEVICE_EXECUTION_PATH}
    ),
    execution_path_error="douyin_execution_path_invalid",
    plan_must_be_non_executable=False,
    non_executable_paths=frozenset({DOUYIN_MANUAL_EXECUTION_PATH}),
    allow_empty_repost_text=True,
)
