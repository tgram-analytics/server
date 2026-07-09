"""ORM models for the self-host MCP OAuth layer.

Table names carry the ``mcp_selfhost_`` prefix because the cloud overlay
already owns ``mcp_oauth_clients`` / ``mcp_authorization_codes`` and the
OSS migration chain also runs on cloud deploys (``alembic upgrade heads``).
"""

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class MCPSelfhostOAuthClient(Base):
    """A dynamically-registered OAuth client (RFC 7591). Public client, no secret."""

    __tablename__ = "mcp_selfhost_oauth_clients"

    id: Mapped[uuid.UUID] = mapped_column(
        sa.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    client_id: Mapped[str] = mapped_column(sa.Text, unique=True, nullable=False)
    client_name: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    redirect_uris: Mapped[list[str]] = mapped_column(ARRAY(sa.Text), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )


class MCPSelfhostOAuthCode(Base):
    """Single-use PKCE-bound authorization code (60s TTL)."""

    __tablename__ = "mcp_selfhost_oauth_codes"

    code: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    client_id: Mapped[str] = mapped_column(
        sa.Text,
        sa.ForeignKey("mcp_selfhost_oauth_clients.client_id", ondelete="CASCADE"),
        nullable=False,
    )
    redirect_uri: Mapped[str] = mapped_column(sa.Text, nullable=False)
    code_challenge: Mapped[str] = mapped_column(sa.Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
