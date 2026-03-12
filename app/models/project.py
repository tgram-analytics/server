"""SQLAlchemy ORM model for the projects table."""

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Project(Base):
    """A tracked application — the top-level organisational unit."""

    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    # Raw API key is shown once on creation; only its SHA-256 hash is stored.
    api_key_hash: Mapped[str] = mapped_column(sa.Text, unique=True, nullable=False)
    admin_chat_id: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    domain_allowlist: Mapped[Any] = mapped_column(
        ARRAY(sa.Text()),
        server_default=sa.text("ARRAY[]::text[]"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        server_default=sa.text("now()"),
        nullable=False,
    )
