"""Event-data MCP tools.

Four handlers, all project-scoped (every one calls
``assert_project_owned_by`` before any analytics service):

- ``query_events`` — events-over-time + total for a project + event +
  period.
- ``compare_periods`` — current-period count vs. immediately-prior period
  + percent delta.
- ``top_pages`` — top URLs from ``pageview`` events.
- ``recent_events`` — most-recent N events (newest first).
"""

from __future__ import annotations

import logging
import uuid

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.fastmcp import FastMCP
from mcp.types import TextContent, ToolAnnotations

from app.mcp.auth import (
    MCPAccessToken,
    ProjectNotOwnedError,
    assert_project_owned_by,
)
from app.mcp.tools._periods import (
    SUPPORTED_PERIODS,
    InvalidPeriodError,
    period_to_window,
    previous_window,
)
from app.mcp.tools._schemas import (
    ComparePeriodsResult,
    QueryEventsResult,
    RecentEventRow,
    RecentEventsResult,
    TimeBucket,
    TopPagesResult,
    TopPagesRow,
)
from app.mcp.tools._session import open_session

logger = logging.getLogger("app.mcp.tools")

# N3 — Analytics reads. All four tools are pure reads against our own DB;
# no external calls, no state changes. Clients can safely retry.
_READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    idempotentHint=True,
    openWorldHint=False,
)

_VALID_GRANULARITIES: tuple[str, ...] = ("hour", "day", "week", "month")


def _not_authenticated() -> list[TextContent]:
    return [
        TextContent(
            type="text",
            text="not authenticated",
            isError=True,  # type: ignore[call-arg]
        )
    ]


def _not_owned_error(project_id: str) -> list[TextContent]:
    return [
        TextContent(
            type="text",
            text=f"project {project_id} not found or you don't have access",
            isError=True,  # type: ignore[call-arg]
        )
    ]


def _bad_input(msg: str) -> list[TextContent]:
    return [
        TextContent(
            type="text",
            text=msg,
            isError=True,  # type: ignore[call-arg]
        )
    ]


def register_data_tools(mcp: FastMCP) -> None:
    """Register the four event-data tools onto *mcp*."""

    @mcp.tool(title="Query events", annotations=_READ_ONLY)
    async def query_events(
        project_id: str,
        event_name: str,
        period: str = "7d",
        granularity: str = "day",
    ) -> list[TextContent] | QueryEventsResult:
        """Return event counts bucketed by *granularity* over *period*.

        Response: ``{"total": int, "series": [{"bucket": iso, "count": int}, ...]}``.
        """
        token = get_access_token()
        if token is None or not isinstance(token, MCPAccessToken):
            return _not_authenticated()

        try:
            pid = uuid.UUID(project_id)
        except (ValueError, AttributeError):
            return _bad_input(f"invalid project_id {project_id!r}; must be a UUID")

        if granularity not in _VALID_GRANULARITIES:
            return _bad_input(
                f"invalid granularity {granularity!r}; expected one of {list(_VALID_GRANULARITIES)}"
            )

        try:
            start, end = period_to_window(period)
        except InvalidPeriodError as exc:
            return _bad_input(str(exc))

        owner_user_id = uuid.UUID(token.extra["user_id"])

        from app.services.analytics import count_events, events_over_time

        async with open_session() as session:
            try:
                await assert_project_owned_by(session, pid, owner_user_id)
            except ProjectNotOwnedError:
                return _not_owned_error(project_id)

            total = await count_events(
                session,
                project_id=pid,
                event_name=event_name,
                start=start,
                end=end,
            )
            rows = await events_over_time(
                session,
                project_id=pid,
                event_name=event_name,
                start=start,
                end=end,
                granularity=granularity,
            )

        series = [
            TimeBucket(
                bucket=r["bucket"].isoformat() if r["bucket"] else None,
                count=r["count"],
            )
            for r in rows
        ]
        return QueryEventsResult(
            total=total,
            series=series,
            period=period,
            granularity=granularity,
        )

    @mcp.tool(title="Compare periods", annotations=_READ_ONLY)
    async def compare_periods(
        project_id: str,
        event_name: str,
        period: str = "7d",
    ) -> list[TextContent] | ComparePeriodsResult:
        """Compare current-period count to the immediately-prior window.

        Response: ``{"current": int, "previous": int, "delta_pct": float | None}``.
        ``delta_pct`` is ``None`` when ``previous`` is zero.
        """
        token = get_access_token()
        if token is None or not isinstance(token, MCPAccessToken):
            return _not_authenticated()

        try:
            pid = uuid.UUID(project_id)
        except (ValueError, AttributeError):
            return _bad_input(f"invalid project_id {project_id!r}; must be a UUID")

        try:
            current_start, current_end = period_to_window(period)
        except InvalidPeriodError as exc:
            return _bad_input(str(exc))

        previous_start, previous_end = previous_window(current_start, current_end)
        owner_user_id = uuid.UUID(token.extra["user_id"])

        from app.services.analytics import compare_periods as svc_compare_periods

        async with open_session() as session:
            try:
                await assert_project_owned_by(session, pid, owner_user_id)
            except ProjectNotOwnedError:
                return _not_owned_error(project_id)

            result = await svc_compare_periods(
                session,
                project_id=pid,
                event_name=event_name,
                current_start=current_start,
                current_end=current_end,
                previous_start=previous_start,
                previous_end=previous_end,
            )

        return ComparePeriodsResult(
            current=result["current"],
            previous=result["previous"],
            delta_pct=result["delta_pct"],
            period=period,
        )

    @mcp.tool(title="Top pages", annotations=_READ_ONLY)
    async def top_pages(
        project_id: str,
        period: str = "7d",
        limit: int = 10,
    ) -> list[TextContent] | TopPagesResult:
        """Return the top URLs from ``pageview`` events in *period*.

        Response: ``{"pages": [{"value": url, "count": int}, ...]}``.
        """
        token = get_access_token()
        if token is None or not isinstance(token, MCPAccessToken):
            return _not_authenticated()

        try:
            pid = uuid.UUID(project_id)
        except (ValueError, AttributeError):
            return _bad_input(f"invalid project_id {project_id!r}; must be a UUID")

        if not isinstance(limit, int) or limit <= 0 or limit > 100:
            return _bad_input(f"invalid limit {limit!r}; must be 1..100")

        try:
            start, end = period_to_window(period)
        except InvalidPeriodError as exc:
            return _bad_input(str(exc))

        owner_user_id = uuid.UUID(token.extra["user_id"])

        from app.services.analytics import top_properties

        async with open_session() as session:
            try:
                await assert_project_owned_by(session, pid, owner_user_id)
            except ProjectNotOwnedError:
                return _not_owned_error(project_id)

            rows = await top_properties(
                session,
                project_id=pid,
                event_name="pageview",
                property_key="url",
                start=start,
                end=end,
                limit=limit,
            )

        return TopPagesResult(
            pages=[TopPagesRow(value=r["value"], count=r["count"]) for r in rows],
            period=period,
        )

    @mcp.tool(title="Recent events", annotations=_READ_ONLY)
    async def recent_events(
        project_id: str,
        event_name: str | None = None,
        limit: int = 50,
    ) -> list[TextContent] | RecentEventsResult:
        """Return the most-recent events for a project, newest first.

        ``event_name`` is optional; when provided, the over-fetched list
        is filtered post-hoc to keep this call to one OSS service. With
        a populated project that filter is good enough at the v1 surface;
        a dedicated server-side filter can come in v2 if needed.

        Response: ``{"events": [{"event_name": str, "timestamp": iso}, ...]}``.
        """
        token = get_access_token()
        if token is None or not isinstance(token, MCPAccessToken):
            return _not_authenticated()

        try:
            pid = uuid.UUID(project_id)
        except (ValueError, AttributeError):
            return _bad_input(f"invalid project_id {project_id!r}; must be a UUID")

        if not isinstance(limit, int) or limit <= 0 or limit > 500:
            return _bad_input(f"invalid limit {limit!r}; must be 1..500")

        owner_user_id = uuid.UUID(token.extra["user_id"])

        from app.services.analytics import list_recent_events

        # Over-fetch to give the post-hoc event_name filter some slack;
        # for the no-filter path, fetch_limit collapses back to limit.
        fetch_limit = limit * 5 if event_name else limit

        async with open_session() as session:
            try:
                await assert_project_owned_by(session, pid, owner_user_id)
            except ProjectNotOwnedError:
                return _not_owned_error(project_id)

            rows = await list_recent_events(
                session,
                project_id=pid,
                limit=fetch_limit,
            )

        if event_name:
            rows = [r for r in rows if r["event_name"] == event_name][:limit]
        else:
            rows = rows[:limit]

        events = [
            RecentEventRow(
                event_name=r["event_name"],
                timestamp=r["timestamp"].isoformat() if r["timestamp"] else None,
            )
            for r in rows
        ]
        return RecentEventsResult(events=events)


__all__ = ["register_data_tools", "SUPPORTED_PERIODS"]
