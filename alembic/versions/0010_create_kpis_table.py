"""Create kpis table for pinned per-project KPIs and the North Star metric.

Revision ID: 0010
Revises: 0009
Create Date: 2026-06-10 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "kpis",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id",
            UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_name", sa.Text, nullable=False),
        sa.Column(
            "is_north_star",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("position", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("project_id", "event_name", name="uq_kpis_project_event"),
    )
    op.create_index("ix_kpis_project_id", "kpis", ["project_id"])
    op.create_index(
        "uq_kpis_project_north_star",
        "kpis",
        ["project_id"],
        unique=True,
        postgresql_where=sa.text("is_north_star"),
    )


def downgrade() -> None:
    op.drop_index("uq_kpis_project_north_star", table_name="kpis")
    op.drop_index("ix_kpis_project_id", table_name="kpis")
    op.drop_table("kpis")
