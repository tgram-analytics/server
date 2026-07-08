"""The OSS app mounts /mcp with the static verifier by default.

The booted-app fixtures (``app_client``, ``app_client_mcp_disabled``,
``app_client_with_token``) and the ``_boot_server`` helper live in
``tests/mcp/conftest.py`` so they are shared with ``test_whoami_extras.py``.
Like the cloud e2e reference (``cloud/tests/test_e2e_mcp_client.py``), they
boot a real uvicorn server on an ephemeral port and talk to it over real
HTTP, because the FastMCP streamable-HTTP mount can't be driven through
httpx's in-process ``ASGITransport``.
"""

from __future__ import annotations

import pytest


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
