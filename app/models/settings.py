"""SQLAlchemy ORM model for the settings table.

One row per project, created automatically alongside the project.
"""

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ProjectSettings(Base):
    __tablename__ = "settings"

    project_id: Mapped[uuid.UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("projects.id", ondelete="CASCADE"),
        primary_key=True,
    )
    # How long to retain raw events in days. 0 means keep forever.
    retention_days: Mapped[int] = mapped_column(
        sa.Integer,
        nullable=False,
        server_default=sa.text("90"),
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        server_default=sa.text("now()"),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        server_default=sa.text("now()"),
        nullable=False,
    )
