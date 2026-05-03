"""SQLAlchemy ORM model for the users table.

A ``User`` represents the Telegram account that owns one or more
projects. The default OSS install bootstraps a single user from
``ADMIN_CHAT_ID`` at startup; deployments that register a custom
resolver via :func:`app.extensions.register_user_resolver` may create
``User`` rows on demand from incoming updates.
"""

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class User(Base):
    """A Telegram account that owns projects."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    telegram_user_id: Mapped[int] = mapped_column(
        sa.BigInteger,
        unique=True,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        server_default=sa.text("now()"),
        nullable=False,
    )
