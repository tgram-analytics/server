"""MCP-related Settings fields exist with self-host-friendly defaults."""

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def _settings(**overrides) -> Settings:
    base = dict(
        telegram_bot_token="123:abc",
        admin_chat_id=1,
        database_url="sqlite+aiosqlite://",
        secret_key="x" * 32,
        # Pin explicitly so an ambient WEBHOOK_BASE_URL leaked into
        # os.environ by an earlier test can't bleed into these cases —
        # explicit init kwargs outrank environment in pydantic-settings.
        webhook_base_url="",
    )
    base.update(overrides)
    return Settings(_env_file=None, **base)


def test_mcp_defaults():
    s = _settings()
    assert s.mcp_enabled is True
    assert s.mcp_public_url == ""
    assert s.mcp_allowed_origins == []
    assert s.mcp_github_token is None


def test_mcp_effective_public_url_falls_back_to_webhook_base():
    s = _settings(webhook_base_url="https://tga.example.com")
    assert s.mcp_effective_public_url == "https://tga.example.com"


def test_mcp_effective_public_url_prefers_explicit():
    s = _settings(
        webhook_base_url="https://tga.example.com",
        mcp_public_url="https://mcp.example.com",
    )
    assert s.mcp_effective_public_url == "https://mcp.example.com"


def test_mcp_effective_public_url_localhost_default():
    s = _settings()
    assert s.mcp_effective_public_url == "http://localhost:8000"


def test_webhook_secret_empty_is_allowed():
    # Long-polling deployments never set it; the route is fail-closed and
    # init_bot raises when WEBHOOK_BASE_URL is set, so empty must be valid here.
    s = _settings(webhook_secret="")
    assert s.webhook_secret == ""


def test_webhook_secret_valid_charset_accepted():
    s = _settings(webhook_secret="Abc_123-XYZ")
    assert s.webhook_secret == "Abc_123-XYZ"


def test_webhook_secret_invalid_charset_rejected():
    # Telegram allows only A-Za-z0-9_- (1-256 chars); spaces/'!' are invalid.
    with pytest.raises(ValidationError):
        _settings(webhook_secret="has spaces!")
