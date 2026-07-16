"""Tests for app.services.mcp_tokens."""

import pytest

from app.models.user import User
from app.services import mcp_tokens as svc


@pytest.fixture()
def session(db_session):
    """Alias the rolled-back per-test session under the name the tests expect."""
    return db_session


async def _make_user(session, telegram_user_id: int) -> User:
    user = User(telegram_user_id=telegram_user_id)
    session.add(user)
    await session.flush()
    return user


@pytest.mark.asyncio
async def test_create_token_returns_raw_once_and_stores_hash(session):
    user = await _make_user(session, 900_001)
    raw, row = await svc.create_token(session, user_id=user.id, label="claude")
    assert raw.startswith("mcp_") and len(raw) == 4 + 64
    assert row.token_hash != raw
    assert row.label == "claude"
    assert row.revoked_at is None


@pytest.mark.asyncio
async def test_lookup_active_token_roundtrip(session):
    user = await _make_user(session, 900_002)
    raw, _ = await svc.create_token(session, user_id=user.id, label="x")
    found = await svc.lookup_active_token(session, raw)
    assert found is not None
    assert found.user_id == user.id


@pytest.mark.asyncio
async def test_lookup_unknown_token_returns_none(session):
    assert await svc.lookup_active_token(session, "mcp_" + "0" * 64) is None


@pytest.mark.asyncio
async def test_revoked_token_not_returned(session):
    user = await _make_user(session, 900_003)
    raw, row = await svc.create_token(session, user_id=user.id, label="x")
    await svc.revoke_token(session, token_id=row.id, user_id=user.id)
    assert await svc.lookup_active_token(session, raw) is None


@pytest.mark.asyncio
async def test_list_tokens_scoped_to_user(session):
    u1 = await _make_user(session, 900_004)
    u2 = await _make_user(session, 900_005)
    await svc.create_token(session, user_id=u1.id, label="a")
    await svc.create_token(session, user_id=u2.id, label="b")
    rows = await svc.list_tokens(session, user_id=u1.id)
    assert [r.label for r in rows] == ["a"]
