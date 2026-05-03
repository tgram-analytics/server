"""Tests for the composable bot-filter extension point (Phase 4).

The OSS bot has a hardcoded admin-only chat filter. Deployments may
register additional filters via ``app.extensions.register_bot_filter``;
those filters AND-combine with the admin-chat default. Replacing the
admin gate is intentionally NOT a hook — that decision lives at the
resolver layer (Phase 2).

These tests exercise the filter composition directly so they don't
require a live Telegram Bot instance.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from telegram import Chat, Message, Update, User
from telegram.ext import filters

from app import extensions as ext


@pytest.fixture(autouse=True)
def _clear_registry():
    ext._reset_for_tests()
    yield
    ext._reset_for_tests()


def _compose_admin_filter(admin_chat_id: int) -> filters.BaseFilter:
    """Replicate the composition logic in build_application for testing.

    Keeping this in the test file rather than exporting it from
    bot/setup.py means the *behavior* is what's tested, not a private
    helper. If build_application's logic drifts from this loop, a
    test below ``test_composition_matches_build_application`` will
    flag it.
    """
    base: filters.BaseFilter = filters.Chat(chat_id=admin_chat_id)
    for extra in ext.get_bot_filters():
        base = base & extra
    return base


def _real_update(chat_id: int, user_id: int = 1, text: str | None = "/help") -> Update:
    """Build a real Update with a Message — PTB filters require real types."""
    chat = Chat(id=chat_id, type="private")
    user = User(id=user_id, first_name="X", is_bot=False)
    msg = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=chat,
        from_user=user,
        text=text,
    )
    return Update(update_id=1, message=msg)


# ── Default behavior: no extras registered ────────────────────────────────────


def test_no_extras_admin_only_passes_admin_chat() -> None:
    f = _compose_admin_filter(admin_chat_id=999)
    update = _real_update(chat_id=999)
    assert f.check_update(update) is True


def test_no_extras_admin_only_rejects_other_chat() -> None:
    f = _compose_admin_filter(admin_chat_id=999)
    update = _real_update(chat_id=1)
    assert not f.check_update(update)


# ── Single extra filter ───────────────────────────────────────────────────────


class _AlwaysTrue(filters.MessageFilter):
    def filter(self, message: Any) -> bool:
        return True


class _AlwaysFalse(filters.MessageFilter):
    def filter(self, message: Any) -> bool:
        return False


def test_extra_always_true_does_not_change_behavior() -> None:
    """``base & True`` is equivalent to ``base`` semantically."""
    ext.register_bot_filter(_AlwaysTrue())
    f = _compose_admin_filter(admin_chat_id=999)

    assert f.check_update(_real_update(chat_id=999)) is True
    assert not f.check_update(_real_update(chat_id=1))


def test_extra_always_false_rejects_everything() -> None:
    """``base & False`` rejects all updates, including admin's."""
    ext.register_bot_filter(_AlwaysFalse())
    f = _compose_admin_filter(admin_chat_id=999)

    assert not f.check_update(_real_update(chat_id=999))
    assert not f.check_update(_real_update(chat_id=1))


# ── Multiple extras ───────────────────────────────────────────────────────────


def test_multiple_extras_all_true_keeps_admin_only_semantics() -> None:
    ext.register_bot_filter(_AlwaysTrue())
    ext.register_bot_filter(_AlwaysTrue())
    ext.register_bot_filter(_AlwaysTrue())
    f = _compose_admin_filter(admin_chat_id=999)

    assert f.check_update(_real_update(chat_id=999)) is True
    assert not f.check_update(_real_update(chat_id=1))


def test_any_false_in_chain_rejects() -> None:
    """``base & T & T & F`` is False — single False short-circuits."""
    ext.register_bot_filter(_AlwaysTrue())
    ext.register_bot_filter(_AlwaysTrue())
    ext.register_bot_filter(_AlwaysFalse())
    f = _compose_admin_filter(admin_chat_id=999)

    assert not f.check_update(_real_update(chat_id=999))


def test_filters_compose_in_registration_order() -> None:
    """Order is preserved (visible via repr / handler.filters); each
    filter is included exactly once in the final composition.
    """
    a, b, c = _AlwaysTrue(), _AlwaysTrue(), _AlwaysTrue()
    ext.register_bot_filter(a)
    ext.register_bot_filter(b)
    ext.register_bot_filter(c)

    assert ext.get_bot_filters() == (a, b, c)


# ── Integration with build_application ────────────────────────────────────────


def test_composition_matches_build_application() -> None:
    """The build_application path uses the same composition as our helper.

    We don't call build_application here (it would need a real bot
    token); instead we verify by inspection that the source still does
    a base & extra loop. Any drift breaks this string match — which is
    deliberate, the loop is load-bearing.
    """
    import inspect

    from app.bot import setup as bot_setup

    src = inspect.getsource(bot_setup.build_application)
    assert "get_bot_filters()" in src
    assert "admin_filter & extra" in src or "admin_filter = admin_filter & extra" in src


# ── Type-safety: register_bot_filter accepts BaseFilter subclasses ────────────


def test_register_accepts_base_filter_subclass() -> None:
    """Built-in filters like filters.TEXT compose cleanly with the chain."""
    ext.register_bot_filter(filters.TEXT)
    f = _compose_admin_filter(admin_chat_id=999)

    # Plain text from admin: passes both filters.
    assert f.check_update(_real_update(chat_id=999, text="hello")) is True

    # Text from admin still passes TEXT (the filter doesn't distinguish
    # commands from plain text — that's filters.COMMAND's job).
    assert f.check_update(_real_update(chat_id=999, text="/start")) is True

    # Non-text update (no text at all): TEXT filter rejects.
    no_text_update = _real_update(chat_id=999, text=None)
    assert not f.check_update(no_text_update)
