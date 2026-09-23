"""Helpers for Telegram chat ids.

Telegram documents user ids as having at most 52 significant bits
(https://core.telegram.org/bots/api#user), and group/channel chat ids are
negative. A chat id at or above ``2**52`` can therefore never be a real
chat. Such ids are used on purpose for accounts that have no Telegram
chat (for example a demo account), so notification code skips the
Telegram call for them instead of making a request that is certain to fail.
"""

from __future__ import annotations

TELEGRAM_USER_ID_CEILING = 2**52


def is_unreachable_chat_id(chat_id: int) -> bool:
    """Return ``True`` if *chat_id* cannot belong to any Telegram chat."""
    return chat_id >= TELEGRAM_USER_ID_CEILING
