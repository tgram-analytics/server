"""Pydantic schemas for event ingestion requests and responses."""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class TrackEventRequest(BaseModel):
    api_key: str = Field(..., min_length=1)
    event: str = Field(..., min_length=1, max_length=255, alias="event")
    session_id: str = Field(..., min_length=1)
    properties: dict[str, Any] = Field(default_factory=dict)
    # Optional client-supplied timestamp; server uses now() when absent.
    timestamp: datetime | None = None


class PageviewRequest(BaseModel):
    api_key: str = Field(..., min_length=1)
    session_id: str = Field(..., min_length=1)
    url: str = Field(..., min_length=1)
    referrer: str | None = None
    timestamp: datetime | None = None


class EventResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    event_name: str
    properties: dict[str, Any]
    session_id: str
    url: str | None
    referrer: str | None
    timestamp: datetime
    received_at: datetime

    model_config = {"from_attributes": True}
