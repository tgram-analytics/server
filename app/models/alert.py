"""SQLAlchemy ORM model for the alerts table."""

import uuid
from datetime import datetime
from enum import StrEnum

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class AlertCondition(StrEnum):
    every = "every"
    every_n = "every_n"
    threshold = "threshold"


class Alert(Base):
    __tablename__ = "alerts"

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
    condition: Mapped[AlertCondition] = mapped_column(
        sa.Enum(AlertCondition, name="alert_condition"),
        nullable=False,
    )
    # Required for every_n and threshold conditions; NULL for 'every'.
    threshold_n: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    # Running count since last trigger — used by every_n.
    counter: Mapped[int] = mapped_column(
        sa.Integer,
        nullable=False,
        server_default=sa.text("0"),
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
