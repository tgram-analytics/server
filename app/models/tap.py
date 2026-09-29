"""SQLAlchemy ORM model for the taps table.

One row per tap (``kind = "tap"``) or one row per pageview for the maximum
scroll depth (``kind = "scroll"``). A row carries no session id, no visitor
hash and no client timestamp, so nothing in the table links two rows to
the same person. Queries filter by ``(project_id, path, received_at)``.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

TAP_KIND = "tap"
SCROLL_KIND = "scroll"


class Tap(Base):
    __tablename__ = "taps"

    __table_args__ = (sa.Index("ix_taps_project_path_ts", "project_id", "path", "received_at"),)

    id: Mapped[int] = mapped_column(
        sa.BigInteger,
        sa.Identity(always=True),
        primary_key=True,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Page path as the SDK sends it (pathname + search), same string as the
    # pageview url.
    path: Mapped[str] = mapped_column(sa.Text, nullable=False)
    # Viewport bucket from window.innerWidth: mobile | tablet | desktop.
    device: Mapped[str] = mapped_column(sa.String(8), nullable=False)
    # window.innerWidth in CSS px.
    vw: Mapped[int] = mapped_column(sa.SmallInteger, nullable=False)
    kind: Mapped[str] = mapped_column(
        sa.String(6), nullable=False, server_default=sa.text(f"'{TAP_KIND}'")
    )
    # Tap: fraction 0..1 of the document width.
    x: Mapped[float | None] = mapped_column(sa.REAL, nullable=True)
    # Tap: CSS px from the document top (viewport px for fixed/sticky targets).
    y: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    # Scroll: maximum scroll depth 0..1 for one pageview.
    depth: Mapped[float | None] = mapped_column(sa.REAL, nullable=True)
    # Tap: short element label.
    label: Mapped[str | None] = mapped_column(sa.String(80), nullable=True)
    # Always server time.
    received_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        server_default=sa.text("now()"),
        nullable=False,
    )
