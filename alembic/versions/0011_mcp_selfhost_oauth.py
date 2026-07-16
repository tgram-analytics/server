"""Create mcp_selfhost_oauth_clients and mcp_selfhost_oauth_codes.

Revision ID: 0011
Revises: 0010
Create Date: 2026-07-09 00:00:00.000000

Names are mcp_selfhost_* because the cloud overlay owns mcp_oauth_* and
this chain also runs on cloud deploys (upgrade heads).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mcp_selfhost_oauth_clients",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("client_id", sa.Text, nullable=False, unique=True),
        sa.Column("client_name", sa.Text, nullable=False, server_default=""),
        sa.Column("redirect_uris", ARRAY(sa.Text), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_table(
        "mcp_selfhost_oauth_codes",
        sa.Column("code", sa.Text, primary_key=True),
        sa.Column(
            "user_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "client_id",
            sa.Text,
            sa.ForeignKey("mcp_selfhost_oauth_clients.client_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("redirect_uri", sa.Text, nullable=False),
        sa.Column("code_challenge", sa.Text, nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("mcp_selfhost_oauth_codes")
    op.drop_table("mcp_selfhost_oauth_clients")
