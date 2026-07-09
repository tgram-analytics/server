"""DB operations for the self-host OAuth layer.

``exchange_code`` returns the RAW derived ``mcp_`` token (or ``None`` on
any failure — expired, used, PKCE/client/redirect mismatch). All failure
modes collapse to ``None`` so the router emits one uniform
``invalid_grant`` and nothing leaks about which check failed.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.mcp.oauth.pkce import verify_s256
from app.models.mcp_oauth import MCPSelfhostOAuthClient, MCPSelfhostOAuthCode
from app.services import mcp_tokens as token_svc

CODE_TTL_SECONDS = 60
_LABEL_MAX = 40


async def register_client(
    session: AsyncSession, *, client_name: str, redirect_uris: list[str]
) -> MCPSelfhostOAuthClient:
    client = MCPSelfhostOAuthClient(
        client_id=secrets.token_urlsafe(24),
        client_name=client_name[:200],
        redirect_uris=redirect_uris,
    )
    session.add(client)
    await session.flush()
    return client


async def get_client(session: AsyncSession, client_id: str) -> MCPSelfhostOAuthClient | None:
    result = await session.execute(
        select(MCPSelfhostOAuthClient).where(MCPSelfhostOAuthClient.client_id == client_id)
    )
    return result.scalar_one_or_none()


async def mint_code(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    client_id: str,
    redirect_uri: str,
    code_challenge: str,
    expires_at: datetime | None = None,
) -> str:
    code = secrets.token_urlsafe(32)
    session.add(
        MCPSelfhostOAuthCode(
            code=code,
            user_id=user_id,
            client_id=client_id,
            redirect_uri=redirect_uri,
            code_challenge=code_challenge,
            expires_at=expires_at or datetime.now(UTC) + timedelta(seconds=CODE_TTL_SECONDS),
        )
    )
    await session.flush()
    return code


async def exchange_code(
    session: AsyncSession,
    *,
    code: str,
    client_id: str,
    redirect_uri: str,
    code_verifier: str,
) -> str | None:
    result = await session.execute(
        select(MCPSelfhostOAuthCode).where(MCPSelfhostOAuthCode.code == code).with_for_update()
    )
    row = result.scalar_one_or_none()
    if row is None or row.used_at is not None:
        return None
    if row.expires_at < datetime.now(UTC):
        return None
    if row.client_id != client_id or row.redirect_uri != redirect_uri:
        return None
    if not verify_s256(code_verifier, row.code_challenge):
        return None

    row.used_at = datetime.now(UTC)
    client = await get_client(session, client_id)
    label = f"oauth:{(client.client_name if client else client_id)}"[:_LABEL_MAX]
    raw, _ = await token_svc.create_token(session, user_id=row.user_id, label=label)
    await session.flush()
    return raw
