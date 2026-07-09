"""Authorization-server metadata document (RFC 8414).

Single pure builder shared by the host-root well-known router and the
OAuth router so the two copies can never drift. URLs derive from the
configured public URL, never the request scheme (behind Cloudflare the
request scheme can read http; see the proxy-headers fix in app.main).
"""

from __future__ import annotations


def build_authorization_server_metadata(public_url: str) -> dict[str, object]:
    base = public_url.rstrip("/")
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/mcp/oauth/authorize",
        "token_endpoint": f"{base}/mcp/oauth/token",
        "registration_endpoint": f"{base}/mcp/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
    }
