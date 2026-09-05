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
from pydantic import ValidationError

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
    DeleteAlertResult,
    ListAlertsResult,
)
from app.mcp.tools._session import open_session
from app.models.alert import AlertCondition

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

    @mcp.tool(title="Create alert", annotations=_CREATE)
    async def create_alert(
        project_id: str,
        event_name: str,
        condition: str,
        threshold_n: int | None = None,
    ) -> list[TextContent] | AlertInfo:
        """Create an alert on *project_id*. The user gets a Telegram message when it fires.

        ``condition``: ``every`` (ping on each event), ``every_n`` (ping
        every N-th event; needs ``threshold_n``), ``threshold`` (ping once
        per day when the daily count reaches N; needs ``threshold_n``).
        Prefer ``every_n`` or ``threshold`` for high-volume events such as
        ``pageview``. Any ``event_name`` is accepted; call ``list_event_names``
        first to match an existing event.
        """
        token = get_access_token()
        if token is None or not isinstance(token, MCPAccessToken):
            return _not_authenticated()
        pid = _parse_uuid(project_id)
        if pid is None:
            return _error(f"invalid project_id {project_id!r}; must be a UUID")
        owner_user_id = uuid.UUID(token.extra["user_id"])

        from app.schemas.alert import AlertCreate
        from app.services.alerts import create_alert as svc_create_alert

        try:
            body = AlertCreate.model_validate(
                {
                    "project_id": pid,
                    "event_name": event_name,
                    "condition": condition,
                    "threshold_n": threshold_n,
                }
            )
        except ValidationError as exc:
            msgs = "; ".join(
                f"{'.'.join(str(p) for p in e['loc']) or 'input'}: {e['msg']}" for e in exc.errors()
            )
            return _error(f"invalid alert: {msgs}")

        if body.condition is AlertCondition.every and body.threshold_n is not None:
            return _error("invalid alert: threshold_n must be omitted when condition is 'every'")

        async with open_session() as session:
            try:
                await assert_project_owned_by(session, pid, owner_user_id)
            except ProjectNotOwnedError:
                return _not_owned_error(project_id)
            alert = await svc_create_alert(
                session,
                project_id=pid,
                event_name=body.event_name,
                condition=body.condition,
                threshold_n=body.threshold_n,
            )
            await session.commit()

        logger.info("created alert %s on project_id=%s via mcp", alert.id, pid)
        return _alert_to_info(alert)

    @mcp.tool(title="Pause or resume alert", annotations=_SET_ACTIVE)
    async def set_alert_active(
        project_id: str,
        alert_id: str,
        is_active: bool,
    ) -> list[TextContent] | AlertInfo:
        """Set an alert active (``true``) or paused (``false``). Idempotent."""
        token = get_access_token()
        if token is None or not isinstance(token, MCPAccessToken):
            return _not_authenticated()
        pid = _parse_uuid(project_id)
        if pid is None:
            return _error(f"invalid project_id {project_id!r}; must be a UUID")
        aid = _parse_uuid(alert_id)
        if aid is None:
            return _error(f"invalid alert_id {alert_id!r}; must be a UUID")
        owner_user_id = uuid.UUID(token.extra["user_id"])

        from app.services.alerts import set_alert_active as svc_set_alert_active

        async with open_session() as session:
            try:
                await assert_project_owned_by(session, pid, owner_user_id)
            except ProjectNotOwnedError:
                return _not_owned_error(project_id)
            alert = await svc_set_alert_active(session, aid, pid, is_active=is_active)
            if alert is None:
                return _error(f"alert {alert_id} not found on project {project_id}")
            await session.commit()

        logger.info("set alert %s is_active=%s on project_id=%s via mcp", aid, is_active, pid)
        return _alert_to_info(alert)

    @mcp.tool(title="Delete alert", annotations=_DELETE)
    async def delete_alert(
        project_id: str,
        alert_id: str,
    ) -> list[TextContent] | DeleteAlertResult:
        """Delete an alert. Rows returned by ``alert_history`` are kept."""
        token = get_access_token()
        if token is None or not isinstance(token, MCPAccessToken):
            return _not_authenticated()
        pid = _parse_uuid(project_id)
        if pid is None:
            return _error(f"invalid project_id {project_id!r}; must be a UUID")
        aid = _parse_uuid(alert_id)
        if aid is None:
            return _error(f"invalid alert_id {alert_id!r}; must be a UUID")
        owner_user_id = uuid.UUID(token.extra["user_id"])

        from app.services.alerts import delete_alert as svc_delete_alert
        from app.services.alerts import get_alert
        from app.services.audit import write_audit

        async with open_session() as session:
            try:
                await assert_project_owned_by(session, pid, owner_user_id)
            except ProjectNotOwnedError:
                return _not_owned_error(project_id)
            alert = await get_alert(session, aid, pid)
            if alert is None:
                return _error(f"alert {alert_id} not found on project {project_id}")
            snapshot = {
                "project_id": str(pid),
                "event_name": alert.event_name,
                "condition": str(getattr(alert.condition, "value", alert.condition)),
                "via": "mcp",
            }
            deleted = await svc_delete_alert(session, aid, pid)
            if not deleted:
                # Raced with a bot-side delete between get_alert and here.
                return _error(f"alert {alert_id} not found on project {project_id}")
            await write_audit(
                session,
                user_id=owner_user_id,
                action="alert.delete",
                target_type="alert",
                target_id=str(aid),
                metadata=snapshot,
            )
            await session.commit()

        logger.info("deleted alert %s on project_id=%s via mcp", aid, pid)
        return DeleteAlertResult(deleted=bool(deleted), alert_id=str(aid))
