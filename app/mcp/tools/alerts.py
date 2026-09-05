"""Alert MCP tools.

Handlers:

- ``list_alerts`` — alerts configured on a project.
- ``alert_history`` — notification attempts recorded in ``alert_deliveries``.
- ``create_alert`` — create an alert directly (no bot approval step).
- ``set_alert_active`` — pause / resume an alert.
- ``delete_alert`` — remove an alert (audited).

Same shape as ``app.mcp.tools.projects``: read the token, parse ids,
``assert_project_owned_by`` before any service call, call
``app.services.alerts``, return a Pydantic result or
``[TextContent(isError=True)]``. Handlers never raise.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.fastmcp import FastMCP
from mcp.types import TextContent, ToolAnnotations

from app.mcp.auth import (
    MCPAccessToken,
    ProjectNotOwnedError,
    assert_project_owned_by,
)
from app.mcp.tools._periods import InvalidPeriodError, period_to_window
from app.mcp.tools._schemas import (
    AlertDeliveryRow,
    AlertHistoryResult,
    AlertInfo,
    ListAlertsResult,
)
from app.mcp.tools._session import open_session

logger = logging.getLogger("app.mcp.tools")

_READ_ONLY = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)
_CREATE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
)
_SET_ACTIVE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
_DELETE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False
)

_MAX_HISTORY_LIMIT = 500


def _error(text: str) -> list[TextContent]:
    return [
        TextContent(
            type="text",
            text=text,
            isError=True,  # type: ignore[call-arg]
        )
    ]


def _not_authenticated() -> list[TextContent]:
    return _error("not authenticated")


def _not_owned_error(project_id: str) -> list[TextContent]:
    return _error(f"project {project_id} not found or you don't have access")


def _parse_uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return None


def _iso(dt: Any) -> str | None:
    return dt.isoformat() if dt is not None else None


def _alert_to_info(alert: Any) -> AlertInfo:
    return AlertInfo(
        id=str(alert.id),
        project_id=str(alert.project_id),
        event_name=alert.event_name,
        condition=str(getattr(alert.condition, "value", alert.condition)),
        threshold_n=alert.threshold_n,
        counter=alert.counter,
        is_active=alert.is_active,
        muted_until=_iso(alert.muted_until),
        created_at=_iso(alert.created_at),
    )


def _delivery_to_row(row: Any) -> AlertDeliveryRow:
    return AlertDeliveryRow(
        id=str(row.id),
        alert_id=str(row.alert_id) if row.alert_id is not None else None,
        event_name=row.event_name,
        condition=str(getattr(row.condition, "value", row.condition)),
        threshold_n=row.threshold_n,
        fired_at=row.fired_at.isoformat(),
        delivered=row.delivered,
        error=row.error,
    )


def register_alert_tools(mcp: FastMCP) -> None:
    """Register the alert tools onto *mcp*."""

    @mcp.tool(title="List alerts", annotations=_READ_ONLY)
    async def list_alerts(project_id: str) -> list[TextContent] | ListAlertsResult:
        """Return every alert configured on *project_id*.

        Each row carries the condition (``every`` / ``every_n`` /
        ``threshold``), ``threshold_n``, whether it is active, and
        ``muted_until`` if it is silenced.
        """
        token = get_access_token()
        if token is None or not isinstance(token, MCPAccessToken):
            return _not_authenticated()
        pid = _parse_uuid(project_id)
        if pid is None:
            return _error(f"invalid project_id {project_id!r}; must be a UUID")
        owner_user_id = uuid.UUID(token.extra["user_id"])

        from app.services.alerts import list_alerts as svc_list_alerts

        async with open_session() as session:
            try:
                await assert_project_owned_by(session, pid, owner_user_id)
            except ProjectNotOwnedError:
                return _not_owned_error(project_id)
            alerts = await svc_list_alerts(session, pid)

        return ListAlertsResult(alerts=[_alert_to_info(a) for a in alerts])

    @mcp.tool(title="Alert history", annotations=_READ_ONLY)
    async def alert_history(
        project_id: str,
        period: str = "7d",
        limit: int = 50,
        event_name: str | None = None,
    ) -> list[TextContent] | AlertHistoryResult:
        """Return the alert notifications sent for *project_id*, newest first.

        ``period`` is one of ``7d`` / ``30d`` / ``90d`` / ``1y``. ``limit``
        is 1..500. ``event_name`` filters to one event. ``delivered`` is
        false when the Telegram send failed; ``error`` then holds the
        exception class name.
        """
        token = get_access_token()
        if token is None or not isinstance(token, MCPAccessToken):
            return _not_authenticated()
        pid = _parse_uuid(project_id)
        if pid is None:
            return _error(f"invalid project_id {project_id!r}; must be a UUID")
        if not 1 <= limit <= _MAX_HISTORY_LIMIT:
            return _error(f"limit must be between 1 and {_MAX_HISTORY_LIMIT}")
        try:
            since, _end = period_to_window(period)
        except InvalidPeriodError as exc:
            return _error(str(exc))
        owner_user_id = uuid.UUID(token.extra["user_id"])

        from app.services.alerts import list_deliveries

        async with open_session() as session:
            try:
                await assert_project_owned_by(session, pid, owner_user_id)
            except ProjectNotOwnedError:
                return _not_owned_error(project_id)
            rows = await list_deliveries(
                session, pid, since=since, limit=limit, event_name=event_name
            )

        return AlertHistoryResult(
            project_id=str(pid),
            period=period,
            rows=[_delivery_to_row(r) for r in rows],
        )
