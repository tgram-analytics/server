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

import asyncio
import os
import socket
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import ExitStack, asynccontextmanager, contextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import pytest_asyncio
import uvicorn
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
    import app.mcp.tools.alerts as alerts_mod
    import app.mcp.tools.data as data_mod
    import app.mcp.tools.projects as projects_mod
    import app.mcp.tools.setup as setup_mod

    monkeypatch.setattr(projects_mod, "open_session", _fake)
    monkeypatch.setattr(data_mod, "open_session", _fake)
    monkeypatch.setattr(setup_mod, "open_session", _fake)
    monkeypatch.setattr(alerts_mod, "open_session", _fake)
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


# ── Booted-app fixtures for the /mcp mount + end-to-end MCP client tests ──────
#
# The FastMCP streamable-HTTP mount can't be driven through httpx's in-process
# ``ASGITransport`` — its session manager runs a task group whose cancel scope
# must be entered and exited in the same task, which the ASGI transport
# violates (and bare ``/mcp`` 307-redirects to ``/mcp/``). So, like the cloud
# e2e reference (``cloud/tests/test_e2e_mcp_client.py``), we boot a real
# uvicorn server on an ephemeral port and talk to it over real HTTP. These
# fixtures are shared by ``test_mount.py`` and ``test_whoami_extras.py``.


def _find_free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@asynccontextmanager
async def _boot_server(
    *,
    mcp_enabled: bool,
    real_db: bool = False,
    database_url: str | None = None,
    extra_env: dict[str, str] | None = None,
    pre_boot: Callable[[], None] | None = None,
) -> AsyncIterator[str]:
    """Boot the real ``app.main`` app under uvicorn with heavy deps stubbed.

    The MCP mount lives inside ``app.main.lifespan`` (after the http-router
    loop), so uvicorn running the real lifespan exercises it. Redis/
    scheduler/bot startup are always patched to no-ops. ``init_db`` is
    patched too by default — none are needed to prove the mount wiring (the
    missing-bearer path 401s before the verifier ever opens a session).
    Pass ``real_db=True`` for the token smoke test: then ``init_db`` runs
    for real so the default ``StaticTokenVerifier`` and the tool handlers
    resolve a live ``get_session_factory()`` against the Postgres test DB.
    In that case pass ``database_url`` too — it MUST be the same URL the
    test harness engine uses (where the token was committed), not a
    hard-coded local default; CI's Postgres credentials differ from
    ``tga:password@localhost``, so a hard-coded URL 401s the verifier.
    Patches stay active for the server's whole lifetime because
    uvicorn.Server runs in-process on this event loop.

    The extension registry is reset on entry and exit so hooks registered by
    a test (e.g. ``register_mcp_whoami_extra``) never leak across tests.
    """
    from app import extensions as ext

    env = {
        "TELEGRAM_BOT_TOKEN": "1234567890:test-token-for-testing-only",
        "ADMIN_CHAT_ID": "123456789",
        "DATABASE_URL": database_url
        or "postgresql+asyncpg://tga:password@localhost/tganalytics_test",
        "SECRET_KEY": "test-secret-key-not-for-production",
        "WEBHOOK_BASE_URL": "https://example.com",
        "MCP_ENABLED": "true" if mcp_enabled else "false",
    }
    if extra_env:
        env.update(extra_env)
    prev = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    ext._reset_for_tests()
    if pre_boot is not None:
        pre_boot()

    import app.main as main_mod

    stack = ExitStack()
    stack.enter_context(patch.object(main_mod, "init_redis", MagicMock()))
    stack.enter_context(patch.object(main_mod, "start_scheduler", MagicMock()))
    stack.enter_context(patch.object(main_mod, "load_plugins", MagicMock()))
    stack.enter_context(patch.object(main_mod, "init_bot", AsyncMock()))
    stack.enter_context(patch.object(main_mod, "shutdown_bot", AsyncMock()))
    stack.enter_context(patch.object(main_mod, "shutdown_scheduler", AsyncMock()))
    stack.enter_context(patch.object(main_mod, "close_redis", AsyncMock()))
    if not real_db:
        stack.enter_context(patch.object(main_mod, "init_db", MagicMock()))
        stack.enter_context(patch.object(main_mod, "close_db", AsyncMock()))

    test_app = main_mod.create_app()
    port = _find_free_port()
    config = uvicorn.Config(
        test_app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        loop="asyncio",
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    base_url = f"http://127.0.0.1:{port}"
    try:
        async with httpx.AsyncClient(timeout=5.0) as probe:
            deadline = asyncio.get_event_loop().time() + 10.0
            while asyncio.get_event_loop().time() < deadline:
                try:
                    r = await probe.get(f"{base_url}/health")
                    if r.status_code == 200:
                        break
                except (httpx.ConnectError, httpx.ReadError):
                    pass
                await asyncio.sleep(0.05)
            else:
                raise RuntimeError("uvicorn never came up")
        yield base_url
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=5.0)
        except TimeoutError:
            task.cancel()
        stack.close()
        ext._reset_for_tests()
        for k, v in prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@pytest_asyncio.fixture
async def app_client() -> AsyncIterator[httpx.AsyncClient]:
    async with (
        _boot_server(mcp_enabled=True) as base_url,
        httpx.AsyncClient(base_url=base_url, timeout=10.0, follow_redirects=True) as client,
    ):
        yield client


@pytest_asyncio.fixture
async def app_client_mcp_disabled() -> AsyncIterator[httpx.AsyncClient]:
    async with (
        _boot_server(mcp_enabled=False) as base_url,
        httpx.AsyncClient(base_url=base_url, timeout=10.0) as client,
    ):
        yield client


@pytest_asyncio.fixture
async def app_client_with_token(
    async_engine,
    session_factory,
) -> AsyncIterator[tuple[httpx.AsyncClient, str]]:
    """Boot the app (mcp enabled, real Postgres DB) + a pre-created token.

    Creates a ``User`` and a static ``mcp_`` token via the service, commits
    them to the Postgres test DB, and yields ``(client, raw_token)``. The
    booted app's ``init_db`` is pointed at the SAME engine URL the token was
    written through (``async_engine``), so it works whether the DB is the
    local container or CI's Postgres service — never a hard-coded default.
    The user is deleted on teardown, cascading to its ``mcp_tokens`` rows.
    """
    from sqlalchemy import text

    from app.models.user import User
    from app.services import mcp_tokens as svc

    db_url = async_engine.url.render_as_string(hide_password=False)

    async with session_factory() as session:
        user = User(telegram_user_id=999_777)
        session.add(user)
        await session.flush()
        raw, _ = await svc.create_token(session, user_id=user.id, label="smoke")
        await session.commit()
        user_id = user.id

    try:
        async with (
            _boot_server(mcp_enabled=True, real_db=True, database_url=db_url) as base_url,
            httpx.AsyncClient(base_url=base_url, timeout=15.0) as client,
        ):
            yield client, raw
    finally:
        async with session_factory() as session:
            await session.execute(text("DELETE FROM users WHERE id = :id"), {"id": str(user_id)})
            await session.commit()
