"""DNS-rebinding protection must allow the deployment's public Host.

FastMCP auto-enables the MCP SDK's transport security with a *localhost-only*
host allow-list (because it binds to 127.0.0.1 behind the proxy). The real
request Host is the public domain, which would otherwise be rejected with
421 "Invalid Host header". ``_build_transport_security`` configures the
allow-list for the public host while keeping loopback entries for local/dev
and the e2e suite.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.mcp.server import _build_transport_security


def test_public_host_and_origin_allowed() -> None:
    settings = SimpleNamespace(
        mcp_effective_public_url="https://mcp.tgram-analytics.com",
        mcp_allowed_origins=[],
    )
    ts = _build_transport_security(settings)
    assert ts.enable_dns_rebinding_protection is True
    assert "mcp.tgram-analytics.com" in ts.allowed_hosts
    assert "https://mcp.tgram-analytics.com" in ts.allowed_origins


def test_loopback_preserved_for_local_and_e2e() -> None:
    settings = SimpleNamespace(
        mcp_effective_public_url="http://127.0.0.1:8000",
        mcp_allowed_origins=[],
    )
    ts = _build_transport_security(settings)
    # Wildcard-port loopback so the e2e client's 127.0.0.1:<port> Host passes.
    assert "127.0.0.1:*" in ts.allowed_hosts


def test_configured_origins_are_merged() -> None:
    settings = SimpleNamespace(
        mcp_effective_public_url="https://mcp.tgram-analytics.com",
        mcp_allowed_origins=["https://claude.ai"],
    )
    ts = _build_transport_security(settings)
    assert "https://claude.ai" in ts.allowed_origins
