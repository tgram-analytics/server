"""Shared pytest fixtures for the tg-analytics test suite."""

import os
from collections.abc import AsyncGenerator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool


@pytest.fixture()
def anyio_backend() -> str:
    return "asyncio"


def make_test_app(overrides: dict | None = None) -> FastAPI:
    """Create a FastAPI app with test-safe environment overrides.

    ``overrides`` is merged into ``os.environ`` before the settings object is
    created, so individual tests can inject missing required variables.
    """
    import os

    defaults = {
        "TELEGRAM_BOT_TOKEN": "1234567890:test-token-for-testing-only",
        "ADMIN_CHAT_ID": "123456789",
        "DATABASE_URL": "postgresql+asyncpg://tga:password@localhost/tganalytics_test",
        "SECRET_KEY": "test-secret-key-not-for-production",
        "WEBHOOK_BASE_URL": "https://example.com",
    }
    env = {**defaults, **(overrides or {})}

    # Patch environment before importing app to ensure Settings picks them up.
    original = {k: os.environ.get(k) for k in env}
    os.environ.update(env)

    try:
        # Re-import to get a fresh app with the patched environment.
        import importlib

        import app.core.config as config_mod
        import app.main as main_mod

        importlib.reload(config_mod)
        importlib.reload(main_mod)
        return main_mod.create_app()
    finally:
        # Restore original env so tests are isolated.
        for k, v in original.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@pytest.fixture()
async def client() -> AsyncClient:
    """Async HTTP client wired to the test app (no real DB connection)."""
    # We override the lifespan so the DB init is skipped for unit tests
    # that don't need a real database.
    import os

    from fastapi import FastAPI

    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1234567890:test-token-for-testing-only")
    os.environ.setdefault("ADMIN_CHAT_ID", "123456789")
    os.environ.setdefault(
        "DATABASE_URL",
        "postgresql+asyncpg://tga:password@localhost/tganalytics_test",
    )
    os.environ.setdefault("SECRET_KEY", "test-secret-key-not-for-production")
    os.environ.setdefault("WEBHOOK_BASE_URL", "https://example.com")

    from collections.abc import AsyncGenerator
    from contextlib import asynccontextmanager

    from app.main import create_app

    @asynccontextmanager
    async def null_lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        """Skip DB init for tests that don't need a real database."""
        yield

    test_app = create_app()
    test_app.router.lifespan_context = null_lifespan  # type: ignore[assignment]

    async with AsyncClient(
        transport=ASGITransport(app=test_app), base_url="http://testserver"
    ) as c:
        yield c


# ── Database integration fixtures ─────────────────────────────────────────────


@pytest.fixture(scope="session")
def db_url() -> str:
    """Return DATABASE_URL or skip the test session if not configured."""
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not set — skipping DB integration tests")
    return url


@pytest.fixture(scope="session")
async def async_engine(db_url: str) -> AsyncGenerator[AsyncEngine, None]:
    """Session-scoped async engine; skips if the DB is not reachable."""
    engine = create_async_engine(db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:
        await engine.dispose()
        pytest.skip(f"DB not reachable — {exc}")
    yield engine
    await engine.dispose()


@pytest.fixture()
async def db_session(async_engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    """Per-test async session that rolls back after each test."""
    async with async_engine.connect() as conn:
        await conn.begin()
        async with AsyncSession(bind=conn, expire_on_commit=False) as session:
            yield session
        await conn.rollback()
