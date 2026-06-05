
from pydantic_settings import BaseSettings



class Settings(BaseSettings):

    database_url: str = "mysql+aiomysql://user:password@mysql:3306/lottery?charset=utf8mb4"

    redis_url: str = "redis://redis:6379/0"

    encryption_key: str = ""

    worker_max_browsers: int = 1

    class Config:

        env_file = ".env"

        extra = "ignore"



settings = Settings()
