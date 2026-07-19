"""Digest handler: /digest — last-7-days summary across all projects."""

from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from telegram import Update
from telegram.ext import ContextTypes

from app.bot.auth import requires_user
from app.bot.rich import reply_rich_html
from app.models.alert import Alert
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


@dataclass
class _ProjectDigest:
    name: str
    sessions_curr: int
    sessions_prev: int
    # (event_name, current-week count, previous-week count), sorted by count.
    events: list[tuple[str, int, int]]
    has_alerts: bool


async def _project_digest(
    session: AsyncSession,
    project: Project,
    now: datetime,
) -> _ProjectDigest:
    """Collect the last-7-days digest numbers for one project."""
    week_ago = now - timedelta(days=7)
    two_weeks_ago = now - timedelta(days=14)

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

    alerted_names = list(
        (
            await session.execute(
                select(Alert.event_name)
                .where(Alert.project_id == project.id, Alert.is_active.is_(True))
                .distinct()
            )
        ).scalars()
    )

    if not alerted_names:
        return _ProjectDigest(project.name, sessions_curr, sessions_prev, [], has_alerts=False)

    counts_rows = (
        await session.execute(
            select(
                Event.event_name,
                func.sum(case((Event.timestamp >= week_ago, 1), else_=0)).label("curr"),
                func.sum(
                    case(
                        (
                            (Event.timestamp >= two_weeks_ago) & (Event.timestamp < week_ago),
                            1,
                        ),
                        else_=0,
                    )
                ).label("prev"),
            )
            .where(
                Event.project_id == project.id,
                Event.event_name.in_(alerted_names),
                Event.timestamp >= two_weeks_ago,
            )
            .group_by(Event.event_name)
        )
    ).all()

    counts: dict[str, tuple[int, int]] = {
        row.event_name: (int(row.curr or 0), int(row.prev or 0)) for row in counts_rows
    }

    rows = [(name, *counts.get(name, (0, 0))) for name in alerted_names]
    rows.sort(key=lambda r: (-r[1], r[0]))

    return _ProjectDigest(project.name, sessions_curr, sessions_prev, rows, has_alerts=True)


def _project_lines(d: _ProjectDigest) -> list[str]:
    """Classic HTML lines for one project (sendMessage fallback)."""
    lines = [
        f"📦 <b>{html.escape(d.name)}</b>",
        f"  👤 Sessions: <b>{d.sessions_curr:,}</b>"
        f"  {_format_delta(d.sessions_curr, d.sessions_prev)}",
    ]
    if not d.has_alerts:
        lines.append("  💤 No alerts — set one with /alerts to track core events")
        return lines
    for name, curr, prev in d.events:
        lines.append(f"  🎯 {html.escape(name)}: <b>{curr:,}</b>  {_format_delta(curr, prev)}")
    return lines


def _project_rich_section(d: _ProjectDigest) -> str:
    """Rich-HTML section for one project: sub-heading + metrics table."""
    rows = [
        "<tr><th></th><th>7d</th><th>vs prev</th></tr>",
        f"<tr><td>👤 Sessions</td><td><b>{d.sessions_curr:,}</b></td>"
        f"<td>{_format_delta(d.sessions_curr, d.sessions_prev)}</td></tr>",
    ]
    for name, curr, prev in d.events:
        rows.append(
            f"<tr><td>🎯 {html.escape(name)}</td><td><b>{curr:,}</b></td>"
            f"<td>{_format_delta(curr, prev)}</td></tr>"
        )
    section = [f"<h5>📦 {html.escape(d.name)}</h5>", f"<table>{''.join(rows)}</table>"]
    if not d.has_alerts:
        section.append("💤 No alerts — set one with /alerts to track core events")
    return "\n".join(section)


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

    subtitle = f"{period}  ·  {len(projects)} project{'s' if len(projects) != 1 else ''}"
    digests = [await _project_digest(session, project, now) for project in projects]

    header = [
        "📰 <b>Weekly digest</b>",
        f"<i>{subtitle}</i>",
        "─────────────────",
    ]
    body: list[str] = []
    for i, d in enumerate(digests):
        if i > 0:
            body.append("")
        body.extend(_project_lines(d))
    fallback = "\n".join(header + body)

    rich = "\n".join(
        ["<h4>📰 Weekly digest</h4>", f"<i>{subtitle}</i>"]
        + [_project_rich_section(d) for d in digests]
    )

    await reply_rich_html(update.message, rich, fallback)
