"""Event insertion and alert evaluation service."""

import uuid
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.alert import Alert, AlertCondition
from app.models.event import Event


async def insert_event(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    event_name: str,
    session_id: str,
    properties: dict[str, Any],
    timestamp: datetime | None = None,
    url: str | None = None,
    referrer: str | None = None,
    visitor_hash: str | None = None,
    browser: str | None = None,
    os: str | None = None,
    device_type: str | None = None,
) -> Event:
    """Insert an event row.

    ``received_at`` is always server time (set by the DB default).
    ``timestamp`` defaults to server now() when the caller passes None.

    Privacy fields (``visitor_hash``, ``browser``, ``os``, ``device_type``)
    are computed by the API layer from the request IP/UA and passed in
    pre-derived; this service never sees the raw IP or UA.
    """
    event = Event(
        project_id=project_id,
        event_name=event_name,
        session_id=session_id,
        properties=properties,
        url=url,
        referrer=referrer,
        visitor_hash=visitor_hash,
        browser=browser,
        os=os,
        device_type=device_type,
    )
    if timestamp is not None:
        event.timestamp = timestamp
    session.add(event)
    await session.flush()
    await session.refresh(event)
    return event


async def evaluate_alerts(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    event_name: str,
) -> list[Alert]:
    """Evaluate all active alerts for *project_id* + *event_name*.

    Mutates alert counters in-place and flushes; the caller must commit.
    Returns the list of alerts that fired (for logging / notification).
    Does NOT send Telegram notifications — that is Phase 8's job.
    """
    now = datetime.now(UTC)
    result = await session.execute(
        select(Alert).where(
            Alert.project_id == project_id,
            Alert.event_name == event_name,
            Alert.is_active.is_(True),
            sa.or_(Alert.muted_until.is_(None), Alert.muted_until <= now),
        )
    )
    alerts = list(result.scalars().all())
    fired: list[Alert] = []

    for alert in alerts:
        if alert.condition == AlertCondition.every:
            fired.append(alert)

        elif alert.condition == AlertCondition.every_n:
            alert.counter += 1
            if alert.threshold_n and alert.counter >= alert.threshold_n:
                alert.counter = 0
                fired.append(alert)

        elif alert.condition == AlertCondition.threshold:
            today_start = datetime.combine(now.date(), datetime.min.time()).replace(tzinfo=UTC)
            count_result = await session.execute(
                select(func.count())
                .select_from(Event)
                .where(
                    Event.project_id == project_id,
                    Event.event_name == event_name,
                    Event.received_at >= today_start,
                )
            )
            today_count = count_result.scalar_one()
            # counter == 0 means the alert has not yet fired today
            if alert.threshold_n and today_count >= alert.threshold_n and alert.counter == 0:
                alert.counter = 1
                fired.append(alert)

    await session.flush()
    return fired


def is_origin_allowed(domain_allowlist: list[str], origin: str | None) -> bool:
    """Return True if the request passes the allowlist check.

    The allowlist is a browser-only guard against abuse of the public
    ``proj_`` key embedded in JS bundles. Server-to-server callers (backend
    SDKs, curl) don't send an ``Origin`` header and are authenticated by
    the API key alone.

    * Empty allowlist → accept all.
    * No ``Origin`` header → accept (server-to-server).
    * ``Origin: null`` (sandboxed/file://) → reject when allowlist is set.
    * Entry may be a bare host, a URL, or a wildcard ``*.example.com``
      matching any subdomain (but not the apex).
    """
    if not domain_allowlist:
        return True
    if origin is None:
        return True
    if origin == "null":
        return False
    from urllib.parse import urlparse

    host = urlparse(origin).netloc or origin
    for entry in domain_allowlist:
        if entry.startswith("*."):
            suffix = entry[1:]  # ".example.com"
            if host.endswith(suffix) and host != suffix[1:]:
                return True
            continue
        normalized = urlparse(entry).netloc or entry
        if host == normalized:
            return True
    return False
