"""Add visitor_hash + parsed UA columns to events.

Phase 4.2 of the SaaS roadmap. Adds four nullable columns to ``events``:

* ``visitor_hash``  — 16-char SHA-256 truncation, daily-rotating, project-bound.
* ``browser``       — UA family (e.g. ``"Chrome"``).
* ``os``            — OS family (e.g. ``"Mac OS X"``).
* ``device_type``   — coarse bucket: ``mobile|tablet|desktop|bot|unknown``.

Plus a composite index ``(project_id, visitor_hash)`` to support
unique-visitor queries.

Expand-only: no backfill, columns are nullable, old rows keep ``NULL``.
The aggregation layer does not yet read these columns, so a slow rollout
where some rows are populated and some are not is safe.

Revision ID: 0006
Revises: 0005
Create Date: 2026-04-27 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("events", sa.Column("visitor_hash", sa.String(16), nullable=True))
    op.add_column("events", sa.Column("browser", sa.String(64), nullable=True))
    op.add_column("events", sa.Column("os", sa.String(64), nullable=True))
    op.add_column("events", sa.Column("device_type", sa.String(64), nullable=True))
    op.create_index(
        "ix_events_visitor_hash_project",
        "events",
        ["project_id", "visitor_hash"],
    )


def downgrade() -> None:
    op.drop_index("ix_events_visitor_hash_project", table_name="events")
    op.drop_column("events", "device_type")
    op.drop_column("events", "os")
    op.drop_column("events", "browser")
    op.drop_column("events", "visitor_hash")
