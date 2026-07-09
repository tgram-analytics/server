"""Tests for the /mcp setup-instructions bot command.

Same fake-``Update`` / ``session_factory`` / ``singleton_user`` idiom as
``tests/test_mcp_token_handler.py``. ``@requires_user`` opens its own
session from the test-wired module-level session factory.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import Message

from app.bot.handlers.mcp import mcp_command
from app.core.config import get_settings

ADMIN_ID = 111


def _make_command():
    update = MagicMock()
    update.effective_chat.id = ADMIN_ID
    update.effective_user.id = ADMIN_ID
    message = MagicMock(spec=Message)
    message.reply_html = AsyncMock()
    update.message = message
    update.callback_query = None
    ctx = MagicMock()
    ctx.args = []
    return update, ctx


@pytest.mark.asyncio
async def test_mcp_default_shows_static_token_flow(session_factory, singleton_user):
    update, ctx = _make_command()
    await mcp_command(update, ctx)

    reply = update.message.reply_html.call_args[0][0]
    base = get_settings().mcp_effective_public_url
    assert f"{base}/mcp" in reply  # server URL present
    assert "/mcp_token new" in reply  # static-token flow
    assert "--transport http" in reply  # claude mcp add one-liner


@pytest.mark.asyncio
async def test_mcp_cloud_shows_oauth_flow(session_factory, singleton_user, monkeypatch):
    # A registered custom verifier (cloud overlay) → OAuth sign-in flow,
    # not the static-token flow.
    monkeypatch.setattr("app.extensions.get_mcp_token_verifier", lambda: object())

    update, ctx = _make_command()
    await mcp_command(update, ctx)

    reply = update.message.reply_html.call_args[0][0]
    assert "sign in" in reply.lower()
    assert "/mcp_token new" not in reply
    assert "--transport http" not in reply


@pytest.mark.asyncio
async def test_mcp_disabled_reports_off(session_factory, singleton_user, monkeypatch):
    # get_settings() builds a fresh Settings() each call, so patch the
    # function to hand the handler an mcp_enabled=False settings object.
    from types import SimpleNamespace

    monkeypatch.setattr(
        "app.core.config.get_settings",
        lambda: SimpleNamespace(mcp_enabled=False),
    )

    update, ctx = _make_command()
    await mcp_command(update, ctx)

    reply = update.message.reply_html.call_args[0][0]
    assert "disabled" in reply.lower()
    assert "/mcp_token" not in reply
