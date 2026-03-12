"""Pydantic schemas for BotConversationState."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel


class BotStateUpdate(BaseModel):
    flow: str | None = None
    step: str | None = None
    payload: dict[str, Any] = {}


class BotStateResponse(BaseModel):
    chat_id: int
    flow: str | None
    step: str | None
    payload: dict[str, Any]
    updated_at: datetime

    model_config = {"from_attributes": True}
