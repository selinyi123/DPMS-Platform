"""Weibo Action Plan policy authority."""

from .base import PlatformActionPlanContract
from .catalog import (
    WEIBO_ACTION_CAPABILITY_REQUIREMENTS,
    WEIBO_ACTION_ORDER,
    WEIBO_MANUAL_EXECUTION_BLOCKER,
    WEIBO_MANUAL_EXECUTION_PATH,
    WEIBO_MAX_UNIQUE_HANDLES,
    WEIBO_OAUTH_CAPABILITY_CONTRACT_VERSION,
    WEIBO_OAUTH_EXECUTION_PATH,
    WEIBO_RIP_ACTIONS,
    weibo_runtime_capability_requirements,
)


WEIBO_ACTION_PLAN_CONTRACT = PlatformActionPlanContract(
    platform_id="weibo",
    action_order=WEIBO_ACTION_ORDER,
    default_execution_path=WEIBO_OAUTH_EXECUTION_PATH,
    allowed_execution_paths=frozenset(
        {WEIBO_OAUTH_EXECUTION_PATH, WEIBO_MANUAL_EXECUTION_PATH}
    ),
    execution_path_error="weibo_execution_path_invalid",
    non_executable_paths=frozenset({WEIBO_MANUAL_EXECUTION_PATH}),
    allow_empty_repost_text=True,
    text_utf16_limit=140,
    unique_handle_limit=WEIBO_MAX_UNIQUE_HANDLES,
    unique_handle_limit_error="weibo_preflight_unique_handle_limit_exceeded",
    capability_execution_path=WEIBO_OAUTH_EXECUTION_PATH,
    capability_factory=weibo_runtime_capability_requirements,
    capability_error="weibo_oauth_capability_contract_mismatch",
)
