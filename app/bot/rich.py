"""Rich-message sending with graceful degradation.

Bot API 10.1 (June 2026) added ``sendRichMessage``: structured content
with headings, real tables, and collapsible ``<details>`` sections.
There is no server-side fallback — clients older than 10.1 render a
"message not supported" placeholder, and older Bot API servers reject
the method outright — so every rich send here degrades to a classic
``sendMessage`` with HTML parse mode on any failure.

Use rich messages only where the structure earns its keep (dense
reports like /digest and /doctor). Interactive messages that need
inline keyboards, and short conversational replies, stay on plain
``sendMessage``.
"""

from __future__ import annotations

import logging

from telegram import Message

logger = logging.getLogger(__name__)


async def _call_send_rich_message(message: Message, rich_html: str) -> None:
    """Raw ``sendRichMessage`` call; raises on any API/network error."""
    await message.get_bot().do_api_request(
        "sendRichMessage",
        api_kwargs={
            "chat_id": message.chat_id,
            "rich_message": {"html": rich_html},
        },
    )


async def reply_rich_html(message: Message, rich_html: str, fallback_text: str) -> None:
    """Reply with a rich HTML message, degrading to plain HTML.

    ``rich_html`` may use the extended tag set (h1-h6, table, details);
    ``fallback_text`` must stay within classic HTML parse-mode entities.
    """
    try:
        await _call_send_rich_message(message, rich_html)
    except Exception:
        logger.info("sendRichMessage failed, falling back to sendMessage", exc_info=True)
        await message.reply_text(fallback_text, parse_mode="HTML")
