"""Telegram notification for AI-agent project-create requests.

When an MCP client files a project-create request, the owning user gets
an inline-keyboard Approve/Reject prompt in Telegram. Strictly
best-effort — a Telegram outage must never fail the MCP tool call, so
every failure path is swallowed (logged at WARNING).
"""

from __future__ import annotations

import logging
from html import escape

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

logger = logging.getLogger("app.mcp")


def approval_keyboard(request_id: str) -> InlineKeyboardMarkup:
    """Approve/Reject inline keyboard for a project-create request.

    Shared by the initial notification and the bot handler's retry path
    (after an ``ExtensionError``) so the callback_data format never drifts.
    """
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"pcr:yes:{request_id}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"pcr:no:{request_id}"),
            ]
        ]
    )


async def notify_project_request(
    *, chat_id: int, request_id: str, name: str, domain_allowlist: list[str]
) -> None:
    try:
        from app.bot.setup import get_bot

        bot = get_bot()
        lines = [
            "🤖 An AI agent connected via MCP wants to create a new project:",
            f"<b>{escape(name)}</b>",
        ]
        if domain_allowlist:
            lines.append("Domains: " + ", ".join(escape(d) for d in domain_allowlist))
        lines.append(
            "Approve to create it — you'll get the API key here. "
            "The agent cannot create it without your confirmation."
        )
        await bot.send_message(
            chat_id=chat_id,
            text="\n".join(lines),
            parse_mode="HTML",
            reply_markup=approval_keyboard(request_id),
        )
    except Exception:
        logger.warning("failed to send project-create request notification", exc_info=True)
