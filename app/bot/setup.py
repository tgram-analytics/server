"""Telegram Application factory and process-level lifecycle management.

Supports webhook mode (production) and long-polling mode (local dev, no
public URL needed).  Mode is selected at runtime based on whether
WEBHOOK_BASE_URL is configured.
"""

from __future__ import annotations

import logging
from typing import Any

from telegram import Bot, BotCommand
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

logger = logging.getLogger(__name__)

_application: Application[Any, Any, Any, Any, Any, Any] | None = None


def build_application(token: str, admin_chat_id: int) -> Application[Any, Any, Any, Any, Any, Any]:
    """Build an Application with all handlers registered.

    Uses ``updater=None`` so updates arrive via the webhook endpoint, not
    Telegram's long-polling.  Handlers are restricted to messages/callbacks
    from the configured admin chat ID only.
    """
    from app.bot.handlers.alerts import alert_callback, alerts_command, handle_text_message
    from app.bot.handlers.digest import digest_command
    from app.bot.handlers.doctor import doctor_command
    from app.bot.handlers.events import events_callback, events_command
    from app.bot.handlers.funnels import funnel_callback
    from app.bot.handlers.mcp_tokens import mcp_token_callback, mcp_token_command
    from app.bot.handlers.onboarding import onboarding_callback
    from app.bot.handlers.overview import overview_command
    from app.bot.handlers.projects import add_command, project_callback, projects_command
    from app.bot.handlers.reports import report_command
    from app.bot.handlers.system import cancel_command, help_command, start_command

    # Defense-in-depth: every handler is also wrapped with ``@requires_user``
    # which resolves the current ``User`` and short-circuits unauthorised
    # callers. The ``filters.Chat(...)`` gate below is a cheap pre-filter
    # so PTB doesn't dispatch updates from non-admin chats. Deployments
    # that register a custom user resolver and additional filters via
    # :mod:`app.extensions` may want to widen the audience — see
    # :func:`app.extensions.register_bot_filter` for the AND-composition
    # contract.
    from app.extensions import get_bot_filters

    admin_filter: filters.BaseFilter = filters.Chat(chat_id=admin_chat_id)
    for extra in get_bot_filters():
        admin_filter = admin_filter & extra

    # Defaults (5s read/write) are too tight for media uploads like
    # edit_message_media on busy/slow networks — bump them so chart photo
    # edits don't surface as telegram.error.TimedOut.
    app = (
        ApplicationBuilder()
        .token(token)
        .updater(None)
        .connect_timeout(10.0)
        .read_timeout(20.0)
        .write_timeout(20.0)
        .media_write_timeout(60.0)
        .build()
    )

    app.add_handler(CommandHandler("start", start_command, filters=admin_filter))
    app.add_handler(CommandHandler("help", help_command, filters=admin_filter))
    app.add_handler(CommandHandler("cancel", cancel_command, filters=admin_filter))
    app.add_handler(CommandHandler("add", add_command, filters=admin_filter))
    app.add_handler(CommandHandler("projects", projects_command, filters=admin_filter))
    app.add_handler(CommandHandler("events", events_command, filters=admin_filter))
    app.add_handler(CommandHandler("report", report_command, filters=admin_filter))
    app.add_handler(CommandHandler("digest", digest_command, filters=admin_filter))
    app.add_handler(CommandHandler("overview", overview_command, filters=admin_filter))
    app.add_handler(CommandHandler("alerts", alerts_command, filters=admin_filter))
    app.add_handler(CommandHandler("doctor", doctor_command, filters=admin_filter))
    app.add_handler(CommandHandler("mcp_token", mcp_token_command, filters=admin_filter))

    # Callback queries don't support CommandHandler filters directly — we
    # guard inside the handler using the same admin_chat_id check.
    # Pattern-matched handlers first, then catch-all project callbacks.
    app.add_handler(CallbackQueryHandler(alert_callback, pattern=r"^(alert_|back:alerts:)"))
    app.add_handler(CallbackQueryHandler(events_callback, pattern=r"^(evt[a:]|back:events)"))
    app.add_handler(CallbackQueryHandler(funnel_callback, pattern=r"^(fnl_|back:funnels:)"))
    app.add_handler(CallbackQueryHandler(onboarding_callback, pattern=r"^onb:"))
    app.add_handler(CallbackQueryHandler(mcp_token_callback, pattern=r"^mcptok:"))
    app.add_handler(CallbackQueryHandler(project_callback))

    # Text messages for multi-step conversation flows (e.g., add-alert)
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & admin_filter, handle_text_message)
    )

    return app


def get_application() -> Application[Any, Any, Any, Any, Any, Any]:
    """Return the running Application singleton."""
    if _application is None:
        raise RuntimeError("Bot application not initialised. Call init_bot() first.")
    return _application


def get_bot() -> Bot:
    bot = get_application().bot
    assert isinstance(bot, Bot)
    return bot


async def init_bot(token: str, admin_chat_id: int, webhook_base_url: str = "") -> None:
    """Initialise the bot application and optionally register the webhook."""
    global _application

    # Bootstrap the singleton ``User`` for self-host mode. ``init_db`` has
    # already run by the time this is called (cf. app/main.py lifespan).
    # Local import to avoid circular imports during module load.
    from app.bot import auth as _auth
    from app.core.database import get_session_factory

    session_factory = get_session_factory()
    async with session_factory() as session:
        user = await _auth.ensure_singleton_user(session, admin_chat_id)
        await session.commit()
        _auth._singleton_user_id = user.id

    _application = build_application(token, admin_chat_id)
    await _application.initialize()
    await _application.start()

    await _application.bot.set_my_commands(
        [
            BotCommand("start", "Home menu"),
            BotCommand("projects", "List your projects"),
            BotCommand("events", "Browse event types"),
            BotCommand("report", "Chart for an event"),
            BotCommand("digest", "Weekly digest across all projects"),
            BotCommand("overview", "Visits chart across all projects"),
            BotCommand("alerts", "List active alerts"),
            BotCommand("add", "Create a new project"),
            BotCommand("doctor", "Health check across all projects"),
            BotCommand("help", "Show help"),
            BotCommand("cancel", "Cancel current operation"),
        ]
    )

    if webhook_base_url:
        webhook_url = f"{webhook_base_url.rstrip('/')}/webhook/{token}"
        await _application.bot.set_webhook(
            url=webhook_url,
            allowed_updates=["message", "callback_query"],
            drop_pending_updates=True,
        )
        masked = token[:8] + "..." if len(token) > 8 else "***"
        logger.info("Webhook registered at %s/webhook/%s", webhook_base_url.rstrip("/"), masked)
    else:
        logger.info(
            "WEBHOOK_BASE_URL not set — bot is in webhook-only mode "
            "(no updates will be received until a webhook is configured)"
        )


async def shutdown_bot() -> None:
    """Stop and tear down the bot application."""
    if _application is not None:
        await _application.stop()
        await _application.shutdown()
        logger.info("Bot application stopped")
