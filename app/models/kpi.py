"""SQLAlchemy ORM model for the kpis table."""

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Kpi(Base):
    __tablename__ = "kpis"

    __table_args__ = (
        # An event can be pinned at most once per project.
        sa.UniqueConstraint("project_id", "event_name", name="uq_kpis_project_event"),
        # At most one North Star per project, enforced at the DB level.
        sa.Index(
            "uq_kpis_project_north_star",
            "project_id",
            unique=True,
            postgresql_where=sa.text("is_north_star"),
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
    is_north_star: Mapped[bool] = mapped_column(
        sa.Boolean,
        nullable=False,
        default=False,
        server_default=sa.text("false"),
    )
    # Display order among non-North-Star KPIs (insertion order).
    position: Mapped[int] = mapped_column(
        sa.Integer,
        nullable=False,
        default=0,
        server_default=sa.text("0"),
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        server_default=sa.text("now()"),
        nullable=False,
    )
