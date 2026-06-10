"""KPI CRUD: pinned per-project KPI events and the North Star metric."""

import uuid

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.kpi import Kpi


async def list_kpis(session: AsyncSession, *, project_id: uuid.UUID) -> list[Kpi]:
    """All KPIs for a project: North Star first, then insertion order."""
    result = await session.execute(
        select(Kpi)
        .where(Kpi.project_id == project_id)
        .order_by(Kpi.is_north_star.desc(), Kpi.position, Kpi.created_at)
    )
    return list(result.scalars().all())


async def get_kpi(session: AsyncSession, kpi_id: uuid.UUID) -> Kpi | None:
    result = await session.execute(select(Kpi).where(Kpi.id == kpi_id))
    return result.scalar_one_or_none()


async def add_kpi(session: AsyncSession, *, project_id: uuid.UUID, event_name: str) -> Kpi:
    """Pin an event as a KPI. Idempotent: returns the existing row if pinned."""
    existing = await session.execute(
        select(Kpi).where(Kpi.project_id == project_id, Kpi.event_name == event_name)
    )
    kpi = existing.scalar_one_or_none()
    if kpi is not None:
        return kpi

    max_pos = (
        await session.execute(
            select(func.coalesce(func.max(Kpi.position), -1)).where(Kpi.project_id == project_id)
        )
    ).scalar_one()

    kpi = Kpi(project_id=project_id, event_name=event_name, position=max_pos + 1)
    session.add(kpi)
    await session.flush()
    await session.refresh(kpi)
    return kpi


async def remove_kpi(session: AsyncSession, kpi_id: uuid.UUID) -> None:
    await session.execute(delete(Kpi).where(Kpi.id == kpi_id))


async def set_north_star(session: AsyncSession, *, project_id: uuid.UUID, event_name: str) -> Kpi:
    """Make *event_name* the project's North Star, pinning it first if needed.

    Clears any previous North Star flag before setting the new one so the
    partial unique index is never violated.
    """
    await session.execute(
        update(Kpi)
        .where(Kpi.project_id == project_id, Kpi.is_north_star.is_(True))
        .values(is_north_star=False)
    )
    await session.flush()

    kpi = await add_kpi(session, project_id=project_id, event_name=event_name)
    kpi.is_north_star = True
    await session.flush()
    return kpi
