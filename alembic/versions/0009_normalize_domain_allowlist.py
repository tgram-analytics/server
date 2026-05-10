"""Backfill: normalize ``projects.domain_allowlist`` entries to bare hosts.

Historical entries were stored as the user typed them — mixing
``https://example.com``, ``example.com``, ``EXAMPLE.com/``, etc.
``is_origin_allowed`` already tolerates this at request time, but the
inconsistency leaks into the bot's ``/doctor`` output and ``/settings``
display. This migration rewrites every row through the same normalizer
the application now applies on write.

Idempotent: running again is a no-op for already-normalized rows.
No-op downgrade — we cannot reconstruct the original strings.

Revision ID: 0009
Revises: 0008
Create Date: 2026-05-10 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY

from alembic import op
from app.services.events import normalize_origin_entries

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT id, domain_allowlist FROM projects")).fetchall()

    for project_id, allowlist in rows:
        if not allowlist:
            continue
        normalized = normalize_origin_entries(list(allowlist))
        if normalized == list(allowlist):
            continue
        bind.execute(
            sa.text("UPDATE projects SET domain_allowlist = :al WHERE id = :id").bindparams(
                sa.bindparam("al", type_=ARRAY(sa.Text()))
            ),
            {"al": normalized, "id": project_id},
        )


def downgrade() -> None:
    # Original raw strings are not recoverable.
    pass
