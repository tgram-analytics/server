"""mcp_oauth_enabled settings field."""

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


def test_mcp_oauth_enabled_defaults_true():
    assert _settings().mcp_oauth_enabled is True


def test_mcp_oauth_enabled_env_off():
    assert _settings(mcp_oauth_enabled=False).mcp_oauth_enabled is False
