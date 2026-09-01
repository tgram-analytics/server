"""Funnel CRUD and conversion analysis."""

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.event import Event
from app.models.funnel import Funnel

# ── CRUD ──────────────────────────────────────────────────────────────────────


async def create_funnel(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    name: str,
    steps: list[str],
    time_window: int,
) -> Funnel:
    funnel = Funnel(
        project_id=project_id,
        name=name,
        steps=steps,
        time_window=time_window,
    )
    session.add(funnel)
    await session.flush()
    await session.refresh(funnel)
    return funnel


async def list_funnels(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
) -> list[Funnel]:
    result = await session.execute(
        select(Funnel).where(Funnel.project_id == project_id).order_by(Funnel.created_at.desc())
    )
    return list(result.scalars().all())


async def get_funnel(session: AsyncSession, funnel_id: uuid.UUID) -> Funnel | None:
    result = await session.execute(select(Funnel).where(Funnel.id == funnel_id))
    return result.scalar_one_or_none()


async def rename_funnel(session: AsyncSession, funnel_id: uuid.UUID, name: str) -> Funnel | None:
    """Give an existing funnel a new display name. Returns the updated row."""
    funnel = await get_funnel(session, funnel_id)
    if funnel is None:
        return None
    funnel.name = name
    await session.flush()
    return funnel


async def delete_funnel(session: AsyncSession, funnel_id: uuid.UUID) -> None:
    await session.execute(delete(Funnel).where(Funnel.id == funnel_id))


# ── Funnel analysis ──────────────────────────────────────────────────────────


async def analyze_funnel(
    session: AsyncSession,
    *,
    funnel: Funnel,
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    """Analyze a funnel: count sessions that complete each step in order.

    *start*/*end* define when step 1 must occur.  The ``time_window`` on the
    funnel is the maximum seconds from step 1 to the final step.

    Returns ``[{"step": "event_name", "count": int}, ...]`` for each step.
    """
    steps: list[str] = funnel.steps
    if not steps:
        return []

    window = timedelta(seconds=funnel.time_window)

    # Build the query iteratively using CTEs.
    # step_0: sessions that triggered steps[0] in [start, end), with their
    #         earliest timestamp for that event.
    # step_N: of the sessions from step_{N-1}, those where steps[N] occurred
    #         AFTER step_{N-1}'s timestamp and within the time window from step_0.

    # Step 0 CTE
    step0 = (
        select(
            Event.session_id.label("session_id"),
            func.min(Event.timestamp).label("t0"),
            func.min(Event.timestamp).label("t_prev"),
        )
        .where(
            Event.project_id == funnel.project_id,
            Event.event_name == steps[0],
            Event.timestamp >= start,
            Event.timestamp < end,
        )
        .group_by(Event.session_id)
        .cte(name="step_0")
    )

    ctes = [step0]

    for i in range(1, len(steps)):
        prev = ctes[i - 1]
        # For each session from the previous step, find the earliest occurrence
        # of steps[i] that is after t_prev and within the time window from t0.
        step_cte = (
            select(
                prev.c.session_id.label("session_id"),
                prev.c.t0.label("t0"),
                func.min(Event.timestamp).label("t_prev"),
            )
            .select_from(prev.join(Event, Event.session_id == prev.c.session_id))
            .where(
                Event.project_id == funnel.project_id,
                Event.event_name == steps[i],
                Event.timestamp > prev.c.t_prev,
                Event.timestamp <= prev.c.t0 + window,
            )
            .group_by(prev.c.session_id, prev.c.t0)
            .cte(name=f"step_{i}")
        )
        ctes.append(step_cte)

    # Now count distinct sessions at each step.
    results: list[dict[str, Any]] = []
    for i, cte in enumerate(ctes):
        count_result = await session.execute(select(func.count(func.distinct(cte.c.session_id))))
        count = count_result.scalar_one()
        results.append({"step": steps[i], "count": count})

    return results
