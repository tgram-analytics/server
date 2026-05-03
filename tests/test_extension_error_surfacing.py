"""Tests for ExtensionError surfacing in user-facing handlers.

Plugin hooks raise to abort an action; OSS handlers catch
ExtensionError specifically and reply with str(exc) so the end user
sees a friendly message instead of a generic failure.

Anything else (TypeError, ValueError from non-plugin code) must NOT be
caught — it represents a real bug and should propagate.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app import extensions as ext


@pytest.fixture(autouse=True)
def _clear_registry():
    ext._reset_for_tests()
    yield
    ext._reset_for_tests()


def test_extension_error_is_an_exception() -> None:
    """ExtensionError must be a real Exception so plugins can raise it."""
    assert issubclass(ext.ExtensionError, Exception)


def test_extension_error_carries_message() -> None:
    """str(ExtensionError) returns the message for handlers to render."""
    e = ext.ExtensionError("over quota")
    assert str(e) == "over quota"


def test_subclassing_is_supported() -> None:
    """Plugins inherit and add typed exception classes."""

    class QuotaExceeded(ext.ExtensionError):
        pass

    e = QuotaExceeded("limit reached")
    assert isinstance(e, ext.ExtensionError)
    assert str(e) == "limit reached"


# ── add_command catches ExtensionError ────────────────────────────────────────


async def test_add_command_renders_extension_error() -> None:
    """A pre-create hook that raises ExtensionError → user sees the message."""

    # Register a hook that always blocks.
    async def blocker(session, **kwargs):  # noqa: ANN001
        raise ext.ExtensionError("you have reached your free-tier limit")

    ext.register_project_pre_create(blocker)

    # Build the dependencies add_command needs.
    from app.bot.handlers.projects import add_command
    from app.models.user import User

    update = MagicMock()
    update.message = AsyncMock()
    ctx = MagicMock()
    ctx.args = ["new-project"]

    fake_user = MagicMock(spec=User)
    fake_user.id = uuid.uuid4()
    fake_user.telegram_user_id = 12345

    session = MagicMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.refresh = AsyncMock()

    # Call the underlying handler (bypass the @requires_user decorator
    # by reaching into __wrapped__).
    inner = add_command.__wrapped__
    await inner(update, ctx, user=fake_user, session=session)

    # The reply text should contain the plugin's message.
    update.message.reply_text.assert_awaited_once()
    sent = update.message.reply_text.await_args[0][0]
    assert "free-tier limit" in sent

    # No project was added.
    session.add.assert_not_called()


async def test_add_command_does_not_swallow_unrelated_exceptions() -> None:
    """A non-ExtensionError must propagate — bugs should not be hidden."""

    async def broken_hook(session, **kwargs):  # noqa: ANN001
        raise TypeError("real bug here")

    ext.register_project_pre_create(broken_hook)

    from app.bot.handlers.projects import add_command
    from app.models.user import User

    update = MagicMock()
    update.message = AsyncMock()
    ctx = MagicMock()
    ctx.args = ["proj"]

    fake_user = MagicMock(spec=User)
    fake_user.id = uuid.uuid4()
    fake_user.telegram_user_id = 12345

    session = MagicMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.refresh = AsyncMock()

    inner = add_command.__wrapped__
    with pytest.raises(TypeError, match="real bug"):
        await inner(update, ctx, user=fake_user, session=session)


async def test_add_command_subclass_caught_too() -> None:
    """ExtensionError subclasses are caught by the same except clause."""

    class CustomQuota(ext.ExtensionError):
        pass

    async def quota_hook(session, **kwargs):  # noqa: ANN001
        raise CustomQuota("custom-tier hit limit")

    ext.register_project_pre_create(quota_hook)

    from app.bot.handlers.projects import add_command
    from app.models.user import User

    update = MagicMock()
    update.message = AsyncMock()
    ctx = MagicMock()
    ctx.args = ["x"]

    fake_user = MagicMock(spec=User)
    fake_user.id = uuid.uuid4()
    fake_user.telegram_user_id = 1

    session = MagicMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.refresh = AsyncMock()

    inner = add_command.__wrapped__
    await inner(update, ctx, user=fake_user, session=session)

    sent = update.message.reply_text.await_args[0][0]
    assert "custom-tier hit limit" in sent
