"""SQLAlchemy ORM model for the events table.

Raw event log — immutable after insert. All queries should use the
composite index on (project_id, event_name, timestamp).
"""

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Event(Base):
    __tablename__ = "events"

    __table_args__ = (
        # Primary query pattern: filter by project + event + time window.
        sa.Index("ix_events_project_event_ts", "project_id", "event_name", "timestamp"),
        # Used for session-based deduplication.
        sa.Index("ix_events_session_id", "session_id"),
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
    # Freeform JSONB bag — no schema enforcement; empty by default.
    properties: Mapped[Any] = mapped_column(
        JSONB,
        server_default=sa.text("'{}'::jsonb"),
        nullable=False,
    )
    session_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    # Populated only for pageview events.
    url: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    referrer: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    # Client-supplied timestamp (defaults to server now() when absent).
    timestamp: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        server_default=sa.text("now()"),
        nullable=False,
    )
    # Always server time — used for ordering and deduplication.
    received_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        server_default=sa.text("now()"),
        nullable=False,
    )
