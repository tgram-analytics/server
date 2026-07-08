"""Per-suite fixtures for ``tests/mcp``.

The tool tests focus on the boundaries that matter for security and
contract:

1. Auth check — every tool returns ``isError=True`` when no token is in
   the contextvar.
2. Ownership check — every project-scoped tool calls
   ``assert_project_owned_by`` BEFORE any analytics service. The
   cross-user fixture proves this by feeding two users + a project
   owned by user A and asserting that user B's call returns
   ``isError=True``.
3. Service dispatch — happy path forwards to the right OSS service
   with the right kwargs.

We mock the OSS service functions directly rather than spinning up the
full Postgres-backed schema. The OSS ``events`` table uses ``JSONB``
and the ``projects`` table uses ``ARRAY[text]``, neither of which
compiles on SQLite. Mocking lets us exercise the tool layer in
isolation; OSS service correctness is covered by the OSS test suite.

The one OSS table we DO need on SQLite is ``users`` — only because
``services.projects.get_project`` queries the ``projects`` table by
``owner_user_id``. Rather than build the OSS projects table on SQLite
(blocked by ``ARRAY``), we mock ``assert_project_owned_by`` itself in
the cross-user-403 path.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import contextmanager
from typing import Any

import pytest
import pytest_asyncio
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.fastmcp import FastMCP

from app.mcp.auth import MCPAccessToken
from app.mcp.tools import register_all_tools


@pytest.fixture(autouse=True)
def _stub_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """OSS Settings requires these env vars to import."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    monkeypatch.setenv("ADMIN_CHAT_ID", "1")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("SECRET_KEY", "x")


@pytest.fixture
def user_a_id() -> uuid.UUID:
    return uuid.UUID("11111111-1111-1111-1111-111111111111")


@pytest.fixture
def user_b_id() -> uuid.UUID:
    return uuid.UUID("22222222-2222-2222-2222-222222222222")


@pytest.fixture
def project_a_id() -> uuid.UUID:
    """A project owned by ``user_a_id``."""
    return uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


def _make_token(user_id: uuid.UUID, tg_id: int = 42) -> MCPAccessToken:
    return MCPAccessToken(
        token="test-token",
        client_id="mcp-client",
        scopes=["mcp:tools"],
        expires_at=int(2 * 10**9),  # year ~2033
        extra={
            "user_id": str(user_id),
            "tg_id": tg_id,
            "jti": str(uuid.uuid4()),
        },
    )


@contextmanager
def _set_auth_token(token: MCPAccessToken | None):
    """Context manager: install *token* into the auth contextvar.

    Mirrors what ``AuthContextMiddleware`` does in production. Tests
    use this to simulate an authenticated request without booting the
    full Starlette stack.
    """
    if token is None:
        sentinel = auth_context_var.set(None)
    else:
        sentinel = auth_context_var.set(AuthenticatedUser(token))
    try:
        yield
    finally:
        auth_context_var.reset(sentinel)


@pytest.fixture
def set_auth_token():
    """Expose ``_set_auth_token`` as a fixture so tests can ``with``-use it."""
    return _set_auth_token


@pytest.fixture
def fresh_mcp() -> FastMCP:
    """A FastMCP instance with all v1 tools registered, suitable for direct dispatch.

    Tests call ``mcp._tool_manager.get_tool(name).fn(**kwargs)`` to run a
    handler directly — that bypasses the MCP framing layer (which would
    JSON-serialize dict returns) and lets assertions read the raw shape.
    """
    mcp = FastMCP(name="test-mcp")
    register_all_tools(mcp)
    return mcp


@pytest_asyncio.fixture
async def mock_session() -> AsyncIterator[Any]:
    """A no-op session — tools call services that are themselves mocked.

    Yielded so that ``open_session()``'s context-manager shape works.
    """
    yield object()


@pytest.fixture
def patch_open_session(monkeypatch: pytest.MonkeyPatch, mock_session):
    """Patch ``open_session`` in every tools module to yield ``mock_session``."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _fake():
        yield mock_session

    # Patch the imported reference inside each tools submodule.
    import app.mcp.tools.data as data_mod
    import app.mcp.tools.projects as projects_mod
    import app.mcp.tools.setup as setup_mod

    monkeypatch.setattr(projects_mod, "open_session", _fake)
    monkeypatch.setattr(data_mod, "open_session", _fake)
    monkeypatch.setattr(setup_mod, "open_session", _fake)
    return _fake


def _call(mcp: FastMCP, name: str, **kwargs: Any) -> Any:
    """Test helper: invoke a registered tool's underlying function.

    N4 — tools now return Pydantic models on the success path. Tests
    that pre-date the migration assert against dict shapes, so we
    transparently downcast via ``model_dump(mode="json")`` before
    returning. The error path (``list[TextContent]``) is forwarded
    unchanged so ``isError`` / ``text`` assertions still work.
    """
    from pydantic import BaseModel

    async def _wrap():
        result = await mcp._tool_manager.get_tool(name).fn(**kwargs)
        if isinstance(result, BaseModel):
            return result.model_dump(mode="json")
        return result

    return _wrap()


@pytest.fixture
def call_tool():
    """Expose the tool-dispatch helper as a fixture."""
    return _call


@pytest_asyncio.fixture
async def seeded_user(session_factory):
    """Insert a real ``User`` via the Postgres-backed ``session_factory``.

    The StaticTokenVerifier tests need durably-committed rows (the verifier
    opens its own session to look the token up), so this fixture commits a
    user and cleans it up afterwards. Deleting the user cascades to any
    ``mcp_tokens`` rows created during the test (FK ``ondelete=CASCADE``).
    """
    from sqlalchemy import text

    from app.models.user import User

    async with session_factory() as session:
        user = User(telegram_user_id=999_001)
        session.add(user)
        await session.commit()
        await session.refresh(user)
        user_id = user.id

    try:
        yield user
    finally:
        async with session_factory() as session:
            await session.execute(text("DELETE FROM users WHERE id = :id"), {"id": str(user_id)})
            await session.commit()
