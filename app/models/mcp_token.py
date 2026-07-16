"""SQLAlchemy ORM model for MCP static access tokens.

Self-hosted installs authenticate MCP clients with long-lived static
bearer tokens created via the ``/mcp_token`` bot command. Only the
SHA-256 hash is stored; the raw ``mcp_<64 hex>`` value is shown once
at creation time (same policy as project API keys).
"""

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class MCPToken(Base):
    """A static MCP bearer token owned by a user."""

    __tablename__ = "mcp_tokens"

    id: Mapped[uuid.UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    token_hash: Mapped[str] = mapped_column(
        sa.Text,
        unique=True,
        nullable=False,
    )
    label: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        server_default=sa.text("now()"),
        nullable=False,
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=True,
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=True,
    )
