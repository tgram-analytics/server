"""SQLAlchemy ORM model for the alert_deliveries table.

One row per alert notification the server attempted to send. Columns
snapshot the alert's configuration at fire time so history survives
alert deletion (``alert_id`` becomes NULL via ON DELETE SET NULL).
"""

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.alert import AlertCondition


class AlertDelivery(Base):
    __tablename__ = "alert_deliveries"

    id: Mapped[uuid.UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    alert_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("alerts.id", ondelete="SET NULL"),
        nullable=True,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    # Reuse the PG enum created in migration 0001; do not create it again.
    condition: Mapped[AlertCondition] = mapped_column(
        postgresql.ENUM(AlertCondition, name="alert_condition", create_type=False),
        nullable=False,
    )
    threshold_n: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    fired_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        server_default=sa.text("now()"),
        nullable=False,
    )
    delivered: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    # Exception class name when the Telegram send failed; NULL on success.
    error: Mapped[str | None] = mapped_column(sa.Text, nullable=True)

    __table_args__ = (
        sa.Index(
            "ix_alert_deliveries_project_fired",
            "project_id",
            sa.text("fired_at DESC"),
        ),
    )
