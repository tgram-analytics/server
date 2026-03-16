"""Telegram Application factory and process-level lifecycle management.

Supports webhook mode (production) and long-polling mode (local dev, no
public URL needed).  Mode is selected at runtime based on whether
WEBHOOK_BASE_URL is configured.
"""

from __future__ import annotations

import logging

from telegram import Bot
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

logger = logging.getLogger(__name__)

_application: Application | None = None


def build_application(token: str, admin_chat_id: int) -> Application:
    """Build an Application with all handlers registered.

    Uses ``updater=None`` so updates arrive via the webhook endpoint, not
    Telegram's long-polling.  Handlers are restricted to messages/callbacks
    from the configured admin chat ID only.
    """
    from app.bot.handlers.alerts import alert_callback, handle_text_message
    from app.bot.handlers.events import events_callback, events_command
    from app.bot.handlers.projects import add_command, project_callback, projects_command
    from app.bot.handlers.reports import report_command
    from app.bot.handlers.system import cancel_command, help_command, start_command

    admin_filter = filters.Chat(chat_id=admin_chat_id)

    app = ApplicationBuilder().token(token).updater(None).build()

    app.add_handler(CommandHandler("start", start_command, filters=admin_filter))
    app.add_handler(CommandHandler("help", help_command, filters=admin_filter))
    app.add_handler(CommandHandler("cancel", cancel_command, filters=admin_filter))
    app.add_handler(CommandHandler("add", add_command, filters=admin_filter))
    app.add_handler(CommandHandler("projects", projects_command, filters=admin_filter))
    app.add_handler(CommandHandler("events", events_command, filters=admin_filter))
    app.add_handler(CommandHandler("report", report_command, filters=admin_filter))

    # Callback queries don't support CommandHandler filters directly — we
    # guard inside the handler using the same admin_chat_id check.
    # Pattern-matched handlers first, then catch-all project callbacks.
    app.add_handler(CallbackQueryHandler(alert_callback, pattern=r"^(alert_|back:alerts:)"))
    app.add_handler(CallbackQueryHandler(events_callback, pattern=r"^(evt[a:]|back:events)"))
    app.add_handler(CallbackQueryHandler(project_callback))

    # Text messages for multi-step conversation flows (e.g., add-alert)
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & admin_filter, handle_text_message)
    )

    return app


def get_application() -> Application:
    """Return the running Application singleton."""
    if _application is None:
        raise RuntimeError("Bot application not initialised. Call init_bot() first.")
    return _application


def get_bot() -> Bot:
    return get_application().bot


async def init_bot(token: str, admin_chat_id: int, webhook_base_url: str = "") -> None:
    """Initialise the bot application and optionally register the webhook."""
    global _application
    _application = build_application(token, admin_chat_id)
    await _application.initialize()
    await _application.start()

    if webhook_base_url:
        webhook_url = f"{webhook_base_url.rstrip('/')}/webhook/{token}"
        await _application.bot.set_webhook(
            url=webhook_url,
            allowed_updates=["message", "callback_query"],
            drop_pending_updates=True,
        )
        logger.info("Webhook registered at %s", webhook_url)
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
