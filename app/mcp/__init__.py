"""MCP server core: transport wiring, tool catalog, docs federation.

Mounted at ``/mcp`` by ``app.main`` when ``settings.mcp_enabled``.
Authentication is pluggable via
``app.extensions.register_mcp_token_verifier``; the default is
:class:`app.mcp.auth.StaticTokenVerifier`.
"""
