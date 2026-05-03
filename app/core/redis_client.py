"""Async Redis client lifecycle management.

Wraps a single module-level ``redis.asyncio.Redis`` instance, mirroring the
``init_db``/``close_db`` style in :mod:`app.core.database`. The client is
optional: when no URL is configured the module returns ``None`` and callers
fall back to in-process state (single-replica self-host mode).

Reference: https://redis.readthedocs.io/en/stable/asyncio.html
"""

from __future__ import annotations

import redis.asyncio as redis_asyncio

# Module-level singleton populated by :func:`init_redis`.
_client: redis_asyncio.Redis | None = None


def init_redis(url: str | None) -> None:
    """Initialise the module-level async Redis client.

    Called once from the FastAPI lifespan handler. If *url* is ``None`` or
    empty, ``_client`` stays ``None`` and dependent code uses its local
    fallback path (intended for single-replica self-host).
    """
    global _client
    if not url:
        _client = None
        return
    _client = redis_asyncio.Redis.from_url(
        url,
        encoding="utf-8",
        decode_responses=True,
    )


def get_redis() -> redis_asyncio.Redis | None:
    """Return the cached async Redis client, or ``None`` if unconfigured."""
    return _client


async def close_redis() -> None:
    """Close the async Redis client; idempotent."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
