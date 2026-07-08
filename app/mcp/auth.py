"""Static bearer-token verifier + ownership helper for the MCP surface.

The OSS default authentication is a long-lived static token created via
the ``/mcp_token`` bot command (hash-at-rest in the ``mcp_tokens``
table). Deployments needing a different scheme (the cloud overlay's
OAuth JWTs) replace the verifier via
``app.extensions.register_mcp_token_verifier``.

Also exposes :func:`assert_project_owned_by` — the single ownership
check every project-scoped MCP tool runs before touching user data.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from mcp.server.auth.provider import AccessToken, TokenVerifier
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger("app.mcp")


class MCPAccessToken(AccessToken):
    """``AccessToken`` extended with a free-form ``extra`` claim bag.

    Tool handlers read the authenticated identity from ``extra``
    (``user_id`` always; ``tg_id`` when known) via
    ``mcp.server.auth.middleware.auth_context.get_access_token()``.
    """

    extra: dict[str, Any] = {}


class StaticTokenVerifier(TokenVerifier):
    """Verify static ``mcp_<hex>`` tokens against the ``mcp_tokens`` table."""

    def __init__(self, *, session_factory: async_sessionmaker[Any] | None = None) -> None:
        # Lazy factory: resolved per-request so build order doesn't matter.
        self._session_factory = session_factory

    def _resolve_factory(self) -> async_sessionmaker[Any] | None:
        if self._session_factory is not None:
            return self._session_factory
        try:
            from app.core.database import get_session_factory

            return get_session_factory()
        except RuntimeError:
            return None

    async def verify_token(self, token: str) -> AccessToken | None:  # noqa: D401
        try:
            factory = self._resolve_factory()
            if factory is None:
                logger.error("cannot verify token: session factory unavailable")
                return None

            from app.models.user import User
            from app.services import mcp_tokens as token_svc

            async with factory() as session:
                row = await token_svc.lookup_active_token(session, token)
                if row is None:
                    return None
                tg_id_result = await session.execute(
                    select(User.telegram_user_id).where(User.id == row.user_id)
                )
                tg_id = tg_id_result.scalar_one_or_none()
                await token_svc.touch_last_used(session, row.id)
                await session.commit()

            return MCPAccessToken(
                token=token,
                client_id=f"mcp-token-{row.id}",
                scopes=["mcp:tools"],
                expires_at=None,
                extra={
                    "user_id": str(row.user_id),
                    "tg_id": int(tg_id) if tg_id is not None else None,
                    "token_id": str(row.id),
                },
            )
        except Exception:  # pragma: no cover - defensive
            logger.exception("verify_token: unexpected error")
            return None


class ProjectNotOwnedError(Exception):
    """Raised by :func:`assert_project_owned_by` when the caller doesn't own *project_id*.

    Non-existent and not-owned collapse into one case to avoid leaking
    project existence to non-owners.
    """


async def assert_project_owned_by(
    session: AsyncSession,
    project_id: uuid.UUID,
    user_id: uuid.UUID,
) -> Any:
    """Return the ``Project`` row when *user_id* owns *project_id*; raise otherwise."""
    from app.services.projects import get_project

    project = await get_project(session, project_id, user_id)
    if project is None:
        raise ProjectNotOwnedError(f"project {project_id} not found or not owned by user {user_id}")
    return project
