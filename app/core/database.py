"""Async SQLAlchemy engine and session factory."""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Shared declarative base for all ORM models."""


def build_engine(database_url: str):  # type: ignore[no-untyped-def]
    """Create an async SQLAlchemy engine from the given URL.

    ``echo=False`` in production; tests may override via ``database_url``.
    """
    return create_async_engine(
        database_url,
        echo=False,
        pool_pre_ping=True,
    )


def build_session_factory(engine) -> async_sessionmaker[AsyncSession]:  # type: ignore[type-arg]
    """Return an ``async_sessionmaker`` bound to *engine*."""
    return async_sessionmaker(engine, expire_on_commit=False)


# Module-level singletons populated during app lifespan.
_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def init_db(database_url: str) -> None:
    """Initialise the module-level engine and session factory.

    Called once from the FastAPI lifespan handler.
    """
    global _engine, _session_factory
    _engine = build_engine(database_url)
    _session_factory = build_session_factory(_engine)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields a database session per request."""
    if _session_factory is None:
        raise RuntimeError("Database not initialised. Call init_db() first.")
    async with _session_factory() as session:
        yield session


async def close_db() -> None:
    """Dispose the engine; called on app shutdown."""
    if _engine is not None:
        await _engine.dispose()
