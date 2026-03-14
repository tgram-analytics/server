"""Telegram webhook endpoint.

Telegram POSTs updates to /webhook/{token}.  The token in the URL acts as a
shared secret — any request with the wrong token is rejected with 403.
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from telegram import Update

from app.bot.setup import get_application
from app.core.config import Settings, get_settings

router = APIRouter(tags=["webhook"])


@router.post("/webhook/{token}", include_in_schema=False)
async def telegram_webhook(
    token: str,
    request: Request,
    settings: Settings = Depends(get_settings),
) -> dict:
    """Receive and dispatch a Telegram update."""
    if token != settings.telegram_bot_token:
        raise HTTPException(status_code=403, detail="Invalid webhook token")

    data = await request.json()
    application = get_application()
    update = Update.de_json(data, application.bot)
    await application.process_update(update)
    return {"ok": True}
