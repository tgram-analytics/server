"""Digest handler: /digest — last-7-days summary across all projects."""

from __future__ import annotations

import html
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from telegram import Update
from telegram.ext import ContextTypes

from app.bot.auth import requires_user
from app.models.event import Event
from app.models.project import Project
from app.models.user import User
from app.services.projects import list_projects


def _format_delta(current: int, previous: int) -> str:
    if previous == 0:
        if current == 0:
            return "—"
        return "🆕"
    pct = round((current - previous) / previous * 100, 1)
    arrow = "▲" if pct > 0 else ("▼" if pct < 0 else "·")
    sign = "+" if pct > 0 else ""
    return f"{arrow} {sign}{pct}%"


async def _project_digest_lines(
    session: AsyncSession,
    project: Project,
    now: datetime,
) -> list[str]:
    """Return HTML lines describing the last-7-days digest for one project."""
    week_ago = now - timedelta(days=7)
    two_weeks_ago = now - timedelta(days=14)

    total_curr = (
        await session.execute(
            select(func.count())
            .select_from(Event)
            .where(Event.project_id == project.id, Event.timestamp >= week_ago)
        )
    ).scalar_one()

    total_prev = (
        await session.execute(
            select(func.count())
            .select_from(Event)
            .where(
                Event.project_id == project.id,
                Event.timestamp >= two_weeks_ago,
                Event.timestamp < week_ago,
            )
        )
    ).scalar_one()

    sessions_curr = (
        await session.execute(
            select(func.count(func.distinct(Event.session_id)))
            .select_from(Event)
            .where(Event.project_id == project.id, Event.timestamp >= week_ago)
        )
    ).scalar_one()

    sessions_prev = (
        await session.execute(
            select(func.count(func.distinct(Event.session_id)))
            .select_from(Event)
            .where(
                Event.project_id == project.id,
                Event.timestamp >= two_weeks_ago,
                Event.timestamp < week_ago,
            )
        )
    ).scalar_one()

    top_events = (
        await session.execute(
            select(Event.event_name, func.count().label("cnt"))
            .where(Event.project_id == project.id, Event.timestamp >= week_ago)
            .group_by(Event.event_name)
            .order_by(func.count().desc())
            .limit(3)
        )
    ).all()

    lines = [
        f"📦 <b>{html.escape(project.name)}</b>",
        f"  📊 Events: <b>{total_curr:,}</b>  {_format_delta(total_curr, total_prev)}",
        f"  👤 Sessions: <b>{sessions_curr:,}</b>  {_format_delta(sessions_curr, sessions_prev)}",
    ]
    if top_events:
        top_str = ", ".join(
            f"{html.escape(row.event_name)} (<b>{row.cnt:,}</b>)" for row in top_events
        )
        lines.append(f"  🏷 Top: {top_str}")
    else:
        lines.append("  <i>no events this week</i>")
    return lines


@requires_user
async def digest_command(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    *,
    user: User,
    session: AsyncSession,
) -> None:
    """/digest — last-7-days summary across all the user's projects."""
    assert update.message is not None

    projects = await list_projects(session, user.id)
    if not projects:
        await update.message.reply_text(
            "📭 No projects yet.\n\nUse /add <i>name</i> to create one.",
            parse_mode="HTML",
        )
        return

    now = datetime.now(UTC)
    week_ago = now - timedelta(days=7)
    period = f"{week_ago.strftime('%-d %b')} – {now.strftime('%-d %b')}"

    header = [
        "📰 <b>Weekly digest</b>",
        f"<i>{period}  ·  {len(projects)} project{'s' if len(projects) != 1 else ''}</i>",
        "─────────────────",
    ]

    body: list[str] = []
    for i, project in enumerate(projects):
        if i > 0:
            body.append("")
        body.extend(await _project_digest_lines(session, project, now))

    await update.message.reply_text(
        "\n".join(header + body),
        parse_mode="HTML",
    )
