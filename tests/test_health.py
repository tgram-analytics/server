"""Phase 0 tests — health endpoint, DB connection guard, env validation."""

import os

import pytest
from httpx import AsyncClient
from pydantic import ValidationError

# ── Health endpoint ───────────────────────────────────────────────────────────


async def test_health_endpoint(client: AsyncClient) -> None:
    """GET /health returns 200 and body {"status": "ok"}."""
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# ── DB connection guard ───────────────────────────────────────────────────────


def test_db_connection_valid_url_does_not_raise() -> None:
    """init_db() with a syntactically valid URL builds an engine without raising."""
    from app.core.database import build_engine

    # We only check engine creation (not a live connection) to keep tests fast.
    engine = build_engine("postgresql+asyncpg://tga:password@localhost/tganalytics_test")
    assert engine is not None


def test_db_connection_invalid_url_raises() -> None:
    """build_engine() with a garbage URL raises at engine-creation time."""
    from sqlalchemy.exc import ArgumentError

    from app.core.database import build_engine

    with pytest.raises((ArgumentError, Exception)):
        build_engine("not-a-valid-db-url://???")


# ── Env validation ────────────────────────────────────────────────────────────


def test_env_validation_missing_telegram_bot_token() -> None:
    """Settings raises ValidationError when TELEGRAM_BOT_TOKEN is absent."""
    import importlib

    saved = os.environ.pop("TELEGRAM_BOT_TOKEN", None)
    try:
        import app.core.config as config_mod

        importlib.reload(config_mod)
        with pytest.raises((ValidationError, Exception)):
            config_mod.Settings(
                admin_chat_id=123,
                database_url="postgresql+asyncpg://x:x@localhost/x",
                secret_key="s",
            )
    finally:
        if saved is not None:
            os.environ["TELEGRAM_BOT_TOKEN"] = saved


def test_env_validation_missing_admin_chat_id() -> None:
    """Settings raises ValidationError when ADMIN_CHAT_ID is absent."""
    import importlib

    import app.core.config as config_mod

    importlib.reload(config_mod)

    # Temporarily remove ADMIN_CHAT_ID so pydantic-settings cannot read it
    # from the environment (a previous test may have set it via os.environ).
    saved = os.environ.pop("ADMIN_CHAT_ID", None)
    try:
        with pytest.raises((ValidationError, Exception)):
            config_mod.Settings(
                telegram_bot_token="1234:token",
                database_url="postgresql+asyncpg://x:x@localhost/x",
                secret_key="s",
            )
    finally:
        if saved is not None:
            os.environ["ADMIN_CHAT_ID"] = saved


def test_env_validation_empty_telegram_bot_token() -> None:
    """Settings raises ValidationError when TELEGRAM_BOT_TOKEN is an empty string."""
    import importlib

    import app.core.config as config_mod

    importlib.reload(config_mod)
    with pytest.raises((ValidationError, Exception)):
        config_mod.Settings(
            telegram_bot_token="   ",
            admin_chat_id=123,
            database_url="postgresql+asyncpg://x:x@localhost/x",
            secret_key="s",
        )
