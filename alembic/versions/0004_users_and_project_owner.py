"""Create users table and link projects to an owning user.

Revision ID: 0004
Revises: 0003
Create Date: 2026-04-24 00:00:00.000000

Back-compat: ``projects.admin_chat_id`` is retained. This migration adds a
``users`` table and a nullable ``projects.owner_user_id`` FK, then backfills
one ``User`` row per distinct ``admin_chat_id`` and populates the FK.

A later migration (post-rollout) will enforce NOT NULL on ``owner_user_id``
and eventually drop ``admin_chat_id`` once all handlers query by user.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # gen_random_uuid() ships in PostgreSQL 13+ core, and is also provided
    # by the pgcrypto extension on older versions. Enable pgcrypto
    # defensively so this migration works on both. No-op if already enabled.
    op.execute(sa.text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))

    # ── users ────────────────────────────────────────────────────────────────
    op.create_table(
        "users",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("telegram_user_id", name="uq_users_telegram_user_id"),
    )

    # ── projects.owner_user_id (nullable FK) ─────────────────────────────────
    op.add_column(
        "projects",
        sa.Column("owner_user_id", UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_projects_owner_user_id",
        source_table="projects",
        referent_table="users",
        local_cols=["owner_user_id"],
        remote_cols=["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_projects_owner_user_id",
        "projects",
        ["owner_user_id"],
    )

    # ── backfill ─────────────────────────────────────────────────────────────
    # One User per distinct admin_chat_id, then populate owner_user_id.
    # Idempotent via ON CONFLICT on the unique telegram_user_id.
    op.execute(
        sa.text(
            """
            INSERT INTO users (id, telegram_user_id)
            SELECT gen_random_uuid(), admin_chat_id
            FROM (SELECT DISTINCT admin_chat_id FROM projects) AS d
            ON CONFLICT (telegram_user_id) DO NOTHING
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE projects p
            SET owner_user_id = u.id
            FROM users u
            WHERE u.telegram_user_id = p.admin_chat_id
              AND p.owner_user_id IS NULL
            """
        )
    )


def downgrade() -> None:
    op.drop_index("ix_projects_owner_user_id", table_name="projects")
    op.drop_constraint("fk_projects_owner_user_id", "projects", type_="foreignkey")
    op.drop_column("projects", "owner_user_id")
    op.drop_table("users")
