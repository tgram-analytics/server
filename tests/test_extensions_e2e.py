"""End-to-end test: reference plugin → loader → all four hooks fire.

Validates the full extension surface composes correctly. If this test
breaks, the public extension contract has regressed and any private
overlay package depending on it is at risk.
"""

from __future__ import annotations

import importlib
import sys
import uuid
from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import pytest

from app import extensions as ext
from app.plugins import ENV_VAR, load_plugins


@pytest.fixture()
def loaded_reference_plugin() -> Iterator[None]:
    """Load the reference plugin via the env-var path and tear it down.

    Importantly, we monkey-patch ``app.core.config.Settings`` back to
    the original at teardown so the Settings extension doesn't leak
    into other tests.
    """
    import os

    from app.core import config as app_config

    # Snapshot state we will restore.
    prev_settings_cls = app_config.Settings
    prev_env = os.environ.get(ENV_VAR)

    # Reset registry + plugin's module-level captured state.
    ext._reset_for_tests()
    if "tests.fixtures.reference_plugin" in sys.modules:
        importlib.reload(sys.modules["tests.fixtures.reference_plugin"])

    os.environ[ENV_VAR] = "tests.fixtures.reference_plugin"
    with patch("app.plugins.entry_points", return_value=[]):
        load_plugins()

    try:
        yield
    finally:
        ext._reset_for_tests()
        app_config.Settings = prev_settings_cls
        if prev_env is None:
            os.environ.pop(ENV_VAR, None)
        else:
            os.environ[ENV_VAR] = prev_env


# ── User resolver hook ────────────────────────────────────────────────────────


async def test_reference_resolver_is_active(loaded_reference_plugin) -> None:
    """After load, get_user_resolver returns the plugin's callable."""
    from tests.fixtures import reference_plugin

    resolver = ext.get_user_resolver()
    assert resolver is reference_plugin.reference_resolver


async def test_reference_resolver_intercepts_get_current_user(
    loaded_reference_plugin,
) -> None:
    """The wired resolver runs in place of the singleton path.

    We don't have a DB, so the resolver returns None when the singleton
    is unset — the important assertion is that the resolver was *called*
    (visible via the captured_resolver_call dict).
    """
    from app.bot.auth import get_current_user
    from tests.fixtures import reference_plugin

    session_sentinel = MagicMock()
    update_sentinel = MagicMock()
    await get_current_user(session_sentinel, update_sentinel)

    assert reference_plugin.captured_resolver_call["session"] is session_sentinel
    assert reference_plugin.captured_resolver_call["update"] is update_sentinel


# ── Pre-create project hook ───────────────────────────────────────────────────


async def test_pre_create_hook_receives_inputs(loaded_reference_plugin) -> None:
    """create_project routes through the pre-create policy hook."""
    from unittest.mock import AsyncMock

    from app.services.projects import create_project
    from tests.fixtures import reference_plugin

    session = MagicMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.refresh = AsyncMock()

    owner_id = uuid.uuid4()
    await create_project(
        session,
        name="my-good-app",
        admin_chat_id=42,
        owner_user_id=owner_id,
        domain_allowlist=["a.com"],
    )

    assert reference_plugin.captured_pre_create_call == {
        "name": "my-good-app",
        "owner_user_id": owner_id,
        "domain_allowlist": ["a.com"],
    }


async def test_pre_create_hook_can_reject(loaded_reference_plugin) -> None:
    """A name matching the policy rule aborts creation."""
    from unittest.mock import AsyncMock

    from app.services.projects import create_project

    session = MagicMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.refresh = AsyncMock()

    with pytest.raises(ValueError, match="forbidden-policy"):
        await create_project(
            session,
            name="this-is-forbidden",
            admin_chat_id=42,
            owner_user_id=uuid.uuid4(),
        )

    session.add.assert_not_called()


# ── Bot filter hook ───────────────────────────────────────────────────────────


def test_reference_filter_is_in_chain(loaded_reference_plugin) -> None:
    """register_bot_filter put exactly one filter into the registry."""
    filters_chain = ext.get_bot_filters()
    assert len(filters_chain) == 1
    # No constraint on the exact class — just that it's the plugin's filter.
    assert filters_chain[0].__class__.__name__ == "_CountingFilter"


def test_reference_filter_is_invoked_when_composed(loaded_reference_plugin) -> None:
    """Composed admin & extra-filter chain calls the extra filter on a real Update."""
    from datetime import UTC, datetime

    from telegram import Chat, Message, Update, User
    from telegram.ext import filters as ptb_filters

    from tests.fixtures import reference_plugin

    base = ptb_filters.Chat(chat_id=999)
    composed = base
    for f in ext.get_bot_filters():
        composed = composed & f

    chat = Chat(id=999, type="private")
    user = User(id=1, first_name="X", is_bot=False)
    msg = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=chat,
        from_user=user,
        text="hi",
    )
    upd = Update(update_id=1, message=msg)

    before = reference_plugin.extra_filter_check_count
    composed.check_update(upd)
    assert reference_plugin.extra_filter_check_count == before + 1


# ── Settings subclass hook ────────────────────────────────────────────────────


def test_settings_extended_with_extra_field(loaded_reference_plugin) -> None:
    """The plugin monkey-patched Settings to add an env var.

    The new field has a default, so instantiation works without setting
    the env var. The point is just that the extended class is now the
    one referenced by ``app.core.config.Settings``.
    """
    from app.core import config as app_config

    fields = app_config.Settings.model_fields
    assert "reference_plugin_extra" in fields
    assert fields["reference_plugin_extra"].default == "default-value"


# ── Public surface end-to-end ─────────────────────────────────────────────────


def test_all_four_hooks_active_after_single_load(loaded_reference_plugin) -> None:
    """One register() call wires all four hooks: smoke test."""
    from app.core import config as app_config

    assert ext.get_user_resolver() is not None
    assert len(ext.get_project_pre_create_hooks()) == 1
    assert len(ext.get_bot_filters()) == 1
    assert "reference_plugin_extra" in app_config.Settings.model_fields
