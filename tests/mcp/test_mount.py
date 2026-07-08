"""The OSS app mounts /mcp with the static verifier by default.

The FastMCP streamable-HTTP mount can't be driven through httpx's
in-process ``ASGITransport`` — its session manager runs a task group whose
cancel scope must be entered and exited in the same task, which the ASGI
transport violates (and bare ``/mcp`` 307-redirects to ``/mcp/``). So, like
the cloud e2e reference (``cloud/tests/test_e2e_mcp_client.py``), we boot a
real uvicorn server on an ephemeral port and talk to it over real HTTP.
"""

from __future__ import annotations

import asyncio
import os
import socket
from collections.abc import AsyncIterator
from contextlib import ExitStack, asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import pytest_asyncio
import uvicorn


def _find_free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@asynccontextmanager
async def _boot_server(*, mcp_enabled: bool, real_db: bool = False) -> AsyncIterator[str]:
    """Boot the real ``app.main`` app under uvicorn with heavy deps stubbed.

    The MCP mount lives inside ``app.main.lifespan`` (after the http-router
    loop), so uvicorn running the real lifespan exercises it. Redis/
    scheduler/bot startup are always patched to no-ops. ``init_db`` is
    patched too by default — none are needed to prove the mount wiring (the
    missing-bearer path 401s before the verifier ever opens a session).
    Pass ``real_db=True`` for the token smoke test: then ``init_db`` runs
    for real so the default ``StaticTokenVerifier`` and the tool handlers
    resolve a live ``get_session_factory()`` against the Postgres test DB.
    Patches stay active for the server's whole lifetime because
    uvicorn.Server runs in-process on this event loop.
    """
    from app import extensions as ext

    env = {
        "TELEGRAM_BOT_TOKEN": "1234567890:test-token-for-testing-only",
        "ADMIN_CHAT_ID": "123456789",
        "DATABASE_URL": "postgresql+asyncpg://tga:password@localhost/tganalytics_test",
        "SECRET_KEY": "test-secret-key-not-for-production",
        "WEBHOOK_BASE_URL": "https://example.com",
        "MCP_ENABLED": "true" if mcp_enabled else "false",
    }
    prev = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    ext._reset_for_tests()

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
        test_app, host="127.0.0.1", port=port, log_level="warning", loop="asyncio"
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


@pytest.mark.asyncio
async def test_mcp_health_route_up(app_client):
    resp = await app_client.get("/mcp/_health")
    assert resp.status_code == 200
    assert resp.json()["module"] == "mcp"


@pytest.mark.asyncio
async def test_mcp_requires_bearer_token(app_client):
    resp = await app_client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Accept": "application/json, text/event-stream"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_mcp_disabled_unmounts(app_client_mcp_disabled):
    resp = await app_client_mcp_disabled.get("/mcp/_health")
    assert resp.status_code == 404


@pytest_asyncio.fixture
async def app_client_with_token(
    session_factory,
) -> AsyncIterator[tuple[httpx.AsyncClient, str]]:
    """Boot the app (mcp enabled, real Postgres DB) + a pre-created token.

    Creates a ``User`` and a static ``mcp_`` token via the service, commits
    them to the Postgres test DB (the same DB ``init_db`` wires the default
    verifier to), and yields ``(client, raw_token)``. The user is deleted on
    teardown, cascading to its ``mcp_tokens`` rows.
    """
    from sqlalchemy import text

    from app.models.user import User
    from app.services import mcp_tokens as svc

    async with session_factory() as session:
        user = User(telegram_user_id=999_777)
        session.add(user)
        await session.flush()
        raw, _ = await svc.create_token(session, user_id=user.id, label="smoke")
        await session.commit()
        user_id = user.id

    try:
        async with (
            _boot_server(mcp_enabled=True, real_db=True) as base_url,
            httpx.AsyncClient(base_url=base_url, timeout=15.0) as client,
        ):
            yield client, raw
    finally:
        async with session_factory() as session:
            await session.execute(text("DELETE FROM users WHERE id = :id"), {"id": str(user_id)})
            await session.commit()


@pytest.mark.asyncio
async def test_mcp_client_lists_tools_with_static_token(app_client_with_token):
    """Official MCP SDK client against /mcp using a pre-created static token."""
    client, raw_token = app_client_with_token
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    mcp_url = str(client.base_url).rstrip("/") + "/mcp/"
    async with (
        streamablehttp_client(
            url=mcp_url,
            headers={"Authorization": f"Bearer {raw_token}"},
        ) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        tools = await session.list_tools()
        names = {t.name for t in tools.tools}
        assert "whoami" in names and "list_projects" in names
        result = await session.call_tool("whoami", {})
        assert not result.isError
