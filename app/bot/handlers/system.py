"""System command handlers: /start, /help, /cancel."""

from telegram import Update
from telegram.ext import ContextTypes

from app.bot.states import BotStateService
from app.core.database import get_session_factory

_HELP_TEXT = (
    "📖 <b>Available commands</b>\n\n"
    "/add <i>name</i> — create a new project and get its API key\n"
    "/projects — list all your projects\n"
    "/help — show this message\n"
    "/cancel — cancel the current operation\n"
)


async def start_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message is not None
    await update.message.reply_text(
        "👋 <b>Welcome to tg-analytics!</b>\n\n"
        "Self-hosted analytics you control via Telegram.\n\n"
        "Use /add <i>name</i> to create your first project and get an API key.\n"
        "Use /help for a full list of commands.",
        parse_mode="HTML",
    )


async def help_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message is not None
    await update.message.reply_text(_HELP_TEXT, parse_mode="HTML")


async def cancel_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message is not None
    chat_id = update.effective_chat.id  # type: ignore[union-attr]

    factory = get_session_factory()
    async with factory() as session:
        svc = BotStateService(session)
        await svc.clear(chat_id)
        await session.commit()

    await update.message.reply_text("✅ Operation cancelled.")
