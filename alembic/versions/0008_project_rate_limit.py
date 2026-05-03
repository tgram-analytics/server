"""Add per-project rate_limit_per_second override column.

A nullable integer column on ``projects``. ``NULL`` means "use the
server-wide ``Settings.rate_limit_per_second`` default" — backwards
compatible with existing rows. Set values are honored at ingestion
time (see ``app.api.ingestion._resolve_project``).

Cloud overlays set this at create time via the pre-create hook so
free-tier projects get a tighter cap than the OSS default. OSS admins
can also set it manually per project for VIP customers or to throttle
abusive accounts.

Revision ID: 0008
Revises: 0007
Create Date: 2026-05-03 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column("rate_limit_per_second", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("projects", "rate_limit_per_second")
