"""Pydantic schemas for ProjectSettings requests and responses."""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class SettingsUpdate(BaseModel):
    retention_days: int = Field(..., ge=0, le=3650)


class SettingsResponse(BaseModel):
    project_id: uuid.UUID
    retention_days: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
