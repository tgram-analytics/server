"""Pydantic schemas for Alert requests and responses."""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field, model_validator

from app.models.alert import AlertCondition


class AlertCreate(BaseModel):
    project_id: uuid.UUID
    event_name: str = Field(..., min_length=1)
    condition: AlertCondition
    threshold_n: int | None = Field(None, ge=1)

    @model_validator(mode="after")
    def threshold_required_for_every_n_and_threshold(self) -> "AlertCreate":
        if (
            self.condition in (AlertCondition.every_n, AlertCondition.threshold)
            and self.threshold_n is None
        ):
            raise ValueError(f"threshold_n is required when condition is '{self.condition.value}'")
        return self


class AlertResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    event_name: str
    condition: AlertCondition
    threshold_n: int | None
    counter: int
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}
