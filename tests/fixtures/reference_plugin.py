"""Reference plugin exercising every OSS extension point.

This plugin lives only under ``tests/fixtures/`` — it is **not** part
of the OSS distribution and is **not** loaded in production. Its sole
purpose is to prove the four extension surfaces compose end-to-end:

* a custom user resolver
* a project pre-create policy hook
* a bot filter
* a Settings subclass with an extra env var

A real downstream package would mirror this shape: a top-level
``register()`` that wires up whatever subset of hooks it needs.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from telegram.ext import filters

# State exposed for inspection by tests.
captured_resolver_call: dict = {}
captured_pre_create_call: dict = {}
extra_filter_check_count = 0


async def reference_resolver(session: Any, update: Any) -> Any:
    """Custom resolver: echoes whatever singleton is already cached.

    Records the call args for the test to verify the resolver was the
    code path actually taken (rather than the singleton fallback).
    """
    captured_resolver_call["session"] = session
    captured_resolver_call["update"] = update

    from app.bot import auth as auth_mod
    from app.models.user import User

    if auth_mod._singleton_user_id is None:
        return None
    return MagicMock(spec=User, id=auth_mod._singleton_user_id)


async def reject_forbidden_names(
    session: Any,
    *,
    name: str,
    owner_user_id: Any,
    domain_allowlist: list[str],
) -> None:
    """Pre-create policy: reject project names containing 'forbidden'.

    Realistic shape: a quota check would receive the same kwargs and
    raise on policy violation. OSS ships zero such hooks; this is a
    fixture only.
    """
    captured_pre_create_call["name"] = name
    captured_pre_create_call["owner_user_id"] = owner_user_id
    captured_pre_create_call["domain_allowlist"] = list(domain_allowlist)
    if "forbidden" in name.lower():
        raise ValueError(f"name {name!r} matches forbidden-policy rule")


class _CountingFilter(filters.MessageFilter):
    """A filter that returns True every time, but counts invocations."""

    def filter(self, message: Any) -> bool:
        global extra_filter_check_count
        extra_filter_check_count += 1
        return True


def register() -> None:
    """Register all four hook kinds.

    Idempotent across calls only if the registry has been reset between
    them — it raises on duplicate user-resolver registration. Tests
    that re-import this module across multiple plugin loads must
    therefore call ``app.extensions._reset_for_tests()`` between them.
    """
    from app import extensions as ext

    ext.register_user_resolver(reference_resolver)
    ext.register_project_pre_create(reject_forbidden_names)
    ext.register_bot_filter(_CountingFilter())

    # Settings extension: subclass and monkey-patch.
    from app.core import config as app_config

    class ExtendedSettings(app_config.Settings):
        reference_plugin_extra: str = "default-value"

    app_config.Settings = ExtendedSettings  # type: ignore[misc]
