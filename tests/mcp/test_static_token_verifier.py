"""StaticTokenVerifier: static mcp_ tokens against the mcp_tokens table."""

import pytest

from app.mcp.auth import MCPAccessToken, StaticTokenVerifier
from app.services import mcp_tokens as svc


@pytest.mark.asyncio
async def test_valid_token_returns_identity(session_factory, seeded_user):
    async with session_factory() as session:
        raw, _ = await svc.create_token(session, user_id=seeded_user.id, label="t")
        await session.commit()
    verifier = StaticTokenVerifier(session_factory=session_factory)
    token = await verifier.verify_token(raw)
    assert isinstance(token, MCPAccessToken)
    assert token.extra["user_id"] == str(seeded_user.id)
    assert token.extra["tg_id"] == seeded_user.telegram_user_id
    assert token.scopes == ["mcp:tools"]


@pytest.mark.asyncio
async def test_unknown_token_rejected(session_factory):
    verifier = StaticTokenVerifier(session_factory=session_factory)
    assert await verifier.verify_token("mcp_" + "0" * 64) is None


@pytest.mark.asyncio
async def test_revoked_token_rejected(session_factory, seeded_user):
    async with session_factory() as session:
        raw, row = await svc.create_token(session, user_id=seeded_user.id, label="t")
        await svc.revoke_token(session, token_id=row.id, user_id=seeded_user.id)
        await session.commit()
    verifier = StaticTokenVerifier(session_factory=session_factory)
    assert await verifier.verify_token(raw) is None


@pytest.mark.asyncio
async def test_valid_token_updates_last_used(session_factory, seeded_user):
    async with session_factory() as session:
        raw, row = await svc.create_token(session, user_id=seeded_user.id, label="t")
        await session.commit()
        token_id = row.id
    verifier = StaticTokenVerifier(session_factory=session_factory)
    await verifier.verify_token(raw)
    async with session_factory() as session:
        from app.models.mcp_token import MCPToken

        refreshed = await session.get(MCPToken, token_id)
        assert refreshed.last_used_at is not None
