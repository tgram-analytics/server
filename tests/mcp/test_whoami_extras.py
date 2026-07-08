"""register_mcp_whoami_extra fields appear in whoami output.

Reuses the ``app_client_with_token`` fixture (in ``tests/mcp/conftest.py``),
which boots the app in-process via ``_boot_server``. That helper resets the
extension registry on entry and exit, so the ``plan_extra`` hook registered
in the test body is cleaned up on fixture teardown and does not leak.
"""

from __future__ import annotations

import json

import pytest

from app import extensions as ext


@pytest.mark.asyncio
async def test_whoami_includes_registered_extras(app_client_with_token):
    async def plan_extra(session, user):
        return {"plan": "test-tier"}

    ext.register_mcp_whoami_extra(plan_extra)
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
        result = await session.call_tool("whoami", {})
        payload = json.loads(result.content[0].text)
        assert payload["plan"] == "test-tier"
