"""Bilibili Action Plan policy authority."""

from .base import PlatformActionPlanContract
from .catalog import (
    BILIBILI_ACTION_ORDER,
    BILIBILI_API_EXECUTION_PATH,
    BILIBILI_API_PREFLIGHT_CONTRACT_VERSION,
    BILIBILI_PREFLIGHT_CONTRACT_VERSION,
)

BILIBILI_ACTION_PLAN_CONTRACT = PlatformActionPlanContract(
    platform_id="bilibili",
    action_order=BILIBILI_ACTION_ORDER,
    default_execution_path=BILIBILI_API_EXECUTION_PATH,
    allowed_execution_paths=frozenset({BILIBILI_API_EXECUTION_PATH}),
    execution_path_error="bilibili_execution_path_not_supported",
)
