
from pydantic import Field
from pydantic_settings import BaseSettings



class Settings(BaseSettings):

    database_url: str = "mysql+aiomysql://user:password@mysql:3306/lottery?charset=utf8mb4"

    mysql_runtime_user: str = "dpms_runtime"

    mysql_migration_user: str = "dpms_migrate"

    redis_url: str = "redis://redis:6379/0"

    # Compose passes credentials separately so URL parsing cannot be broken by
    # punctuation in generated passwords. Empty values preserve compatibility
    # with explicitly unsecured local Redis instances outside Compose.
    redis_username: str = ""

    redis_password: str = ""

    # The control Core uses ``core``; an isolated platform runner uses a
    # platform-specific ACL identity such as ``core-bilibili``.
    redis_expected_username: str = "core"

    redis_acl_preflight_required: bool = False

    # Keep the same fail-fast floor as Worker. Core has fewer blocking readers,
    # but per-lane relays, continuity checks and metrics can burst together.
    redis_max_connections: int = Field(default=64, ge=64)

    # Redis-py otherwise permits an indefinitely stalled socket.  Blocking
    # stream reads use at most five seconds, so a fifteen-second command
    # deadline preserves their protocol while bounding a half-open connection.
    redis_socket_timeout_seconds: float = Field(default=15.0, gt=5.0, le=60.0)

    redis_connect_timeout_seconds: float = Field(default=5.0, gt=0.0, le=30.0)

    # A group with no consumer interaction for this window is reported stale.
    # Retirement remains a separate, explicit operator action.
    redis_consumer_group_stale_seconds: int = Field(
        default=900,
        ge=60,
        le=86400,
    )

    encryption_key: str = ""

    platform_security_mode: str = "compat"

    update_secret: str = "changeme"

    admin_token: str = "change-me-admin-token"

    real_run_enabled: bool = False

    # Public egress identity for Weibo OAuth mutations that require ``rip``.
    # X-Real-IP is considered only when Core's direct peer is in the explicit
    # trusted proxy CIDR list below.
    weibo_public_rip: str = ""

    weibo_trusted_proxy_cidrs: str = ""

    # Keep atomically fanning out and recovering the historical
    # ``lottery_tasks/workers`` queue until pending, lag, and undelivered
    # legacy Outbox rows all reach zero.
    legacy_task_stream_drain_enabled: bool = True

    # Probe and account-calibration compatibility queues have an independent
    # drain lifecycle from executable lottery tasks.
    legacy_control_stream_drain_enabled: bool = True

    # Sent Outbox rows are archived only when an operator has recorded a
    # global Redis continuity watermark for the stream boundary. The feature
    # is opt-in so an upgrade never archives history before its deployment
    # runbook has observed the matching Redis epoch.
    outbox_archive_enabled: bool = False

    outbox_archive_retention_seconds: int = Field(
        default=30 * 24 * 60 * 60,
        ge=3600,
        le=365 * 24 * 60 * 60,
    )

    outbox_archive_batch: int = Field(default=200, ge=1, le=5000)

    outbox_archive_interval_seconds: int = Field(
        default=3600,
        ge=60,
        le=7 * 24 * 60 * 60,
    )

    prometheus_enabled: bool = False

    # "dev" (default) only warns about weak secrets; "production" refuses to
    # start when ADMIN_TOKEN / UPDATE_SECRET / ENCRYPTION_KEY are default/unset.
    deployment_mode: str = "dev"

    serverchan_key: str = ""

    feishu_webhook: str = ""

    generic_webhook_url: str = ""

    telegram_bot_token: str = ""

    telegram_chat_id: str = ""

    cors_origins: str = "http://localhost,http://127.0.0.1,http://localhost:3000,http://127.0.0.1:3000"

    class Config:

        env_file = ".env"

        extra = "ignore"



settings = Settings()
