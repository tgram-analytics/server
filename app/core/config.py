"""Application configuration loaded from environment variables."""

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All configuration values required to run the server.

    Values are loaded from environment variables (case-insensitive) or a
    `.env` file in the working directory.  Missing required fields raise a
    ``ValidationError`` at import time, so misconfiguration is caught before
    the server starts accepting traffic.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",  # ignore postgres_*, etc. from docker-compose .env
    )

    # ── Telegram ──────────────────────────────────────────────────────────
    telegram_bot_token: str
    admin_chat_id: int

    # ── Database ──────────────────────────────────────────────────────────
    database_url: str

    # ── External services ─────────────────────────────────────────────────
    quickchart_url: str = "http://quickchart:3400"

    # ── Security ──────────────────────────────────────────────────────────
    secret_key: str

    # ── Bot webhook ───────────────────────────────────────────────────────
    webhook_base_url: str = ""

    # ── Rate limiting ─────────────────────────────────────────────────────
    rate_limit_per_second: int = 100

    @field_validator("telegram_bot_token")
    @classmethod
    def token_must_not_be_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("TELEGRAM_BOT_TOKEN must not be empty")
        return v

    @field_validator("database_url")
    @classmethod
    def database_url_must_not_be_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("DATABASE_URL must not be empty")
        return v


def get_settings() -> Settings:
    """Return a cached Settings instance.

    Raises ``ValidationError`` (pydantic) if required variables are missing.
    """
    return Settings()  # type: ignore[call-arg]
