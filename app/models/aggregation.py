"""SQLAlchemy ORM model for the aggregations table.

Pre-computed rollups refreshed hourly by the scheduler. The composite
unique constraint prevents duplicate rows from concurrent cron runs.
"""

import uuid
from datetime import datetime
from enum import StrEnum

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class AggregationPeriod(StrEnum):
    hour = "hour"
    day = "day"
    week = "week"
    month = "month"


class Aggregation(Base):
    __tablename__ = "aggregations"

    __table_args__ = (
        sa.UniqueConstraint(
            "project_id",
            "event_name",
            "period",
            "period_start",
            name="uq_aggregations_composite",
        ),
        sa.Index(
            "ix_aggregations_lookup",
            "project_id",
            "event_name",
            "period",
            "period_start",
        ),
    )

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
    period: Mapped[AggregationPeriod] = mapped_column(
        sa.Enum(AggregationPeriod, name="aggregation_period"),
        nullable=False,
    )
    period_start: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
    )
    count: Mapped[int] = mapped_column(
        sa.Integer,
        nullable=False,
        server_default=sa.text("0"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        server_default=sa.text("now()"),
        nullable=False,
    )
