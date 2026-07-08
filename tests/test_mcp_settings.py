"""MCP-related Settings fields exist with self-host-friendly defaults."""

from app.core.config import Settings


def _settings(**overrides) -> Settings:
    base = dict(
        telegram_bot_token="123:abc",
        admin_chat_id=1,
        database_url="sqlite+aiosqlite://",
        secret_key="x" * 32,
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
