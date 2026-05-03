"""Append-only audit_events table + UPDATE/DELETE immutability trigger.

Phase 4.6. Adds the audit log surface that all destructive bot/API actions
will write to from Phase 6 onwards. Trigger guarantees the row history is
tamper-evident even from a compromised application role.

Revision ID: 0007
Revises: 0006
Create Date: 2026-04-27 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audit_events",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        # No FK to users — audit rows must survive user deletion, and the
        # immutability trigger below would block any cascading UPDATE/DELETE.
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("target_type", sa.String(64), nullable=False),
        sa.Column("target_id", sa.String(64), nullable=True),
        sa.Column(
            "metadata",
            JSONB,
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )

    # Append-only trigger: rejects UPDATE/DELETE for every row, every role.
    op.execute(
        sa.text(
            "CREATE OR REPLACE FUNCTION audit_events_block_mutation() "
            "RETURNS trigger AS $$ "
            "BEGIN "
            "  RAISE EXCEPTION 'audit_events is append-only (op=%)', TG_OP; "
            "END $$ LANGUAGE plpgsql;"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER audit_events_no_update_delete "
            "BEFORE UPDATE OR DELETE ON audit_events "
            "FOR EACH ROW EXECUTE FUNCTION audit_events_block_mutation();"
        )
    )


def downgrade() -> None:
    op.execute(sa.text("DROP TRIGGER IF EXISTS audit_events_no_update_delete ON audit_events;"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS audit_events_block_mutation();"))
    op.drop_table("audit_events")
