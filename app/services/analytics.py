"""Analytics query service.

All functions work directly on the raw ``events`` table.
Callers may cache results or route to the ``aggregations`` table for
historical periods — that routing layer is added in Phase 4's query router.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.event import Event


async def count_events(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    event_name: str,
    start: datetime,
    end: datetime,
) -> int:
    """Return the number of events in [start, end)."""
    result = await session.execute(
        select(func.count())
        .select_from(Event)
        .where(
            Event.project_id == project_id,
            Event.event_name == event_name,
            Event.timestamp >= start,
            Event.timestamp < end,
        )
    )
    return result.scalar_one()


async def events_over_time(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    event_name: str,
    start: datetime,
    end: datetime,
    granularity: str = "day",
) -> list[dict[str, Any]]:
    """Return event counts bucketed by *granularity* (hour/day/week/month).

    Buckets with zero events are not included; callers should zero-fill
    if a continuous series is required.
    Returns ``[{"bucket": datetime, "count": int}, ...]`` ordered by bucket.
    """
    trunc_map = {"hour": "hour", "day": "day", "week": "week", "month": "month"}
    trunc = trunc_map.get(granularity, "day")
    bucket_col = func.date_trunc(trunc, Event.timestamp).label("bucket")

    result = await session.execute(
        select(bucket_col, func.count().label("count"))
        .where(
            Event.project_id == project_id,
            Event.event_name == event_name,
            Event.timestamp >= start,
            Event.timestamp < end,
        )
        .group_by(bucket_col)
        .order_by(bucket_col)
    )
    return [{"bucket": row.bucket, "count": row.count} for row in result]


async def top_properties(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    event_name: str,
    property_key: str,
    start: datetime,
    end: datetime,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Return the top *limit* values for *property_key*, sorted by count desc.

    Only events that have the property key are counted.
    Returns ``[{"value": str, "count": int}, ...]``.
    """
    value_col = Event.properties[property_key].astext.label("value")

    result = await session.execute(
        select(value_col, func.count().label("count"))
        .where(
            Event.project_id == project_id,
            Event.event_name == event_name,
            Event.timestamp >= start,
            Event.timestamp < end,
            Event.properties[property_key].astext.isnot(None),
        )
        .group_by(value_col)
        .order_by(func.count().desc())
        .limit(limit)
    )
    return [{"value": row.value, "count": row.count} for row in result]


async def list_event_names(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
) -> list[dict[str, Any]]:
    """Return distinct event names with count and last-seen time, ordered by count desc."""
    result = await session.execute(
        select(
            Event.event_name,
            func.count().label("count"),
            func.max(Event.timestamp).label("last_seen"),
        )
        .where(Event.project_id == project_id)
        .group_by(Event.event_name)
        .order_by(func.count().desc())
    )
    return [{"event_name": r.event_name, "count": r.count, "last_seen": r.last_seen} for r in result]


async def compare_periods(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    event_name: str,
    current_start: datetime,
    current_end: datetime,
    previous_start: datetime,
    previous_end: datetime,
) -> dict[str, Any]:
    """Compare event counts across two time windows.

    Returns ``{"current": int, "previous": int, "delta_pct": float | None}``.
    ``delta_pct`` is None when *previous* is zero (avoids division by zero).
    """
    current = await count_events(
        session,
        project_id=project_id,
        event_name=event_name,
        start=current_start,
        end=current_end,
    )
    previous = await count_events(
        session,
        project_id=project_id,
        event_name=event_name,
        start=previous_start,
        end=previous_end,
    )
    delta_pct: float | None = None
    if previous > 0:
        delta_pct = round((current - previous) / previous * 100, 1)
    return {"current": current, "previous": previous, "delta_pct": delta_pct}
