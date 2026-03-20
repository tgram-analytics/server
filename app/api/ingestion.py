"""Event ingestion endpoints: POST /api/v1/track and POST /api/v1/pageview.

Authentication: ``api_key`` field in the JSON request body.
Rate limiting:  per-project sliding-window (configurable, default 100 req/s).
Origin check:   project's domain_allowlist; empty list = allow all.
"""

import uuid
from collections import defaultdict, deque
from time import monotonic
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.database import get_session, get_session_factory
from app.core.security import validate_api_key
from app.schemas.event import PageviewRequest, TrackEventRequest
from app.services.events import evaluate_alerts, insert_event, is_origin_allowed

router = APIRouter(prefix="/api/v1", tags=["ingestion"])

# ── In-memory per-project rate limiter (sliding 1-second window) ───────────

_rate_windows: dict[str, deque[float]] = defaultdict(deque)


def _is_rate_limited(project_id: uuid.UUID, limit: int) -> bool:
    """Return True if this project has exceeded *limit* requests in the last second."""
    key = str(project_id)
    now = monotonic()
    dq = _rate_windows[key]
    cutoff = now - 1.0
    while dq and dq[0] < cutoff:
        dq.popleft()
    if len(dq) >= limit:
        return True
    dq.append(now)
    return False


# ── Background task helper ─────────────────────────────────────────────────


async def _run_alert_evaluation(
    project_id: uuid.UUID, event_name: str, properties: dict[str, Any] | None = None
) -> None:
    """Background task: evaluate alerts after event insertion.

    Creates its own session so it can run after the HTTP response is sent.
    Sends Telegram notifications for fired alerts.
    """
    import logging

    from sqlalchemy import select

    from app.bot.setup import get_bot
    from app.models.alert import AlertCondition
    from app.models.project import Project

    log = logging.getLogger(__name__)
    try:
        factory = get_session_factory()
        async with factory() as session, session.begin():
            fired = await evaluate_alerts(session, project_id=project_id, event_name=event_name)
            if not fired:
                return

            log.info(
                "alerts fired: project=%s event=%s count=%d",
                project_id,
                event_name,
                len(fired),
            )

            result = await session.execute(select(Project).where(Project.id == project_id))
            project = result.scalar_one_or_none()
            if project is None:
                log.warning("project not found for alert notification: %s", project_id)
                return

            from telegram import InlineKeyboardButton, InlineKeyboardMarkup

            bot = get_bot()
            for alert in fired:
                if alert.condition == AlertCondition.every:
                    msg = f"🔔 Event <b>{event_name}</b> received " f"on <b>{project.name}</b>"
                elif alert.condition == AlertCondition.every_n:
                    msg = (
                        f"🔔 Event <b>{event_name}</b> received "
                        f"<b>{alert.threshold_n}</b> times on <b>{project.name}</b>"
                    )
                else:  # threshold
                    msg = (
                        f"🔔 Event <b>{event_name}</b> exceeded "
                        f"<b>{alert.threshold_n}</b> today on <b>{project.name}</b>"
                    )

                if properties:
                    lines = [f"<b>{k}:</b> {v}" for k, v in properties.items()]
                    msg += "\n\n" + "\n".join(lines)

                aid = str(alert.id)
                keyboard = InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton("🔕 Silence", callback_data=f"alert_sil:{aid}"),
                            InlineKeyboardButton("🚫 Disable", callback_data=f"alert_dis:{aid}"),
                        ]
                    ]
                )

                try:
                    await bot.send_message(
                        chat_id=project.admin_chat_id,
                        text=msg,
                        parse_mode="HTML",
                        reply_markup=keyboard,
                    )
                except Exception:
                    log.exception(
                        "failed to send alert notification: alert=%s project=%s",
                        alert.id,
                        project_id,
                    )
    except Exception:
        log.exception("alert evaluation failed for project=%s event=%s", project_id, event_name)


# ── Shared pre-flight logic ────────────────────────────────────────────────


async def _resolve_project(
    api_key: str,
    origin: str | None,
    session: AsyncSession,
    rate_limit: int,
):
    """Validate API key, rate limit, and origin. Returns the Project."""
    project = await validate_api_key(api_key, session)
    if project is None:
        raise HTTPException(status_code=400, detail="Invalid API key")

    if _is_rate_limited(project.id, rate_limit):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")

    if not is_origin_allowed(project.domain_allowlist, origin):
        raise HTTPException(status_code=403, detail="Origin not in allowlist")

    return project


# ── Endpoints ─────────────────────────────────────────────────────────────


@router.post("/track", status_code=202)
async def track(
    body: TrackEventRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Ingest a custom event.

    Returns 202 immediately; alert evaluation runs as a background task.
    """
    origin = request.headers.get("origin")
    project = await _resolve_project(body.api_key, origin, session, settings.rate_limit_per_second)

    await insert_event(
        session,
        project_id=project.id,
        event_name=body.event_name,
        session_id=body.session_id,
        properties=body.properties,
        timestamp=body.timestamp,
    )
    await session.commit()

    background_tasks.add_task(_run_alert_evaluation, project.id, body.event_name, body.properties)
    return {"status": "accepted"}


@router.post("/pageview", status_code=202)
async def pageview(
    body: PageviewRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Ingest a pageview event.

    Forces ``event_name = "pageview"`` and stores url/referrer in dedicated
    columns as well as in ``properties`` for easy querying.
    """
    origin = request.headers.get("origin")
    project = await _resolve_project(body.api_key, origin, session, settings.rate_limit_per_second)

    properties = {**body.properties, "url": body.url}
    if body.referrer:
        properties["referrer"] = body.referrer

    await insert_event(
        session,
        project_id=project.id,
        event_name="pageview",
        session_id=body.session_id,
        properties=properties,
        timestamp=body.timestamp,
        url=body.url,
        referrer=body.referrer,
    )
    await session.commit()

    background_tasks.add_task(_run_alert_evaluation, project.id, "pageview", properties)
    return {"status": "accepted"}
