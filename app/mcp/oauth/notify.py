"""Telegram notification when an OAuth flow issues a derived MCP token.

The spec's detectability control: silent token theft (confused-deputy
authorize link) becomes a visible event with a one-tap revoke. Strictly
best-effort — a Telegram outage must never fail the OAuth grant, so
every failure path is swallowed (logged at WARNING).
"""

from __future__ import annotations

import logging
from html import escape

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

logger = logging.getLogger("app.mcp.oauth")


async def notify_token_issued(
    *, admin_chat_id: int, client_name: str, token_id: str, redirect_host: str
) -> None:
    try:
        from app.bot.setup import get_bot

        bot = get_bot()
        await bot.send_message(
            chat_id=admin_chat_id,
            text=(
                "🔑 New MCP client authorized via OAuth: "
                f"<b>{escape(client_name)}</b>\n"
                f"Callback host: <b>{escape(redirect_host)}</b>\n"
                "Not you? Revoke it now."
            ),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🗑 Revoke", callback_data=f"mcptok:revoke:{token_id}")]]
            ),
        )
    except Exception:
        logger.warning("failed to send MCP OAuth issuance notification", exc_info=True)
