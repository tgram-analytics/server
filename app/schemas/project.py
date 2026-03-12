"""Pydantic schemas for Project requests and responses."""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class ProjectCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    domain_allowlist: list[str] = Field(default_factory=list)


class ProjectResponse(BaseModel):
    id: uuid.UUID
    name: str
    admin_chat_id: int
    domain_allowlist: list[str]
    created_at: datetime

    model_config = {"from_attributes": True}


class ProjectWithKeyResponse(ProjectResponse):
    """Returned only on creation — includes the plaintext API key."""

    api_key: str
