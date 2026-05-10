"""Pydantic schemas for Project requests and responses."""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.services.events import normalize_origin_entries


class ProjectCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    domain_allowlist: list[str] = Field(default_factory=list)

    @field_validator("domain_allowlist", mode="before")
    @classmethod
    def _normalize_allowlist(cls, v: object) -> list[str]:
        if v is None:
            return []
        if not isinstance(v, list):
            raise ValueError("domain_allowlist must be a list of strings")
        return normalize_origin_entries([str(x) for x in v])


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
