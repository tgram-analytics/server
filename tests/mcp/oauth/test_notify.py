"""Best-effort Telegram alert on derived-token issuance."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.mcp.oauth.notify import notify_token_issued


@pytest.mark.asyncio
async def test_sends_message_with_revoke_button():
    bot = MagicMock()
    bot.send_message = AsyncMock()
    with patch("app.bot.setup.get_bot", return_value=bot):
        await notify_token_issued(admin_chat_id=42, client_name="Claude", token_id="abc-123")
    kwargs = bot.send_message.call_args.kwargs
    assert kwargs["chat_id"] == 42
    assert "Claude" in kwargs["text"]
    markup = kwargs["reply_markup"]
    assert markup.inline_keyboard[0][0].callback_data == "mcptok:revoke:abc-123"


@pytest.mark.asyncio
async def test_failure_is_swallowed():
    with patch("app.bot.setup.get_bot", side_effect=RuntimeError("bot down")):
        await notify_token_issued(admin_chat_id=42, client_name="C", token_id="x")
    # no raise = pass
