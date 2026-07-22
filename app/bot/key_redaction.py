"""Delayed redaction of secrets revealed in bot messages.

Project API keys (``proj_<64 hex>``) and MCP tokens (``mcp_<64 hex>``) are
shown exactly once — but the Telegram message that reveals them lives in
the chat forever, and Telegram chats get screen-shared, forwarded and
backed up. This module edits that message a few minutes after sending,
replacing every secret in it with a masked stub, and offers a "Hide now"
button for owners who have already saved the key.

Redaction is text-driven, not call-site-driven: :func:`redact_secrets`
rewrites *every* secret-shaped run in the message body, so a message that
embeds the same key twice (``/add`` puts it in both the env block and the
curl snippet) is fully covered, and future reveal sites inherit the
behaviour for free.

Timers live in-process (an ``asyncio`` task per pending message): a
restart inside the window drops them and the message keeps its secret
until the owner taps Hide or rotates the key. That trade-off is
deliberate — persisting pending redactions would need a table and a
startup sweep for a window measured in minutes.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

# Both secrets are minted as ``<prefix>_`` + ``secrets.token_hex(32)`` —
# see app.core.security.generate_api_key and app.services.mcp_tokens.
_SECRET_RE = re.compile(r"\b(proj|mcp)_[0-9a-f]{64}\b")

REDACT_AFTER_SECONDS = 300
HIDE_CALLBACK_DATA = "hidekey"
HIDE_PATTERN = r"^hidekey$"

# (chat_id, message_id) -> pending redaction. Holds a strong reference to
# the timer task so it isn't garbage-collected mid-sleep.
_pending: dict[tuple[int, int], _Pending] = {}


class _Pending:
    __slots__ = ("text", "markup", "task")

    def __init__(
        self,
        text: str,
        markup: InlineKeyboardMarkup | None,
        task: asyncio.Task[None] | None,
    ) -> None:
        self.text = text
        self.markup = markup
        self.task = task


def mask_secret(secret: str) -> str:
    """Return *secret* with its body masked: ``proj_••••706cc`` style.

    Keeps the prefix (so the reader can tell an API key from an MCP token)
    and the last four characters (so they can tell *which* key it was),
    which is not enough to reconstruct the other 60.
    """
    prefix, _, body = secret.partition("_")
    return f"{prefix}_••••{body[-4:]}"


def redact_secrets(text: str) -> str:
    """Mask every API key / MCP token occurrence in *text*."""
    return _SECRET_RE.sub(lambda m: mask_secret(m.group(0)), text)


def contains_secret(text: str) -> bool:
    """True when *text* has at least one unmasked secret in it."""
    return _SECRET_RE.search(text) is not None


def hide_button() -> InlineKeyboardButton:
    return InlineKeyboardButton("🙈 Hide key now", callback_data=HIDE_CALLBACK_DATA)


def with_hide_button(markup: InlineKeyboardMarkup | None) -> InlineKeyboardMarkup:
    """Return *markup* with a "Hide key now" row appended."""
    rows = list(markup.inline_keyboard) if markup is not None else []
    return InlineKeyboardMarkup([*rows, [hide_button()]])


def schedule_redaction(
    bot: Bot,
    chat_id: int,
    message_id: int,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
    delay: float | None = None,
) -> asyncio.Task[None] | None:
    """Redact the secrets in *text* on message *message_id* after *delay*.

    *text* is the message as sent; only its redacted form is retained, so
    the plaintext secret does not linger in process memory. *reply_markup*
    is the keyboard the message should carry **after** redaction — pass the
    original keyboard without the Hide button.

    Returns the timer task, or ``None`` when *text* holds no secret — that
    case is a no-op, so callers can apply this blindly.
    """
    if not contains_secret(text):
        return None

    key = (chat_id, message_id)
    existing = _pending.pop(key, None)
    if existing is not None and existing.task is not None:
        existing.task.cancel()

    wait = REDACT_AFTER_SECONDS if delay is None else delay
    entry = _Pending(redact_secrets(text), reply_markup, None)
    _pending[key] = entry
    entry.task = asyncio.create_task(_redact_after(bot, chat_id, message_id, wait))
    return entry.task


async def _redact_after(bot: Bot, chat_id: int, message_id: int, delay: float) -> None:
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.sleep(delay)
        await redact_message(bot, chat_id, message_id)


async def redact_message(bot: Bot, chat_id: int, message_id: int) -> bool:
    """Rewrite the tracked message with its secrets masked.

    Returns False when the message isn't tracked (already redacted, or
    scheduled by a process that has since restarted).
    """
    entry = _pending.pop((chat_id, message_id), None)
    if entry is None:
        return False
    if entry.task is not None and entry.task is not asyncio.current_task():
        entry.task.cancel()

    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=entry.text,
            parse_mode="HTML",
            reply_markup=entry.markup,
        )
    except TelegramError:
        # Message deleted, or already carries this exact text. Either way
        # the secret is no longer on screen in a form we can improve.
        logger.warning(
            "could not redact secret in message %s/%s", chat_id, message_id, exc_info=True
        )
        return False
    return True


async def hide_key_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the "Hide key now" button — redact immediately.

    Deliberately ungated: the only effect is removing a secret from a
    message this process already tracks, in the chat that message lives in.
    """
    query = update.callback_query
    assert query is not None
    message = query.message
    if message is None:
        await query.answer()
        return

    # ``message`` may be an InaccessibleMessage (older than 48h from the
    # bot's view) — both variants carry ``chat`` and ``message_id``, which
    # is all edit_message_text needs.
    done = await redact_message(query.get_bot(), message.chat.id, message.message_id)
    await query.answer("Key hidden." if done else "Already hidden.")


def pending_count() -> int:
    """Number of messages awaiting redaction. Introspection for tests."""
    return len(_pending)


def cancel_pending() -> None:
    """Drop all pending redactions. Test helper / shutdown hook."""
    for entry in _pending.values():
        if entry.task is not None:
            entry.task.cancel()
    _pending.clear()
