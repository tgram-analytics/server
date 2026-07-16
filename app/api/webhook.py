"""Telegram webhook endpoint.

Telegram POSTs updates to /webhook and authenticates by echoing the secret
configured via set_webhook(secret_token=...) in the
X-Telegram-Bot-Api-Secret-Token header. The bot token no longer appears in
the URL (it would otherwise leak into access logs).
"""

import hmac

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from telegram import Update

from app.bot.setup import get_application
from app.core.config import Settings, get_settings

router = APIRouter(tags=["webhook"])


@router.post("/webhook", include_in_schema=False)
async def telegram_webhook(
    request: Request,
    settings: Settings = Depends(get_settings),
    x_telegram_bot_api_secret_token: str = Header(default=""),
) -> dict[str, bool]:
    """Receive and dispatch a Telegram update."""
    # Compare on bytes: Starlette latin-1-decodes headers, so a non-ASCII
    # byte in the header yields a non-ASCII str, and hmac.compare_digest on
    # two str values raises TypeError unless both are pure ASCII. Encoding
    # both sides keeps the comparison constant-time and returns a clean 403.
    expected = settings.webhook_secret
    provided = x_telegram_bot_api_secret_token
    if not expected or not hmac.compare_digest(
        provided.encode("utf-8", "ignore"), expected.encode("utf-8")
    ):
        raise HTTPException(status_code=403, detail="Invalid webhook token")

    data = await request.json()
    application = get_application()
    update = Update.de_json(data, application.bot)
    await application.process_update(update)
    return {"ok": True}
