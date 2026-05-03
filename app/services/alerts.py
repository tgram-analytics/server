"""Alert CRUD service.

All functions accept an ``AsyncSession`` and flush but do NOT commit —
the caller is responsible for committing or rolling back.
"""

import uuid
from datetime import UTC, datetime, timedelta

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
        select(Alert).where(Alert.project_id == project_id).order_by(Alert.created_at)
    )
    return list(result.scalars().all())


async def get_alert(
    session: AsyncSession,
    alert_id: uuid.UUID,
    project_id: uuid.UUID | None = None,
) -> Alert | None:
    """Return an alert by ID, or None if not found.

    If project_id is provided, also verifies the alert belongs to that project.
    """
    query = select(Alert).where(Alert.id == alert_id)
    if project_id is not None:
        query = query.where(Alert.project_id == project_id)
    result = await session.execute(query)
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


async def disable_alert(
    session: AsyncSession,
    alert_id: uuid.UUID,
) -> Alert | None:
    """Set is_active=False on an alert (called from notification buttons).

    Does not verify project ownership — the bot already authenticates the
    user before dispatching to this handler.
    Returns None if the alert is not found.
    """
    alert = await get_alert(session, alert_id)
    if alert is None:
        return None
    alert.is_active = False
    await session.flush()
    await session.refresh(alert)
    return alert


async def get_active_alerts_across_projects(
    session: AsyncSession,
    owner_user_id: uuid.UUID,
) -> list[tuple[Alert, str]]:
    """Return all active alerts across all projects owned by *owner_user_id*.

    Returns a list of ``(alert, project_name)`` tuples ordered by project name
    then alert creation time.
    """
    from app.models.project import Project

    result = await session.execute(
        select(Alert, Project.name)
        .join(Project, Alert.project_id == Project.id)
        .where(
            Project.owner_user_id == owner_user_id,
            Alert.is_active == True,  # noqa: E712
        )
        .order_by(Project.name, Alert.created_at)
    )
    return [(row.Alert, row.name) for row in result]


async def mute_alert(
    session: AsyncSession,
    alert_id: uuid.UUID,
    hours: int,
) -> Alert | None:
    """Set muted_until = now + hours on an alert. Returns None if not found."""
    alert = await get_alert(session, alert_id)
    if alert is None:
        return None
    alert.muted_until = datetime.now(UTC) + timedelta(hours=hours)
    await session.flush()
    await session.refresh(alert)
    return alert
