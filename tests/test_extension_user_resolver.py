"""Tests for the pluggable user-resolver extension point (Phase 2).

Validates that ``app.bot.auth.get_current_user`` consults the
extensions registry first and falls back to the singleton path
otherwise. Also covers ``register_user_resolver``'s
single-registration contract.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app import extensions as ext
from app.bot import auth as auth_mod
from app.bot.auth import get_current_user
from app.models.user import User


@pytest.fixture(autouse=True)
def _clear_registry():
    """Reset the registry before each test for isolation."""
    ext._reset_for_tests()
    yield
    ext._reset_for_tests()


async def test_no_resolver_returns_none_when_singleton_unset() -> None:
    """Default path: no resolver, no singleton bootstrap → None.

    Mirrors the defensive branch in ``get_current_user`` when
    ``_singleton_user_id`` has not been populated by ``init_bot``.
    """
    prev = auth_mod._singleton_user_id
    auth_mod._singleton_user_id = None
    try:
        session = MagicMock()
        update = MagicMock()
        result = await get_current_user(session, update)
        assert result is None
    finally:
        auth_mod._singleton_user_id = prev


async def test_custom_resolver_replaces_default() -> None:
    """Registered resolver is called; singleton path is bypassed."""
    sentinel_user = MagicMock(spec=User)
    custom = AsyncMock(return_value=sentinel_user)
    ext.register_user_resolver(custom)

    session = MagicMock()
    update = MagicMock()
    result = await get_current_user(session, update)

    assert result is sentinel_user
    custom.assert_awaited_once_with(session, update)


async def test_custom_resolver_returning_none_short_circuits() -> None:
    """Resolver may decline to authorize by returning None.

    ``requires_user`` then surfaces "Not authorized" — that decorator path
    is exercised in test_phase6.py; here we only verify get_current_user
    propagates None faithfully.
    """
    custom = AsyncMock(return_value=None)
    ext.register_user_resolver(custom)

    result = await get_current_user(MagicMock(), MagicMock())
    assert result is None
    custom.assert_awaited_once()


def test_register_user_resolver_raises_on_duplicate() -> None:
    """Single-registration contract: second call must fail loudly."""
    ext.register_user_resolver(AsyncMock())
    with pytest.raises(RuntimeError, match="already registered"):
        ext.register_user_resolver(AsyncMock())


def test_reset_clears_resolver() -> None:
    """``_reset_for_tests`` puts the registry back to a fresh state."""
    ext.register_user_resolver(AsyncMock())
    assert ext.get_user_resolver() is not None

    ext._reset_for_tests()
    assert ext.get_user_resolver() is None
