"""Async-session factory wrapper for MCP tool handlers.

The OSS ``app.core.database.get_session`` is a FastAPI dependency that
``yield``s a session — useful for HTTP routes but awkward for FastMCP
tool handlers, which run outside FastAPI's dependency machinery. Tools
just need a context manager.

This module exposes :func:`open_session` — an ``@asynccontextmanager``
wrapper around the OSS session factory. It reuses the OSS engine
exclusively (via ``get_session_factory``); we never spin up a second
engine in the cloud overlay.

For tests, the factory may be overridden via :func:`set_session_factory`
so suites can point tools at an in-memory SQLite engine without booting
``init_db()``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# Test-time override. ``None`` (the default) means "ask the OSS module
# for the live factory" via :func:`get_session_factory`.
_override_factory: async_sessionmaker[AsyncSession] | None = None


def set_session_factory(factory: async_sessionmaker[AsyncSession] | None) -> None:
    """Install (or clear) a session-factory override for tool handlers.

    Tests call this in a fixture to point tools at a SQLite engine. Pass
    ``None`` to fall back to the OSS module-level factory.
    """
    global _override_factory
    _override_factory = factory


def _resolve_factory() -> async_sessionmaker[AsyncSession]:
    if _override_factory is not None:
        return _override_factory
    # Late import: at module-import time the OSS engine isn't necessarily
    # initialised yet (e.g. in tests that don't call ``init_db``). The
    # call site that needs a session is, by definition, running inside a
    # request handler, so by then the lifespan has already booted.
    from app.core.database import get_session_factory

    return get_session_factory()


@asynccontextmanager
async def open_session() -> AsyncIterator[Any]:
    """Yield a fresh ``AsyncSession`` for the duration of the ``async with``.

    Mirrors the OSS ``get_session`` shape (single-yield) but as a plain
    context manager rather than a FastAPI dependency.
    """
    factory = _resolve_factory()
    async with factory() as session:
        yield session
