"""Alert CRUD service.

All functions accept an ``AsyncSession`` and flush but do NOT commit —
the caller is responsible for committing or rolling back.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.alert import Alert, AlertCondition


async def create_alert(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    event_name: str,
    condition: AlertCondition,
    threshold_n: int | None = None,
) -> Alert:
    """Create an alert for a project.

    For ``every_n`` and ``threshold`` conditions, ``threshold_n`` is required.
    For ``every``, it should be None.
    """
    alert = Alert(
        project_id=project_id,
        event_name=event_name,
        condition=condition,
        threshold_n=threshold_n,
    )
    session.add(alert)
    await session.flush()
    await session.refresh(alert)
    return alert


async def list_alerts(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> list[Alert]:
    """Return all alerts for a project, ordered by creation time."""
    result = await session.execute(
        select(Alert)
        .where(Alert.project_id == project_id)
        .order_by(Alert.created_at)
    )
    return list(result.scalars().all())


async def get_alert(
    session: AsyncSession,
    alert_id: uuid.UUID,
    project_id: uuid.UUID,
) -> Alert | None:
    """Return an alert by ID, or None if not found or doesn't belong to project."""
    result = await session.execute(
        select(Alert).where(
            Alert.id == alert_id,
            Alert.project_id == project_id,
        )
    )
    return result.scalar_one_or_none()


async def delete_alert(
    session: AsyncSession,
    alert_id: uuid.UUID,
    project_id: uuid.UUID,
) -> bool:
    """Delete an alert. Returns False if not found."""
    alert = await get_alert(session, alert_id, project_id)
    if alert is None:
        return False
    await session.delete(alert)
    await session.flush()
    return True


async def toggle_alert(
    session: AsyncSession,
    alert_id: uuid.UUID,
    project_id: uuid.UUID,
) -> Alert | None:
    """Toggle the is_active flag on an alert. Returns None if not found."""
    alert = await get_alert(session, alert_id, project_id)
    if alert is None:
        return None
    alert.is_active = not alert.is_active
    await session.flush()
    await session.refresh(alert)
    return alert
