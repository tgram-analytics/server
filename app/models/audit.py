"""Append-only audit trail of user-initiated actions.

Postgres triggers (migration 0007) reject UPDATE/DELETE on this table; the
application MUST treat it as insert-only. The Python attribute is named
``metadata_json`` because SQLAlchemy reserves ``metadata`` on ``Base``;
the column name in the DB stays ``metadata``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[uuid.UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    )
    # No FK to users — audit rows must survive user deletion, and the
    # immutability trigger would block any cascading UPDATE/DELETE anyway.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.UUID(as_uuid=True),
        nullable=True,
    )
    action: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    target_type: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    target_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    metadata_json: Mapped[Any] = mapped_column(
        "metadata",
        JSONB,
        server_default=sa.text("'{}'::jsonb"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        server_default=sa.text("now()"),
        nullable=False,
    )
