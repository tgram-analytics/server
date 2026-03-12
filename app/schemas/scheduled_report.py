"""Pydantic schemas for ScheduledReport requests and responses."""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.models.scheduled_report import ChartPeriod, ReportFrequency


class ScheduledReportCreate(BaseModel):
    project_id: uuid.UUID
    event_name: str = Field(..., min_length=1)
    frequency: ReportFrequency
    chart_period: ChartPeriod


class ScheduledReportResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    event_name: str
    frequency: ReportFrequency
    chart_period: ChartPeriod
    last_sent_at: datetime | None
    next_send_at: datetime | None
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}
