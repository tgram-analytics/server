"""Enforce NOT NULL on projects.owner_user_id.

Phase 3.5 of the SaaS roadmap. Runs after all handlers + services + the
internal API have been refactored off ``admin_chat_id`` (Phases 3.1–3.4).
Migration 0004 has already backfilled ``owner_user_id`` for every existing
row, and every code path that creates a project now populates it, so the
column can safely become ``NOT NULL``.

The ``admin_chat_id`` column is intentionally NOT dropped here — it stays
as a back-compat column for one release cycle so a rollback to pre-0005
code keeps working. A later migration (post-rollout) drops it.

Revision ID: 0005
Revises: 0004
Create Date: 2026-04-24 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Pre-flight: fail loudly with a self-explanatory error if any project
    # still has a NULL owner. Without this, the ALTER below would surface
    # only as a generic "column ... contains null values" error from
    # Postgres, which is harder to triage.
    op.execute(
        sa.text(
            "DO $$ BEGIN "
            "  IF EXISTS (SELECT 1 FROM projects WHERE owner_user_id IS NULL) THEN "
            "    RAISE EXCEPTION 'projects.owner_user_id has NULLs — "
            "backfill (migration 0004) must run first'; "
            "  END IF; "
            "END $$;"
        )
    )
    op.alter_column(
        "projects",
        "owner_user_id",
        existing_type=sa.dialects.postgresql.UUID(as_uuid=True),
        nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "projects",
        "owner_user_id",
        existing_type=sa.dialects.postgresql.UUID(as_uuid=True),
        nullable=True,
    )
