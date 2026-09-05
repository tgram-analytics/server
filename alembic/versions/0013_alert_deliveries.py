"""Create alert_deliveries table (alert notification history).

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-05 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "alert_deliveries",
        sa.Column(
            "id",
            sa.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "alert_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("alerts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "project_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_name", sa.Text(), nullable=False),
        sa.Column(
            "condition",
            postgresql.ENUM(
                "every",
                "every_n",
                "threshold",
                name="alert_condition",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("threshold_n", sa.Integer(), nullable=True),
        sa.Column(
            "fired_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("delivered", sa.Boolean(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_alert_deliveries_project_fired",
        "alert_deliveries",
        ["project_id", sa.text("fired_at DESC")],
    )
    op.create_index("ix_alert_deliveries_alert_id", "alert_deliveries", ["alert_id"])


def downgrade() -> None:
    op.drop_index("ix_alert_deliveries_alert_id", table_name="alert_deliveries")
    op.drop_index("ix_alert_deliveries_project_fired", table_name="alert_deliveries")
    op.drop_table("alert_deliveries")
