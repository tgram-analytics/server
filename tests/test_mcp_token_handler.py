"""Tests for the /mcp_token bot command.

Uses the same fake-``Update`` / ``session_factory`` / ``singleton_user``
idiom as ``tests/test_export_handler.py``: the ``@requires_user``
decorator opens its own session from the (test-wired) module-level
session factory, so handler writes are verified through a fresh session
opened via ``session_factory``.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import Message

from app.bot.handlers.mcp_tokens import mcp_token_callback, mcp_token_command
from app.services import mcp_tokens as svc

ADMIN_ID = 111


def _make_command(args: list[str]):
    """Fake a ``/mcp_token`` command update + context with ``ctx.args``."""
    update = MagicMock()
    update.effective_chat.id = ADMIN_ID
    update.effective_user.id = ADMIN_ID
    message = MagicMock(spec=Message)
    message.reply_html = AsyncMock()
    update.message = message
    update.callback_query = None
    ctx = MagicMock()
    ctx.args = args
    return update, ctx


def _make_callback(data: str):
    """Fake a ``mcptok:...`` callback-query update + context."""
    update = MagicMock()
    update.effective_chat.id = ADMIN_ID
    update.effective_user.id = ADMIN_ID
    update.message = None
    query = MagicMock()
    query.data = data
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    update.callback_query = query
    ctx = MagicMock()
    return update, ctx, query


@pytest.mark.asyncio
async def test_mcp_token_new_creates_and_shows_raw_once(session_factory, singleton_user):
    update, ctx = _make_command(["new", "claude-desktop"])
    await mcp_token_command(update, ctx)

    reply = update.message.reply_html.call_args[0][0]
    assert "mcp_" in reply  # raw token shown

    async with session_factory() as session:
        rows = await svc.list_tokens(session, user_id=singleton_user.id)
    assert len(rows) == 1 and rows[0].label == "claude-desktop"
    assert rows[0].token_hash not in reply  # hash never shown


@pytest.mark.asyncio
async def test_mcp_token_list_empty(session_factory, singleton_user):
    update, ctx = _make_command([])
    await mcp_token_command(update, ctx)

    reply = update.message.reply_html.call_args[0][0]
    assert "No MCP tokens" in reply


@pytest.mark.asyncio
async def test_mcp_token_revoke_via_callback(session_factory, singleton_user):
    async with session_factory() as session:
        raw, row = await svc.create_token(session, user_id=singleton_user.id, label="x")
        await session.commit()
        token_id = row.id

    update, ctx, query = _make_callback(f"mcptok:revoke:{token_id}")
    await mcp_token_callback(update, ctx)

    async with session_factory() as session:
        assert await svc.lookup_active_token(session, raw) is None
    assert isinstance(token_id, uuid.UUID)
