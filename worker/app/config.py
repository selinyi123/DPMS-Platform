
from pydantic import Field
from pydantic_settings import BaseSettings



class Settings(BaseSettings):

    database_url: str = "mysql+aiomysql://user:password@mysql:3306/lottery?charset=utf8mb4"

    mysql_runtime_user: str = "dpms_runtime"

    redis_url: str = "redis://redis:6379/0"

    redis_username: str = ""

    redis_password: str = ""

    # The control Worker uses ``worker``; an isolated platform Worker uses a
    # platform-specific ACL identity such as ``worker-bilibili``.
    redis_expected_username: str = "worker"

    redis_acl_preflight_required: bool = False

    # Steady state already owns about twenty connections (8 task readers,
    # 4 probes, 4 calibrators, 2 legacy fanouts, login and pubsub). Pending
    # refresh/recovery and execution writes need independent burst capacity;
    # fail fast instead of allowing a too-small pool to flap arbitrary lanes.
    redis_max_connections: int = Field(default=64, ge=64)

    # Account/probe/login readers block for up to five seconds.  Keep the
    # socket deadline above that value while ensuring a half-open connection
    # cannot stall one platform lane forever.
    redis_socket_timeout_seconds: float = Field(default=15.0, gt=5.0, le=60.0)

    redis_connect_timeout_seconds: float = Field(default=5.0, gt=0.0, le=30.0)

    # Keep Worker metadata retirement on the same clock as Core governance.
    # A stopped, zero-pending consumer should not remain visible as stale for
    # days after Core has already raised the operator warning.
    redis_consumer_group_stale_seconds: int = Field(
        default=900,
        ge=60,
        le=86400,
    )

    encryption_key: str = ""

    platform_security_mode: str = "compat"

    worker_max_browsers: int = 1

    legacy_control_stream_drain_enabled: bool = True

    deployment_mode: str = "dev"

    class Config:

        env_file = ".env"

        extra = "ignore"



settings = Settings()
