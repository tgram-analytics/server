"""Tests for the composable bot-filter extension point (Phase 4).

The bot's pre-dispatch gate is built by ``app.bot.setup.build_handler_filter``.
Its base depends on whether a custom user resolver is registered:

* no resolver — single-tenant, ``filters.Chat(admin_chat_id)``
* resolver registered — multi-tenant, ``filters.ChatType.PRIVATE``, because
  the resolver plus ``@requires_user`` is then the authorization gate and a
  single-chat pre-filter would drop every other account before dispatch

Filters registered via ``app.extensions.register_bot_filter`` AND-combine on
top of whichever base applies — they can narrow the audience, never widen it.

These tests exercise the composition directly so they don't require a live
Telegram Bot instance.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from telegram import Chat, Message, Update, User
from telegram.ext import filters

from app import extensions as ext
from app.bot.setup import build_handler_filter


@pytest.fixture(autouse=True)
def _clear_registry():
    ext._reset_for_tests()
    yield
    ext._reset_for_tests()


async def _dummy_resolver(session: Any, update: Update) -> None:
    """Stand-in for a multi-tenant resolver — never called by these tests.

    ``build_handler_filter`` only checks whether *a* resolver is registered,
    not what it returns.
    """
    return None


def _real_update(
    chat_id: int,
    user_id: int = 1,
    text: str | None = "/help",
    chat_type: str = "private",
) -> Update:
    """Build a real Update with a Message — PTB filters require real types."""
    chat = Chat(id=chat_id, type=chat_type)
    user = User(id=user_id, first_name="X", is_bot=False)
    msg = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=chat,
        from_user=user,
        text=text,
    )
    return Update(update_id=1, message=msg)


# ── Default behavior: single-tenant, no extras registered ─────────────────────


def test_no_extras_admin_only_passes_admin_chat() -> None:
    f = build_handler_filter(admin_chat_id=999)
    update = _real_update(chat_id=999)
    assert f.check_update(update) is True


def test_no_extras_admin_only_rejects_other_chat() -> None:
    f = build_handler_filter(admin_chat_id=999)
    update = _real_update(chat_id=1)
    assert not f.check_update(update)


# ── Multi-tenant: a registered user resolver widens the base ──────────────────


def test_resolver_registered_admits_non_admin_private_chat() -> None:
    """The regression this guards: without it, every account that isn't the
    operator gets silence — PTB drops the update before any handler runs, so
    not even ``@requires_user``'s "Not authorized." reply is sent.
    """
    ext.register_user_resolver(_dummy_resolver)
    f = build_handler_filter(admin_chat_id=999)

    assert f.check_update(_real_update(chat_id=1234567)) is True
    assert f.check_update(_real_update(chat_id=999)) is True


def test_resolver_registered_still_rejects_group_chats() -> None:
    """Handler output is one account's analytics — never fan it out to a group."""
    ext.register_user_resolver(_dummy_resolver)
    f = build_handler_filter(admin_chat_id=999)

    assert not f.check_update(_real_update(chat_id=-100123, chat_type="group"))
    assert not f.check_update(_real_update(chat_id=-100123, chat_type="supergroup"))


def test_no_resolver_keeps_single_tenant_base() -> None:
    """Absent a resolver the gate stays admin-chat-only, private or not."""
    f = build_handler_filter(admin_chat_id=999)

    assert not f.check_update(_real_update(chat_id=1234567))


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
    f = build_handler_filter(admin_chat_id=999)

    assert f.check_update(_real_update(chat_id=999)) is True
    assert not f.check_update(_real_update(chat_id=1))


def test_extra_always_false_rejects_everything() -> None:
    """``base & False`` rejects all updates, including admin's."""
    ext.register_bot_filter(_AlwaysFalse())
    f = build_handler_filter(admin_chat_id=999)

    assert not f.check_update(_real_update(chat_id=999))
    assert not f.check_update(_real_update(chat_id=1))


def test_extra_filter_narrows_multi_tenant_base() -> None:
    """Extras still only narrow once the base is widened by a resolver."""
    ext.register_user_resolver(_dummy_resolver)
    ext.register_bot_filter(_AlwaysFalse())
    f = build_handler_filter(admin_chat_id=999)

    assert not f.check_update(_real_update(chat_id=1234567))


# ── Multiple extras ───────────────────────────────────────────────────────────


def test_multiple_extras_all_true_keeps_admin_only_semantics() -> None:
    ext.register_bot_filter(_AlwaysTrue())
    ext.register_bot_filter(_AlwaysTrue())
    ext.register_bot_filter(_AlwaysTrue())
    f = build_handler_filter(admin_chat_id=999)

    assert f.check_update(_real_update(chat_id=999)) is True
    assert not f.check_update(_real_update(chat_id=1))


def test_any_false_in_chain_rejects() -> None:
    """``base & T & T & F`` is False — single False short-circuits."""
    ext.register_bot_filter(_AlwaysTrue())
    ext.register_bot_filter(_AlwaysTrue())
    ext.register_bot_filter(_AlwaysFalse())
    f = build_handler_filter(admin_chat_id=999)

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


def test_build_application_uses_the_shared_gate() -> None:
    """Every message handler must go through ``build_handler_filter``.

    We don't call build_application here (it would need a real bot token);
    instead we verify by inspection that it delegates rather than composing
    its own filter. Any drift breaks this match — deliberately, since a
    hand-rolled chat filter is exactly the bug this module guards against.
    """
    import inspect

    from app.bot import setup as bot_setup

    src = inspect.getsource(bot_setup.build_application)
    assert "build_handler_filter(admin_chat_id)" in src
    assert "filters.Chat(" not in src


# ── Type-safety: register_bot_filter accepts BaseFilter subclasses ────────────


def test_register_accepts_base_filter_subclass() -> None:
    """Built-in filters like filters.TEXT compose cleanly with the chain."""
    ext.register_bot_filter(filters.TEXT)
    f = build_handler_filter(admin_chat_id=999)

    # Plain text from admin: passes both filters.
    assert f.check_update(_real_update(chat_id=999, text="hello")) is True

    # Text from admin still passes TEXT (the filter doesn't distinguish
    # commands from plain text — that's filters.COMMAND's job).
    assert f.check_update(_real_update(chat_id=999, text="/start")) is True

    # Non-text update (no text at all): TEXT filter rejects.
    no_text_update = _real_update(chat_id=999, text=None)
    assert not f.check_update(no_text_update)
