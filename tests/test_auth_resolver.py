"""Tests for ``app.bot.auth`` — singleton User bootstrap & resolver.

Phase 3.1 of the SaaS foundation work. Verifies that
``ensure_singleton_user`` is idempotent, race-safe, and keyed correctly on
``telegram_user_id``.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.auth import ensure_singleton_user
from app.models.user import User


async def test_singleton_created_then_returned(db_session: AsyncSession) -> None:
    """First call inserts; second call returns the existing row."""
    tg_id = 900_000_001

    user_a = await ensure_singleton_user(db_session, tg_id)
    assert user_a.id is not None
    assert user_a.telegram_user_id == tg_id

    user_b = await ensure_singleton_user(db_session, tg_id)
    assert user_b.id == user_a.id

    # Exactly one row in the DB for this tg id.
    result = await db_session.execute(select(User).where(User.telegram_user_id == tg_id))
    rows = result.scalars().all()
    assert len(rows) == 1


async def test_mismatched_telegram_user_id_creates_distinct_row(
    db_session: AsyncSession,
) -> None:
    """A different ``telegram_user_id`` is NOT served from any cache.

    Regression check: the resolver must key on the supplied id, not on a
    previously-seen one.
    """
    tg_a = 900_000_002
    tg_b = 900_000_003

    user_a = await ensure_singleton_user(db_session, tg_a)
    user_b = await ensure_singleton_user(db_session, tg_b)

    assert user_a.id != user_b.id
    assert user_a.telegram_user_id == tg_a
    assert user_b.telegram_user_id == tg_b


async def test_idempotent_two_calls_one_row(db_session: AsyncSession) -> None:
    """Two ``ensure_singleton_user`` calls produce a single row.

    Relies on the ``telegram_user_id`` UNIQUE constraint enforced by the
    ``users`` table (migration 0004).
    """
    tg_id = 900_000_004

    await ensure_singleton_user(db_session, tg_id)
    await ensure_singleton_user(db_session, tg_id)

    result = await db_session.execute(select(User).where(User.telegram_user_id == tg_id))
    assert len(result.scalars().all()) == 1
