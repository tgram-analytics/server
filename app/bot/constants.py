"""Shared constants and helpers for bot handlers."""

from datetime import timedelta

from telegram import CallbackQuery, Message

PERIODS: dict[str, timedelta] = {
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
    "90d": timedelta(days=90),
}

PERIOD_LABEL: dict[str, str] = {
    "7d": "last 7 days",
    "30d": "last 30 days",
    "90d": "last 90 days",
}

TIME_WINDOWS: dict[str, int] = {
    "5min": 300,
    "1h": 3600,
    "24h": 86400,
    "7d": 604800,
}

TIME_WINDOW_LABEL: dict[str, str] = {
    "5min": "5 minutes",
    "1h": "1 hour",
    "24h": "24 hours",
    "7d": "7 days",
}


async def escape_photo(query: CallbackQuery) -> CallbackQuery:
    """If the callback originates from a photo message, delete it and replace
    the underlying message with a plain text placeholder so downstream handlers
    can safely call ``edit_message_text``.

    Returns *query* unchanged when the message is already a text message.
    """
    if not isinstance(query.message, Message) or not query.message.photo:
        return query
    chat_id = query.message.chat_id
    await query.message.delete()
    placeholder = await query.get_bot().send_message(chat_id, "\u23f3")
    query._unfreeze()  # noqa: SLF001
    query.message = placeholder
    query._freeze()  # noqa: SLF001
    return query
