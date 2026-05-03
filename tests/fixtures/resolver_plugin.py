"""Plugin that registers a custom user resolver for end-to-end tests."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

resolver_mock: Any = None


def register() -> None:
    """Register an AsyncMock as the user resolver.

    Tests can then inspect the mock to verify the loader successfully
    plumbed an external module's hook into the live registry.
    """
    global resolver_mock
    from app import extensions as ext

    resolver_mock = AsyncMock(return_value=None)
    ext.register_user_resolver(resolver_mock)
