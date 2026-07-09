"""Host-root OAuth discovery metadata for the self-host MCP OAuth layer.

FastMCP's 401 advertises ``resource_metadata=<host>/.well-known/
oauth-protected-resource/mcp`` — before this router existed that URL
404'd on self-host, which is precisely why OAuth-only clients (Claude
Desktop) failed to connect. Serves RFC 9728 protected-resource metadata
(canonical + bare paths) and mirrors the RFC 8414 authorization-server
document at host root (required when the issuer is a bare host).
"""

from __future__ import annotations

from fastapi import APIRouter

from app.mcp.oauth.metadata import build_authorization_server_metadata


def build_well_known_router(*, public_url: str) -> APIRouter:
    base = public_url.rstrip("/")
    resource_payload: dict[str, object] = {
        "resource": f"{base}/mcp",
        "authorization_servers": [base],
        "scopes_supported": ["mcp:tools"],
        "bearer_methods_supported": ["header"],
        "resource_documentation": "https://github.com/tgram-analytics/server",
    }
    as_metadata = build_authorization_server_metadata(base)
    router = APIRouter(tags=["mcp-well-known"])

    @router.get("/.well-known/oauth-protected-resource/mcp", response_model=None)
    async def protected_resource_with_path() -> dict[str, object]:
        return resource_payload

    @router.get("/.well-known/oauth-protected-resource", response_model=None)
    async def protected_resource_bare() -> dict[str, object]:
        return resource_payload

    @router.get("/.well-known/oauth-authorization-server", response_model=None)
    async def authorization_server() -> dict[str, object]:
        return as_metadata

    return router
