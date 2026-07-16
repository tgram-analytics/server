"""Service layer for static MCP bearer tokens.

Token format mirrors project API keys: ``mcp_`` + 64 hex chars from
``secrets.token_hex(32)``. Only the SHA-256 hex digest is persisted.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.mcp_token import MCPToken


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


async def create_token(
    session: AsyncSession, *, user_id: uuid.UUID, label: str
) -> tuple[str, MCPToken]:
    """Create a token for *user_id*; return ``(raw_token, row)``.

    The raw token is never stored — the caller shows it once and drops it.
    """
    raw = "mcp_" + secrets.token_hex(32)
    row = MCPToken(user_id=user_id, token_hash=_hash(raw), label=label)
    session.add(row)
    await session.flush()
    return raw, row


async def lookup_active_token(session: AsyncSession, raw: str) -> MCPToken | None:
    """Return the non-revoked token row matching *raw*, or ``None``."""
    result = await session.execute(
        select(MCPToken).where(
            MCPToken.token_hash == _hash(raw),
            MCPToken.revoked_at.is_(None),
        )
    )
    return result.scalar_one_or_none()


async def list_tokens(session: AsyncSession, *, user_id: uuid.UUID) -> list[MCPToken]:
    """Return all of *user_id*'s tokens (active and revoked), oldest first."""
    result = await session.execute(
        select(MCPToken).where(MCPToken.user_id == user_id).order_by(MCPToken.created_at)
    )
    return list(result.scalars())


async def revoke_token(session: AsyncSession, *, token_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    """Revoke *token_id* if owned by *user_id*. Return ``True`` if revoked."""
    result = await session.execute(
        select(MCPToken).where(
            MCPToken.id == token_id,
            MCPToken.user_id == user_id,
            MCPToken.revoked_at.is_(None),
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        return False
    row.revoked_at = datetime.now(UTC)
    await session.flush()
    return True


async def touch_last_used(session: AsyncSession, token_id: uuid.UUID) -> None:
    """Best-effort update of ``last_used_at``; caller commits."""
    result = await session.execute(select(MCPToken).where(MCPToken.id == token_id))
    row = result.scalar_one_or_none()
    if row is not None:
        row.last_used_at = datetime.now(UTC)
        await session.flush()
