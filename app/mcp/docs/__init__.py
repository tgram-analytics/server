"""Federated docs subpackage for MCP integration tools.

Contains:

- :mod:`sources` — platform → raw GitHub README URL map.
- :mod:`fetch` — TTL-cached fetcher with stale-on-error fallback.
- :mod:`render` — Jinja-based SDK init snippet renderer.
- :mod:`local` — placeholder for cloud-specific (filesystem) docs.

Used by the ``get_integration_guide`` and ``get_sdk_snippet`` MCP tools
in :mod:`app.mcp.tools.setup`.
"""
