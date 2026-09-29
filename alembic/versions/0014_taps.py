"""Create taps table (tap positions and scroll depth per page).

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-29 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "taps",
        sa.Column(
            "id",
            sa.BigInteger(),
            sa.Identity(always=True),
            primary_key=True,
        ),
        sa.Column(
            "project_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("device", sa.String(8), nullable=False),
        sa.Column("vw", sa.SmallInteger(), nullable=False),
        sa.Column("kind", sa.String(6), server_default=sa.text("'tap'"), nullable=False),
        sa.Column("x", sa.REAL(), nullable=True),
        sa.Column("y", sa.Integer(), nullable=True),
        sa.Column("depth", sa.REAL(), nullable=True),
        sa.Column("label", sa.String(80), nullable=True),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_taps_project_path_ts",
        "taps",
        ["project_id", "path", "received_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_taps_project_path_ts", table_name="taps")
    op.drop_table("taps")
