"""Pydantic schemas for aggregation responses."""

import uuid
from datetime import datetime

from pydantic import BaseModel

from app.models.aggregation import AggregationPeriod


class AggregationResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    event_name: str
    period: AggregationPeriod
    period_start: datetime
    count: int
    updated_at: datetime

    model_config = {"from_attributes": True}
