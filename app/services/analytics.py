"""Analytics query service.

All functions work directly on the raw ``events`` table.
Callers may cache results or route to the ``aggregations`` table for
historical periods — that routing layer is added in Phase 4's query router.
"""

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.event import Event


def _zero_fill(
    rows: list[dict[str, Any]],
    start: datetime,
    end: datetime,
    granularity: str,
) -> list[dict[str, Any]]:
    """Insert zero-count entries for missing buckets so charts show gaps."""
    if granularity == "hour":
        step = timedelta(hours=1)
    elif granularity == "week":
        step = timedelta(weeks=1)
    elif granularity == "month":
        # approximate; good enough for iteration
        step = timedelta(days=30)
    else:
        step = timedelta(days=1)

    existing = {row["bucket"]: row["count"] for row in rows}

    # Truncate start to the bucket boundary
    if granularity == "hour":
        cursor = start.replace(minute=0, second=0, microsecond=0)
    elif granularity == "week":
        # Monday-start ISO week
        cursor = (start - timedelta(days=start.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    elif granularity == "month":
        cursor = start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        cursor = start.replace(hour=0, minute=0, second=0, microsecond=0)

    filled: list[dict[str, Any]] = []
    while cursor < end:
        filled.append({"bucket": cursor, "count": existing.get(cursor, 0)})
        if granularity == "month":
            # advance to first day of next month
            if cursor.month == 12:
                cursor = cursor.replace(year=cursor.year + 1, month=1)
            else:
                cursor = cursor.replace(month=cursor.month + 1)
        else:
            cursor = cursor + step

    return filled


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

    Missing buckets are zero-filled so the returned series is continuous.
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
    rows = [{"bucket": row.bucket, "count": row.count} for row in result]
    if not rows:
        return []
    return _zero_fill(rows, start, end, granularity)


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
    return [
        {"event_name": r.event_name, "count": r.count, "last_seen": r.last_seen} for r in result
    ]


async def list_recent_events(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Return the most recent *limit* events for a project, newest first.

    Returns ``[{"event_name": str, "timestamp": datetime}, ...]``.
    """
    result = await session.execute(
        select(Event.event_name, Event.received_at)
        .where(Event.project_id == project_id)
        .order_by(Event.received_at.desc())
        .limit(limit)
    )
    return [{"event_name": r.event_name, "timestamp": r.received_at} for r in result]


async def list_property_keys(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    event_name: str,
    start: datetime,
    end: datetime,
    limit: int = 10,
) -> list[str]:
    """Return distinct top-level property keys for an event in [start, end).

    Only returns keys that appear at least once in the given time window.
    Results are ordered by frequency (most common first).
    """
    key_col = func.jsonb_object_keys(Event.properties).label("key")

    # Sub-query: one row per (event_id, key)
    sub = (
        select(key_col)
        .where(
            Event.project_id == project_id,
            Event.event_name == event_name,
            Event.timestamp >= start,
            Event.timestamp < end,
        )
        .subquery()
    )
    result = await session.execute(
        select(sub.c.key, func.count().label("cnt"))
        .group_by(sub.c.key)
        .order_by(func.count().desc())
        .limit(limit)
    )
    return [row.key for row in result]


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
