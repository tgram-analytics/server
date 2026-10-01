"""Aggregation and retention cron services.

Designed to be called from APScheduler jobs (Phase 9) or triggered manually.
All functions accept an ``AsyncSession`` and flush but do NOT commit —
the scheduler wrapper is responsible for the transaction boundary.
"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.aggregation import Aggregation, AggregationPeriod
from app.models.event import REAL_EVENTS, Event
from app.models.settings import ProjectSettings
from app.models.tap import Tap


def _period_start(dt: datetime, period: AggregationPeriod) -> datetime:
    """Return the UTC start of the *period* bucket containing *dt*."""
    match period:
        case AggregationPeriod.hour:
            return dt.replace(minute=0, second=0, microsecond=0)
        case AggregationPeriod.day:
            return dt.replace(hour=0, minute=0, second=0, microsecond=0)
        case AggregationPeriod.week:
            # ISO week starts on Monday.
            monday = dt - timedelta(days=dt.weekday())
            return monday.replace(hour=0, minute=0, second=0, microsecond=0)
        case AggregationPeriod.month:
            return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def run_aggregation_cron(session: AsyncSession) -> int:
    """Upsert aggregation counts for all projects/events for each period type.

    Uses ``ON CONFLICT DO UPDATE`` so re-running is fully idempotent.
    Returns the number of aggregation rows upserted.
    """
    now = datetime.now(UTC)
    upserted = 0

    for period in AggregationPeriod:
        period_start = _period_start(now, period)

        # Sum events per (project_id, event_name) within the current bucket.
        rows = await session.execute(
            select(
                Event.project_id,
                Event.event_name,
                func.count().label("cnt"),
            )
            .where(Event.timestamp >= period_start, Event.timestamp <= now, REAL_EVENTS)
            .group_by(Event.project_id, Event.event_name)
        )

        for row in rows:
            stmt = (
                pg_insert(Aggregation)
                .values(
                    id=uuid.uuid4(),
                    project_id=row.project_id,
                    event_name=row.event_name,
                    period=period,
                    period_start=period_start,
                    count=row.cnt,
                    updated_at=now,
                )
                .on_conflict_do_update(
                    constraint="uq_aggregations_composite",
                    set_={"count": row.cnt, "updated_at": now},
                )
            )
            await session.execute(stmt)
            upserted += 1

    await session.flush()
    return upserted


async def run_retention_cron(session: AsyncSession) -> int:
    """Delete events and taps older than each project's ``retention_days``.

    A ``retention_days`` value of 0 means keep forever (no deletion).
    Returns the total number of rows deleted (events plus taps).
    """
    now = datetime.now(UTC)
    settings_rows = await session.execute(select(ProjectSettings))
    total_deleted = 0

    for settings in settings_rows.scalars():
        if settings.retention_days <= 0:
            continue
        cutoff = now - timedelta(days=settings.retention_days)
        result = await session.execute(
            delete(Event).where(
                Event.project_id == settings.project_id,
                Event.received_at < cutoff,
            )
        )
        total_deleted += result.rowcount  # type: ignore[attr-defined]
        result = await session.execute(
            delete(Tap).where(
                Tap.project_id == settings.project_id,
                Tap.received_at < cutoff,
            )
        )
        total_deleted += result.rowcount  # type: ignore[attr-defined]

    await session.flush()
    return total_deleted


async def reset_threshold_alert_counters(session: AsyncSession) -> None:
    """Reset threshold alert counters to 0 at midnight.

    Called once per day so threshold alerts can fire again the next day.
    """
    from sqlalchemy import update

    from app.models.alert import Alert, AlertCondition

    await session.execute(
        update(Alert).where(Alert.condition == AlertCondition.threshold).values(counter=0)
    )
    await session.flush()
