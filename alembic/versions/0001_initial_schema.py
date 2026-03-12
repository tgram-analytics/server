"""Initial schema — all tables for tg-analytics v1.

Revision ID: 0001
Revises:
Create Date: 2025-01-01 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── Enum types ────────────────────────────────────────────────────────────
    sa.Enum("hour", "day", "week", "month", name="aggregation_period").create(
        op.get_bind(), checkfirst=True
    )
    sa.Enum("every", "every_n", "threshold", name="alert_condition").create(
        op.get_bind(), checkfirst=True
    )
    sa.Enum("daily", "weekly", "monthly", name="report_frequency").create(
        op.get_bind(), checkfirst=True
    )
    sa.Enum("7d", "30d", "90d", "1y", name="chart_period").create(op.get_bind(), checkfirst=True)

    # ── projects ──────────────────────────────────────────────────────────────
    op.create_table(
        "projects",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("api_key_hash", sa.Text(), nullable=False),
        sa.Column("admin_chat_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "domain_allowlist",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("ARRAY[]::text[]"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("api_key_hash"),
    )

    # ── events ────────────────────────────────────────────────────────────────
    op.create_table(
        "events",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("event_name", sa.Text(), nullable=False),
        sa.Column(
            "properties",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("referrer", sa.Text(), nullable=True),
        sa.Column(
            "timestamp",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_events_project_event_ts",
        "events",
        ["project_id", "event_name", "timestamp"],
    )
    op.create_index("ix_events_session_id", "events", ["session_id"])

    # ── aggregations ──────────────────────────────────────────────────────────
    op.create_table(
        "aggregations",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("event_name", sa.Text(), nullable=False),
        sa.Column(
            "period",
            sa.Enum("hour", "day", "week", "month", name="aggregation_period"),
            nullable=False,
        ),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "project_id",
            "event_name",
            "period",
            "period_start",
            name="uq_aggregations_composite",
        ),
    )
    op.create_index(
        "ix_aggregations_lookup",
        "aggregations",
        ["project_id", "event_name", "period", "period_start"],
    )

    # ── alerts ────────────────────────────────────────────────────────────────
    op.create_table(
        "alerts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("event_name", sa.Text(), nullable=False),
        sa.Column(
            "condition",
            sa.Enum("every", "every_n", "threshold", name="alert_condition"),
            nullable=False,
        ),
        sa.Column("threshold_n", sa.Integer(), nullable=True),
        sa.Column(
            "counter",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )

    # ── scheduled_reports ─────────────────────────────────────────────────────
    op.create_table(
        "scheduled_reports",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("event_name", sa.Text(), nullable=False),
        sa.Column(
            "frequency",
            sa.Enum("daily", "weekly", "monthly", name="report_frequency"),
            nullable=False,
        ),
        sa.Column(
            "chart_period",
            sa.Enum("7d", "30d", "90d", "1y", name="chart_period"),
            nullable=False,
        ),
        sa.Column("last_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_send_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )

    # ── bot_conversation_state ────────────────────────────────────────────────
    op.create_table(
        "bot_conversation_state",
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("flow", sa.Text(), nullable=True),
        sa.Column("step", sa.Text(), nullable=True),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("chat_id"),
    )

    # ── settings ──────────────────────────────────────────────────────────────
    op.create_table(
        "settings",
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column(
            "retention_days",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("90"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("project_id"),
    )


def downgrade() -> None:
    op.drop_table("settings")
    op.drop_table("bot_conversation_state")
    op.drop_table("scheduled_reports")
    op.drop_table("alerts")
    op.drop_index("ix_aggregations_lookup", table_name="aggregations")
    op.drop_table("aggregations")
    op.drop_index("ix_events_session_id", table_name="events")
    op.drop_index("ix_events_project_event_ts", table_name="events")
    op.drop_table("events")
    op.drop_table("projects")

    # Drop enum types (order does not matter — they have no dependents now).
    sa.Enum(name="chart_period").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="report_frequency").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="alert_condition").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="aggregation_period").drop(op.get_bind(), checkfirst=True)
