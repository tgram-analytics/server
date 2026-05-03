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
    # Legacy single-tenant owner reference — Telegram chat id of the admin.
    # Retained for back-compat while handlers are migrated off it; the
    # authoritative owner link is ``owner_user_id`` → ``users.id``.
    admin_chat_id: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    # Nullable during the rollout window so the migration can add the column
    # first and backfill next. After backfill it is effectively NOT NULL; a
    # later migration (post-rollout) can enforce that at the DB level.
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
    )
    domain_allowlist: Mapped[Any] = mapped_column(
        ARRAY(sa.Text()),
        server_default=sa.text("ARRAY[]::text[]"),
        nullable=False,
    )
    # Per-project ingestion rate limit (requests/sec). NULL = use the
    # server-wide ``Settings.rate_limit_per_second`` default. Cloud
    # overlays set this at create time so free-tier projects get a
    # tighter cap than the OSS default; admins can override per project
    # for VIP customers or for abusive accounts.
    rate_limit_per_second: Mapped[int | None] = mapped_column(
        sa.Integer,
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        server_default=sa.text("now()"),
        nullable=False,
    )
