"""SQLAlchemy ORM model for the scheduled_reports table."""

import uuid
from datetime import datetime
from enum import StrEnum

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ReportFrequency(StrEnum):
    daily = "daily"
    weekly = "weekly"
    monthly = "monthly"


class ChartPeriod(StrEnum):
    seven_days = "7d"
    thirty_days = "30d"
    ninety_days = "90d"
    one_year = "1y"


class ScheduledReport(Base):
    __tablename__ = "scheduled_reports"

    id: Mapped[uuid.UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    frequency: Mapped[ReportFrequency] = mapped_column(
        sa.Enum(ReportFrequency, name="report_frequency"),
        nullable=False,
    )
    chart_period: Mapped[ChartPeriod] = mapped_column(
        sa.Enum(ChartPeriod, name="chart_period"),
        nullable=False,
    )
    last_sent_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=True,
    )
    next_send_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=True,
    )
    is_active: Mapped[bool] = mapped_column(
        sa.Boolean,
        nullable=False,
        server_default=sa.text("true"),
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        server_default=sa.text("now()"),
        nullable=False,
    )
