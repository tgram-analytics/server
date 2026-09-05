"""MCP tool registrations for the cloud overlay.

Each ``register_*_tools(mcp)`` decorates a group of related ``@mcp.tool()``
handlers onto the supplied :class:`FastMCP` instance. The aggregator
:func:`register_all_tools` is what
``app.mcp.server.build_fastmcp_server`` calls after the
inline ``whoami`` decoration.

Phase 5b registered eight real tools plus two Phase-6 stubs. Phase 6
filled the stubs in and the Option-D follow-up added ``rotate_api_key``,
bringing the v1 surface (excluding ``whoami``, which is wired inline by
``build_fastmcp_server``) to eleven:

- Discovery / metadata:
  ``list_projects``, ``get_project``, ``list_event_names``.
- Analytics:
  ``query_events``, ``compare_periods``, ``top_pages``, ``recent_events``.
- Setup / docs:
  ``verify_integration``, ``get_integration_guide``, ``get_sdk_snippet``.
- Project mutation:
  ``rotate_api_key``.
- Alerts:
  ``list_alerts``, ``alert_history``, ``create_alert``,
  ``set_alert_active``, ``delete_alert``.

All handlers follow the Phase 4 ``whoami`` pattern: read the access token
via ``get_access_token()``, run a service call, and return either a JSON-
serializable ``dict`` (success) or ``[TextContent(isError=True)]`` (error
boundary). Handlers never ``raise``; the internal helper
:func:`app.mcp.auth.assert_project_owned_by` may raise
``ProjectNotOwnedError`` and the handler translates that to an error
content part.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from app.mcp.tools.alerts import register_alert_tools
from app.mcp.tools.data import register_data_tools
from app.mcp.tools.projects import register_project_tools
from app.mcp.tools.setup import register_setup_tools


def register_all_tools(mcp: FastMCP) -> None:
    """Register every v1 tool onto *mcp*.

    Order is irrelevant for FastMCP — this just keeps the registration
    side-effect explicit at the call site.
    """
    register_project_tools(mcp)
    register_data_tools(mcp)
    register_setup_tools(mcp)
    register_alert_tools(mcp)


__all__ = ["register_all_tools"]
