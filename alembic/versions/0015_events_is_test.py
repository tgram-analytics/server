"""Add events.is_test (test events are stored but excluded from analytics).

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-01 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Constant default: metadata-only on Postgres 11+, no table rewrite.
    op.add_column(
        "events",
        sa.Column("is_test", sa.Boolean(), server_default=sa.false(), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("events", "is_test")
