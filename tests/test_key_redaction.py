"""Tests for delayed redaction of secrets revealed in bot messages.

Covers the masking rules, the timer path, and the "Hide key now" button.
Handler-level wiring (which messages get scheduled) is asserted in the
per-handler test modules.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.error import BadRequest

from app.bot.key_redaction import (
    contains_secret,
    hide_key_callback,
    mask_secret,
    redact_message,
    redact_secrets,
    schedule_redaction,
    with_hide_button,
)

API_KEY = "proj_" + "e9" * 32
MCP_TOKEN = "mcp_" + "7c" * 32
CHAT_ID = 111
MESSAGE_ID = 222


def _bot():
    bot = MagicMock()
    bot.edit_message_text = AsyncMock()
    return bot


def _callback_update():
    update = MagicMock()
    query = MagicMock()
    query.answer = AsyncMock()
    query.message.chat.id = CHAT_ID
    query.message.message_id = MESSAGE_ID
    update.callback_query = query
    return update, query


# ── masking ───────────────────────────────────────────────────────────────────


def test_mask_keeps_prefix_and_last_four():
    assert mask_secret(API_KEY) == "proj_••••e9e9"
    assert mask_secret(MCP_TOKEN) == "mcp_••••7c7c"


def test_redacts_every_occurrence_including_snippets():
    text = (
        f"TGA_API_KEY={API_KEY}\n"
        f'curl -d \'{{"api_key": "{API_KEY}"}}\'\n'
        f"Authorization: Bearer {MCP_TOKEN}"
    )
    out = redact_secrets(text)

    assert API_KEY not in out
    assert MCP_TOKEN not in out
    assert out.count("proj_••••e9e9") == 2
    assert "mcp_••••7c7c" in out
    # Surrounding structure survives.
    assert out.startswith("TGA_API_KEY=proj_")
    assert "Authorization: Bearer" in out


def test_redaction_is_idempotent():
    once = redact_secrets(f"key={API_KEY}")
    assert not contains_secret(once)
    assert redact_secrets(once) == once


def test_non_secret_lookalikes_survive():
    text = "proj_short and mcp_ and proj_" + "z" * 64
    assert redact_secrets(text) == text


# ── timer path ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_timer_edits_message_with_masked_text():
    bot = _bot()
    task = schedule_redaction(bot, CHAT_ID, MESSAGE_ID, f"Save it: {API_KEY}", delay=0)
    assert task is not None
    await task

    kwargs = bot.edit_message_text.call_args.kwargs
    assert kwargs["chat_id"] == CHAT_ID
    assert kwargs["message_id"] == MESSAGE_ID
    assert API_KEY not in kwargs["text"]
    assert "proj_••••e9e9" in kwargs["text"]
    assert kwargs["parse_mode"] == "HTML"


@pytest.mark.asyncio
async def test_secretless_message_is_not_scheduled():
    bot = _bot()
    assert schedule_redaction(bot, CHAT_ID, MESSAGE_ID, "No secrets here", delay=0) is None
    bot.edit_message_text.assert_not_called()


@pytest.mark.asyncio
async def test_redacting_an_untracked_message_is_a_noop():
    bot = _bot()
    assert await redact_message(bot, CHAT_ID, 999) is False
    bot.edit_message_text.assert_not_called()


@pytest.mark.asyncio
async def test_telegram_failure_is_swallowed():
    bot = _bot()
    bot.edit_message_text.side_effect = BadRequest("message to edit not found")
    schedule_redaction(bot, CHAT_ID, MESSAGE_ID, f"key {API_KEY}", delay=3600)

    assert await redact_message(bot, CHAT_ID, MESSAGE_ID) is False
    await asyncio.sleep(0)


# ── hide button ───────────────────────────────────────────────────────────────


def test_hide_button_is_appended_below_existing_rows():
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    original = InlineKeyboardMarkup([[InlineKeyboardButton("a", callback_data="a")]])
    merged = with_hide_button(original)

    assert len(merged.inline_keyboard) == 2
    assert merged.inline_keyboard[0][0].text == "a"
    assert merged.inline_keyboard[1][0].callback_data == "hidekey"
    assert len(with_hide_button(None).inline_keyboard) == 1


@pytest.mark.asyncio
async def test_hide_button_redacts_immediately_and_cancels_timer():
    bot = _bot()
    update, query = _callback_update()
    query.get_bot.return_value = bot

    task = schedule_redaction(bot, CHAT_ID, MESSAGE_ID, f"key {API_KEY}", delay=3600)
    assert task is not None

    await hide_key_callback(update, MagicMock())
    for _ in range(3):  # let the cancelled timer unwind
        await asyncio.sleep(0)

    assert API_KEY not in bot.edit_message_text.call_args.kwargs["text"]
    query.answer.assert_awaited_with("Key hidden.")
    assert task.done()
    assert bot.edit_message_text.await_count == 1


@pytest.mark.asyncio
async def test_hide_button_on_already_redacted_message_answers_politely():
    bot = _bot()
    update, query = _callback_update()
    query.get_bot.return_value = bot

    await hide_key_callback(update, MagicMock())

    bot.edit_message_text.assert_not_called()
    query.answer.assert_awaited_with("Already hidden.")
