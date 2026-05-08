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
    quickchart_url: str = "https://quickchart.io"

    # ── Security ──────────────────────────────────────────────────────────
    secret_key: str

    # ── Bot webhook ───────────────────────────────────────────────────────
    webhook_base_url: str = ""

    # ── Rate limiting ─────────────────────────────────────────────────────
    rate_limit_per_second: int = 100

    # ── Retention cap ─────────────────────────────────────────────────────
    # Hard ceiling on ``ProjectSettings.retention_days`` for user-driven
    # updates (the Telegram /settings flow). ``None`` means no cap —
    # OSS / self-host users can pick any value, including 0 ("forever").
    # The cloud overlay sets this to 60 to keep the free tier from
    # accumulating unbounded raw events. When set, the value also acts
    # as the create-time default for new projects (via the cloud
    # pre-create hook); existing projects keep their stored value but
    # cannot be raised above the cap on the next user edit.
    max_retention_days: int | None = None

    # ── Sentry (optional error tracking) ──────────────────────────────────
    # Set ``SENTRY_DSN`` to enable error reporting. ``sentry-sdk`` ships as
    # a base dependency so self-hosted Docker / pip installs only need to
    # set the DSN to opt in. With the DSN unset, init is a silent no-op
    # (no network, no overhead).
    sentry_dsn: str | None = None
    sentry_environment: str | None = None
    # 0.0 disables performance tracing (errors only). Bump to e.g. 0.1 to
    # sample 10% of transactions in production.
    sentry_traces_sample_rate: float = 0.0
    # Profiles are sampled as a fraction of *traced* transactions.
    sentry_profiles_sample_rate: float = 0.0
    # Optional release identifier. Falls back to the package version
    # (defined in pyproject.toml) when unset; supply a commit SHA in CI for
    # commit-resolution and source-map links.
    sentry_release: str | None = None
    # Optional human-readable name for the running instance. Useful when
    # multiple deployments share one Sentry project (e.g. "prod-eu",
    # "staging"). Defaults to the OS hostname when unset.
    sentry_server_name: str | None = None
    # Minimum log level forwarded to Sentry as breadcrumbs. WARNING+ becomes
    # an event automatically; below that is recorded as breadcrumb context.
    sentry_log_level: str = "INFO"
    # Forward request bodies and IP addresses to Sentry. Off by default to
    # avoid leaking analytics payloads / visitor IPs to a third party.
    sentry_send_default_pii: bool = False

    # ── Redis ─────────────────────────────────────────────────────────────
    # Optional. Required only for multi-replica deployments where the daily
    # visitor-hash salt (and, in later phases, the rate limiter) must be
    # shared across processes. Single-replica self-host installs leave this
    # unset and fall back to in-process state.
    redis_url: str | None = None

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
