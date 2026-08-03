
from pydantic import BaseModel, ConfigDict, Field

from enum import StrEnum

from typing import Literal, Optional
from typing import Any

from datetime import datetime


LOTTERY_SOURCE_TYPE_MAX_LENGTH = 32
LOTTERY_SOURCE_ID_MAX_LENGTH = 64
LOTTERY_RAW_URL_MAX_LENGTH = 512
TRACKED_SOURCE_VALUE_MAX_LENGTH = 256



class AccountStatusEnum(StrEnum):

    cold = "cold"

    login_required = "login_required"

    warming = "warming"

    ready = "ready"

    executing = "executing"

    cooling = "cooling"

    frozen = "frozen"

    banned = "banned"


class TaskModeEnum(StrEnum):

    dry_run = "dry_run"

    shadow_run = "shadow_run"

    real_run = "real_run"



class AccountCreate(BaseModel):

    platform: str = "bilibili"

    fingerprint_id: Optional[int] = None

    proxy_id: Optional[int] = None

    encrypted_credential: str = Field(default="", max_length=200_000)


class AccountCredentialUpdate(BaseModel):

    encrypted_credential: str = Field(min_length=1, max_length=200_000)


class AccountProxyUpdate(BaseModel):

    proxy_id: Optional[int] = None


class QRLoginStart(BaseModel):

    platform: str = "bilibili"



class AccountUpdateStatus(BaseModel):

    target: AccountStatusEnum

    version: int


class AccountHealthRecheckRequest(BaseModel):

    cooldown_minutes: int = 15

    stale_execution_minutes: int = 10


class ProxyCreate(BaseModel):

    proxy_url: str = Field(min_length=8, max_length=2048)

    proxy_type: str = Field(default="socks5", max_length=16)

    provider: Optional[str] = Field(default=None, max_length=64)

    country: Optional[str] = Field(default=None, max_length=64)


class ProxyCooldownRequest(BaseModel):

    minutes: int = Field(default=30, ge=1, le=1440)

    reason: Optional[str] = Field(default=None, max_length=255)


class ProxyCheckRequest(BaseModel):

    target_url: str = Field(default="https://www.bilibili.com", max_length=2048)

    timeout_seconds: float = Field(default=8.0, ge=2.0, le=20.0)


class ProxyStatusUpdate(BaseModel):

    status: str


class AccountCalibrationRequest(BaseModel):

    force: bool = False


class WeiboOAuthCapabilityAttestationRequest(BaseModel):

    model_config = ConfigDict(extra="forbid")

    app_review_status: str = Field(min_length=1, max_length=16)

    client_type: str = Field(min_length=1, max_length=16)

    granted_actions: dict[str, Any]

    confirm: bool = False



class LotteryResponse(BaseModel):

    id: int

    platform: str

    source_type: str

    raw_url: str

    canonical_url: str

    title: Optional[str] = None

    rule_text: Optional[str] = None

    action_plan: Optional[dict] = None

    published_at: Optional[datetime] = None

    status: str

    value_score: int

    expires_at: Optional[datetime]



class NotifyRequest(BaseModel):

    title: str = Field(min_length=1, max_length=200)

    content: str = Field(min_length=1, max_length=5000)

    channel: str = Field(default="serverchan", max_length=32)


class NotifySecretUpdate(BaseModel):

    serverchan_key: Optional[str] = Field(default=None, max_length=512)

    feishu_webhook: Optional[str] = Field(default=None, max_length=2048)

    generic_webhook_url: Optional[str] = Field(default=None, max_length=2048)

    telegram_bot_token: Optional[str] = Field(default=None, max_length=512)

    telegram_chat_id: Optional[str] = Field(default=None, max_length=128)


class NotifySecretBundleUpdate(BaseModel):

    content: str = Field(min_length=1, max_length=10_000)


class LotteryCreate(BaseModel):

    platform: str = "bilibili"

    source_type: str = Field(
        default="manual",
        min_length=1,
        max_length=LOTTERY_SOURCE_TYPE_MAX_LENGTH,
    )

    source_id: Optional[str] = Field(
        default=None,
        max_length=LOTTERY_SOURCE_ID_MAX_LENGTH,
    )

    raw_url: str = Field(
        min_length=8,
        max_length=LOTTERY_RAW_URL_MAX_LENGTH,
    )

    canonical_url: Optional[str] = None

    value_score: int = Field(default=0, ge=0, le=100)

    expires_at: Optional[datetime] = None


class LotteryActionPlanUpdate(BaseModel):

    required_actions: list[str]

    rule_text: Optional[str] = None

    # Review is an explicit operator attestation.  Omitting the field must
    # never turn a draft into an executable plan.
    reviewed: bool = False

    # A review and a provenance attestation are deliberately separate.  A
    # legacy client may still save a draft, but it cannot accidentally certify
    # that a truncated discovery summary is the complete source rule.
    rule_complete_confirmed: bool = False

    # Keep the existing public-model default for generated clients.  The API
    # distinguishes omission via ``model_fields_set`` and selects the target
    # platform's safe default before persisting the plan.
    execution_path_id: str = Field(default="bilibili_api_v2", min_length=1, max_length=128)

    action_payloads: dict[str, dict[str, Any]] = Field(default_factory=dict)


class LotteryTargetImport(BaseModel):

    platform: str = "bilibili"

    source_type: str = Field(
        default="manual_upload",
        min_length=1,
        max_length=LOTTERY_SOURCE_TYPE_MAX_LENGTH,
    )

    source_id: Optional[str] = Field(
        default=None,
        max_length=LOTTERY_SOURCE_ID_MAX_LENGTH,
    )

    content: str = Field(min_length=1, max_length=200_000)

    value_score: int = Field(default=50, ge=0, le=100)


class TrackedSourceCreate(BaseModel):

    platform: str = "bilibili"

    source_type: str = "url_list"

    # Keep request validation aligned with tracked_sources.source_value
    # (VARCHAR(256)). Accepting a larger value here otherwise turns an
    # ordinary client error into a database failure or silent truncation.
    source_value: str = Field(
        min_length=1,
        max_length=TRACKED_SOURCE_VALUE_MAX_LENGTH,
    )

    scan_interval_minutes: int = Field(default=30, ge=1, le=1440)


class DispatchTaskRequest(BaseModel):

    account_id: Optional[int] = None

    mode: Optional[TaskModeEnum] = None

    dry_run: bool = True

    confirm: bool = False


class RealRunSettingUpdate(BaseModel):

    enabled: bool


class RuntimeRollbackRequest(BaseModel):

    reason: Optional[str] = "manual runtime rollback"


class AutopilotHeartbeatReport(BaseModel):

    """Bounded, non-secret status reported by the Autopilot process."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool

    deployment_real_run_enabled: bool

    real_run_ack_valid: bool

    platform_allowlist: list[
        Literal["bilibili", "weibo", "xiaohongshu", "douyin"]
    ] = Field(default_factory=list, max_length=4)

    platform_allowlist_valid: bool = True

    poll_interval_seconds: float = Field(ge=1, le=3600)

    round_status: Literal["ok", "error", "disabled"]

    selected: int = Field(default=0, ge=0, le=100)

    dispatched: int = Field(default=0, ge=0, le=100)

    failures: int = Field(default=0, ge=0, le=100)

    probes_requested: int = Field(default=0, ge=0, le=100)

    deferred: int = Field(default=0, ge=0, le=100)


class AdapterProbeRequest(BaseModel):

    account_id: Optional[int] = None


class AdapterSelectorConfigUpdate(BaseModel):

    config: dict[str, Any]


class LotteryResultUpdate(BaseModel):

    status: str

    note: Optional[str] = None


class ExperimentBranchCreate(BaseModel):

    key: str

    label: Optional[str] = None

    weight: float = 1.0

    config: Optional[dict[str, Any]] = None


class ExperimentCreate(BaseModel):

    name: str

    platform: str = "bilibili"

    mode: TaskModeEnum = TaskModeEnum.shadow_run

    hypothesis: Optional[str] = None

    branches: list[ExperimentBranchCreate]

    allow_real_run: bool = False


class ExperimentAssignRequest(BaseModel):

    lottery_id: int

    task_id: Optional[str] = None

    account_id: Optional[int] = None


class ExperimentStopRequest(BaseModel):

    reason: Optional[str] = "manual stop"


class ExperimentBranchStopRequest(BaseModel):

    reason: Optional[str] = "manual branch stop"


class PolicyPublishRequest(BaseModel):

    policy_key: str = "real_run_gate"

    definition: dict[str, Any]

    reason_code: str

    rollback_condition: str

    loosening_justification: Optional[str] = None

    note: Optional[str] = None


class PolicyActivateRequest(BaseModel):

    note: Optional[str] = None
