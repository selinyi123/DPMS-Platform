
from pydantic_settings import BaseSettings



class Settings(BaseSettings):

    database_url: str = "mysql+aiomysql://user:password@mysql:3306/lottery?charset=utf8mb4"

    redis_url: str = "redis://redis:6379/0"

    encryption_key: str = ""

    update_secret: str = "changeme"

    admin_token: str = "change-me-admin-token"

    real_run_enabled: bool = False

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
