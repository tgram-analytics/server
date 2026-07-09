"""OAuth service: DCR, code mint/exchange, derived-token issuance."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.mcp.oauth import service as svc
from app.mcp.oauth.pkce import s256_challenge
from app.models.user import User

REDIRECT = "https://claude.ai/api/mcp/auth_callback"


async def _user(session) -> User:
    u = User(telegram_user_id=910_000 + uuid.uuid4().int % 10_000)
    session.add(u)
    await session.flush()
    return u


@pytest.mark.asyncio
async def test_register_client_roundtrip(db_session):
    client = await svc.register_client(db_session, client_name="Claude", redirect_uris=[REDIRECT])
    assert client.client_id
    found = await svc.get_client(db_session, client.client_id)
    assert found is not None and found.redirect_uris == [REDIRECT]


@pytest.mark.asyncio
async def test_mint_and_exchange_code_issues_derived_token(db_session):
    user = await _user(db_session)
    client = await svc.register_client(db_session, client_name="C", redirect_uris=[REDIRECT])
    challenge = s256_challenge("verifier-123")
    code = await svc.mint_code(
        db_session,
        user_id=user.id,
        client_id=client.client_id,
        redirect_uri=REDIRECT,
        code_challenge=challenge,
    )
    raw = await svc.exchange_code(
        db_session,
        code=code,
        client_id=client.client_id,
        redirect_uri=REDIRECT,
        code_verifier="verifier-123",
    )
    assert raw is not None and raw.startswith("mcp_")
    from app.services.mcp_tokens import lookup_active_token

    row = await lookup_active_token(db_session, raw)
    assert row is not None and row.user_id == user.id
    assert row.label.startswith("oauth:")


@pytest.mark.asyncio
async def test_exchange_rejects_pkce_mismatch(db_session):
    user = await _user(db_session)
    client = await svc.register_client(db_session, client_name="C", redirect_uris=[REDIRECT])
    code = await svc.mint_code(
        db_session,
        user_id=user.id,
        client_id=client.client_id,
        redirect_uri=REDIRECT,
        code_challenge=s256_challenge("right"),
    )
    assert (
        await svc.exchange_code(
            db_session,
            code=code,
            client_id=client.client_id,
            redirect_uri=REDIRECT,
            code_verifier="wrong",
        )
        is None
    )


@pytest.mark.asyncio
async def test_exchange_code_single_use(db_session):
    user = await _user(db_session)
    client = await svc.register_client(db_session, client_name="C", redirect_uris=[REDIRECT])
    code = await svc.mint_code(
        db_session,
        user_id=user.id,
        client_id=client.client_id,
        redirect_uri=REDIRECT,
        code_challenge=s256_challenge("v"),
    )
    kwargs = dict(code=code, client_id=client.client_id, redirect_uri=REDIRECT, code_verifier="v")
    assert await svc.exchange_code(db_session, **kwargs) is not None
    assert await svc.exchange_code(db_session, **kwargs) is None  # second use dead


@pytest.mark.asyncio
async def test_exchange_rejects_expired_and_mismatches(db_session):
    user = await _user(db_session)
    client = await svc.register_client(db_session, client_name="C", redirect_uris=[REDIRECT])
    code = await svc.mint_code(
        db_session,
        user_id=user.id,
        client_id=client.client_id,
        redirect_uri=REDIRECT,
        code_challenge=s256_challenge("v"),
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    assert (
        await svc.exchange_code(
            db_session,
            code=code,
            client_id=client.client_id,
            redirect_uri=REDIRECT,
            code_verifier="v",
        )
        is None
    )
    # redirect mismatch
    code2 = await svc.mint_code(
        db_session,
        user_id=user.id,
        client_id=client.client_id,
        redirect_uri=REDIRECT,
        code_challenge=s256_challenge("v"),
    )
    assert (
        await svc.exchange_code(
            db_session,
            code=code2,
            client_id=client.client_id,
            redirect_uri="https://evil.example/cb",
            code_verifier="v",
        )
        is None
    )
