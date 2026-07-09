"""OAuth surface mounts on self-host, not with a plugin verifier, not when disabled."""

import httpx
import pytest

from tests.mcp.conftest import _boot_server


@pytest.mark.asyncio
async def test_selfhost_mounts_oauth(app_client):
    r = await app_client.get("/.well-known/oauth-protected-resource/mcp")
    assert r.status_code == 200
    r = await app_client.get("/.well-known/oauth-authorization-server")
    assert r.status_code == 200
    assert "authorization_endpoint" in r.json()
    r = await app_client.get("/favicon.ico")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/svg+xml")
    # Guards mount ORDER: the oauth router must be reachable through the
    # fully-mounted app, i.e. included BEFORE the catch-all /mcp ASGI mount.
    # authorize GET early-returns 400 before any DB work, so this needs no
    # Postgres; if the /mcp mount shadowed /mcp/oauth/*, FastMCP would 404/406.
    r = await app_client.get("/mcp/oauth/authorize")
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_plugin_verifier_disables_oss_oauth():
    from app import extensions as ext

    async with (
        _boot_server(
            mcp_enabled=True, pre_boot=lambda: ext.register_mcp_token_verifier(object())
        ) as base_url,
        httpx.AsyncClient(base_url=base_url, timeout=10.0) as client,
    ):
        r = await client.get("/.well-known/oauth-authorization-server")
        assert r.status_code == 404


@pytest.mark.asyncio
async def test_oauth_flag_off_unmounts():
    async with (
        _boot_server(mcp_enabled=True, extra_env={"MCP_OAUTH_ENABLED": "false"}) as base_url,
        httpx.AsyncClient(base_url=base_url, timeout=10.0) as client,
    ):
        assert (await client.get("/.well-known/oauth-authorization-server")).status_code == 404


@pytest.mark.asyncio
async def test_forwarded_proto_yields_https_redirect(app_client):
    """Behind a TLS-terminating proxy the /mcp 307 must point at https, not http."""
    r = await app_client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={
            "Accept": "application/json, text/event-stream",
            "X-Forwarded-Proto": "https",
            "X-Forwarded-For": "203.0.113.9",
        },
        follow_redirects=False,
    )
    assert r.status_code == 307
    assert r.headers["location"].startswith("https://")
