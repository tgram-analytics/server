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

    # ── Security ──────────────────────────────────────────────────────────
    secret_key: str

    # ── Bot webhook ───────────────────────────────────────────────────────
    webhook_base_url: str = ""

    # ── Rate limiting ─────────────────────────────────────────────────────
    rate_limit_per_second: int = 100

    # ── Request-body size limit ───────────────────────────────────────────
    # Hard cap (bytes) on request bodies. Enforced by an ASGI middleware via
    # the Content-Length header before the body is read, so oversized payloads
    # are rejected with 413 without ever being materialized in RAM.
    max_request_body_bytes: int = 1_048_576  # 1 MB

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

    # ── MCP server ────────────────────────────────────────────────────────
    # The MCP surface is mounted at /mcp when enabled. On a fresh install
    # it is inert until an access token is created via /mcp_token — every
    # request 401s with no token rows in the DB.
    mcp_enabled: bool = True
    # Externally-reachable base URL for the MCP server. Used for the
    # OAuth-style issuer/resource metadata and Origin/Host allow-lists.
    # Empty ⇒ fall back to webhook_base_url, then http://localhost:8000.
    mcp_public_url: str = ""
    # Extra allowed browser Origins for the MCP endpoint (DNS-rebinding
    # protection). The effective public URL is always included.
    mcp_allowed_origins: list[str] = []
    # Optional GitHub token to raise the raw.githubusercontent.com rate
    # limit for the docs-federation fetcher (60/hr anon → 5000/hr).
    mcp_github_token: str | None = None
    # Browser OAuth for MCP clients that cannot send custom headers
    # (Claude Desktop). Self-host only: mounted when the default
    # StaticTokenVerifier is in use; a plugin-registered verifier
    # (cloud overlay) supplies its own OAuth and this flag is inert.
    mcp_oauth_enabled: bool = True

    @property
    def mcp_effective_public_url(self) -> str:
        """Resolved public base URL for MCP metadata and allow-lists."""
        return (
            self.mcp_public_url.rstrip("/")
            or self.webhook_base_url.rstrip("/")
            or "http://localhost:8000"
        )

    @property
    def mcp_canonical_resource_uri(self) -> str:
        """RFC 8707 canonical resource URI advertised to MCP clients."""
        return f"{self.mcp_effective_public_url}/mcp"

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
