"""RFC 8414 / RFC 9728 discovery documents."""

import httpx
import pytest
from fastapi import FastAPI

from app.mcp.oauth.metadata import build_authorization_server_metadata
from app.mcp.well_known import build_well_known_router

BASE = "https://tga.example.com"


def test_as_metadata_shape():
    doc = build_authorization_server_metadata(BASE)
    assert doc["issuer"] == BASE
    assert doc["authorization_endpoint"] == f"{BASE}/mcp/oauth/authorize"
    assert doc["token_endpoint"] == f"{BASE}/mcp/oauth/token"
    assert doc["registration_endpoint"] == f"{BASE}/mcp/oauth/register"
    assert doc["code_challenge_methods_supported"] == ["S256"]
    assert doc["grant_types_supported"] == ["authorization_code"]  # no refresh in v1
    assert doc["token_endpoint_auth_methods_supported"] == ["none"]


@pytest.mark.asyncio
async def test_well_known_routes():
    app = FastAPI()
    app.include_router(build_well_known_router(public_url=BASE))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        for path in (
            "/.well-known/oauth-protected-resource/mcp",
            "/.well-known/oauth-protected-resource",
        ):
            r = await client.get(path)
            assert r.status_code == 200
            body = r.json()
            assert body["resource"] == f"{BASE}/mcp"
            assert body["authorization_servers"] == [BASE]
            assert body["scopes_supported"] == ["mcp:tools"]
        r = await client.get("/.well-known/oauth-authorization-server")
        assert r.status_code == 200
        assert r.json()["issuer"] == BASE
