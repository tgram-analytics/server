"""Self-host OAuth for MCP clients that cannot send custom headers.

Claude Desktop's connector UI is OAuth-only. This package fronts the
existing static-token auth with a standard OAuth 2.1 surface: DCR
(RFC 7591) + PKCE S256 (RFC 7636) + a browser authorize page where the
admin pastes a token minted via the /mcp_token bot command. The /token
endpoint exchanges the auth code for a *derived* ``mcp_tokens`` row, so
the existing ``StaticTokenVerifier`` validates every MCP call — there is
no JWT and no second verification path.

Mounted by ``app.main`` only when the default verifier is in use (the
cloud overlay registers its own verifier + OAuth and must not conflict).
"""
