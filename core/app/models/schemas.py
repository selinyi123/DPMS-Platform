
from pydantic import BaseModel

from enum import StrEnum

from typing import Optional
from typing import Any

from datetime import datetime



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

    encrypted_credential: str = ""


class AccountCredentialUpdate(BaseModel):

    encrypted_credential: str


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

    proxy_url: str

    proxy_type: str = "socks5"

    provider: Optional[str] = None

    country: Optional[str] = None


class ProxyCooldownRequest(BaseModel):

    minutes: int = 30

    reason: Optional[str] = None


class ProxyCheckRequest(BaseModel):

    target_url: str = "https://www.bilibili.com"

    timeout_seconds: float = 8.0


class ProxyStatusUpdate(BaseModel):

    status: str


class AccountCalibrationRequest(BaseModel):

    force: bool = False



class LotteryResponse(BaseModel):

    id: int

    platform: str

    source_type: str

    raw_url: str

    canonical_url: str

    status: str

    value_score: int

    expires_at: Optional[datetime]



class NotifyRequest(BaseModel):

    title: str

    content: str

    channel: str = "serverchan"


class NotifySecretUpdate(BaseModel):

    serverchan_key: Optional[str] = None

    feishu_webhook: Optional[str] = None

    generic_webhook_url: Optional[str] = None

    telegram_bot_token: Optional[str] = None

    telegram_chat_id: Optional[str] = None


class NotifySecretBundleUpdate(BaseModel):

    content: str


class LotteryCreate(BaseModel):

    platform: str = "bilibili"

    source_type: str = "manual"

    source_id: Optional[str] = None

    raw_url: str

    canonical_url: Optional[str] = None

    value_score: int = 0

    expires_at: Optional[datetime] = None


class LotteryTargetImport(BaseModel):

    platform: str = "bilibili"

    source_type: str = "manual_upload"

    source_id: Optional[str] = None

    content: str

    value_score: int = 50


class TrackedSourceCreate(BaseModel):

    platform: str = "bilibili"

    source_type: str = "url_list"

    source_value: str

    scan_interval_minutes: int = 30


class DispatchTaskRequest(BaseModel):

    account_id: Optional[int] = None

    mode: Optional[TaskModeEnum] = None

    dry_run: bool = True

    confirm: bool = False


class RealRunSettingUpdate(BaseModel):

    enabled: bool


class RuntimeRollbackRequest(BaseModel):

    reason: Optional[str] = "manual runtime rollback"


class AdapterProbeRequest(BaseModel):

    account_id: Optional[int] = None


class AdapterSelectorConfigUpdate(BaseModel):

    config: dict[str, Any]


class LotteryResultUpdate(BaseModel):

    status: str

    note: Optional[str] = None
