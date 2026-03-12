"""Pydantic request/response schemas."""

from app.schemas.aggregation import AggregationResponse
from app.schemas.alert import AlertCreate, AlertResponse
from app.schemas.bot_conversation_state import BotStateResponse, BotStateUpdate
from app.schemas.event import EventResponse, PageviewRequest, TrackEventRequest
from app.schemas.project import ProjectCreate, ProjectResponse, ProjectWithKeyResponse
from app.schemas.scheduled_report import ScheduledReportCreate, ScheduledReportResponse
from app.schemas.settings import SettingsResponse, SettingsUpdate

__all__ = [
    "ProjectCreate",
    "ProjectResponse",
    "ProjectWithKeyResponse",
    "TrackEventRequest",
    "PageviewRequest",
    "EventResponse",
    "AggregationResponse",
    "AlertCreate",
    "AlertResponse",
    "ScheduledReportCreate",
    "ScheduledReportResponse",
    "BotStateUpdate",
    "BotStateResponse",
    "SettingsUpdate",
    "SettingsResponse",
]
