"""SQLAlchemy ORM model for the project_create_requests table.

Confirmation flow for AI-agent-initiated project creation: an MCP tool
inserts a ``pending`` row and notifies the owning user in Telegram; the
bot's inline-keyboard callback either approves the request (creating the
real ``Project`` row and linking it via ``project_id``) or rejects it.
MCP clients poll the request status until it resolves. The project API
key is never stored here — it is minted only when the real project is
created on approval.
"""

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ProjectCreateRequest(Base):
    """A pending request to create a project, awaiting owner confirmation.

    ``status`` values (plain Text, no DB enum):

    - ``pending``  — inserted by the MCP tool, awaiting the owner's
      decision in Telegram.
    - ``approved`` — owner tapped Approve; the real project was created
      and ``project_id`` points at it.
    - ``rejected`` — owner tapped Reject; no project was created.
    - ``expired``  — the request timed out before the owner decided.
    """

    __tablename__ = "project_create_requests"

    id: Mapped[uuid.UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    domain_allowlist: Mapped[Any] = mapped_column(
        ARRAY(sa.Text()),
        server_default=sa.text("ARRAY[]::text[]"),
        nullable=False,
    )
    # Lifecycle state — see class docstring for the allowed values.
    status: Mapped[str] = mapped_column(
        sa.Text,
        server_default=sa.text("'pending'"),
        nullable=False,
    )
    # Audit trail of which surface asked for the project (e.g. ``mcp``).
    requested_via: Mapped[str] = mapped_column(
        sa.Text,
        server_default=sa.text("'mcp'"),
        nullable=False,
    )
    # Set when the request is approved and the real project is created.
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("projects.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        server_default=sa.text("now()"),
        nullable=False,
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=True,
    )
