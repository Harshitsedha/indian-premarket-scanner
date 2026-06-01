from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

# Central model name — change here to upgrade across the whole pipeline
CLAUDE_MODEL = "claude-sonnet-4-6"

# Absolute path to .env at project root — works regardless of cwd
_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_ENV_PATH),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Anthropic
    anthropic_api_key: str

    # Upstox
    upstox_api_key: str
    upstox_api_secret: str
    upstox_access_token: str = ""
    upstox_refresh_token: str = ""
    upstox_extended_token: str = ""
    # Port 8765 is dedicated to the one-time OAuth callback server (upstox_auth.py)
    # so it never conflicts with the API server on port 8000.
    upstox_redirect_uri: str = "http://localhost:8765/callback"

    # Telegram
    telegram_bot_token: str
    telegram_chat_id: str

    # Healthchecks
    healthcheck_scraper_url: str = ""
    healthcheck_analyser_url: str = ""
    healthcheck_upstox_refresh_url: str = ""

    # Database
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "premarket"
    postgres_user: str = "premarket"
    postgres_password: str

    # Redis
    redis_host: str = "localhost"
    redis_port: int = 6379

    # App
    app_env: str = "development"
    log_level: str = "INFO"
    api_port: int = 8000
    dashboard_port: int = 3000

    @property
    def postgres_dsn(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:"
            f"{self.postgres_password}@{self.postgres_host}:"
            f"{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def redis_url(self) -> str:
        return f"redis://{self.redis_host}:{self.redis_port}"


# singleton — import this everywhere
settings = Settings()