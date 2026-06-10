"""Digest handler: /digest — last-7-days summary across all projects."""

from __future__ import annotations

import html
from datetime import UTC, datetime, timedelta

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from telegram import Update
from telegram.ext import ContextTypes

from app.bot.auth import requires_user
from app.models.alert import Alert
from app.models.event import Event
from app.models.project import Project
from app.models.user import User
from app.services.kpis import list_kpis
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
    """Return HTML lines describing the last-7-days digest for one project.

    Order: North Star → Visitors → Sessions → Pageviews → pinned KPIs →
    alerted events not already shown as KPIs.
    """
    week_ago = now - timedelta(days=7)
    two_weeks_ago = now - timedelta(days=14)

    kpis = await list_kpis(session, project_id=project.id)
    north_star = next((k for k in kpis if k.is_north_star), None)
    pinned = [k for k in kpis if not k.is_north_star]

    alerted_names = list(
        (
            await session.execute(
                select(Alert.event_name)
                .where(Alert.project_id == project.id, Alert.is_active.is_(True))
                .distinct()
            )
        ).scalars()
    )

    async def _distinct_counts(column) -> tuple[int, int]:
        curr = (
            await session.execute(
                select(func.count(func.distinct(column)))
                .select_from(Event)
                .where(Event.project_id == project.id, Event.timestamp >= week_ago)
            )
        ).scalar_one()
        prev = (
            await session.execute(
                select(func.count(func.distinct(column)))
                .select_from(Event)
                .where(
                    Event.project_id == project.id,
                    Event.timestamp >= two_weeks_ago,
                    Event.timestamp < week_ago,
                )
            )
        ).scalar_one()
        return curr, prev

    visitors_curr, visitors_prev = await _distinct_counts(Event.visitor_hash)
    sessions_curr, sessions_prev = await _distinct_counts(Event.session_id)

    # One grouped query covers pageviews, all pinned KPIs, and alerted events.
    tracked_names = {"pageview", *(k.event_name for k in kpis), *alerted_names}
    counts: dict[str, tuple[int, int]] = {}
    if tracked_names:
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
                    Event.event_name.in_(tracked_names),
                    Event.timestamp >= two_weeks_ago,
                )
                .group_by(Event.event_name)
            )
        ).all()
        counts = {row.event_name: (int(row.curr or 0), int(row.prev or 0)) for row in counts_rows}

    lines = [f"📦 <b>{html.escape(project.name)}</b>"]

    if north_star is not None:
        curr, prev = counts.get(north_star.event_name, (0, 0))
        lines.append(
            f"  ⭐ {html.escape(north_star.event_name)}: <b>{curr:,}</b>"
            f"  {_format_delta(curr, prev)}"
        )

    lines.append(
        f"  👥 Visitors: <b>{visitors_curr:,}</b>  {_format_delta(visitors_curr, visitors_prev)}"
    )
    lines.append(
        f"  👤 Sessions: <b>{sessions_curr:,}</b>  {_format_delta(sessions_curr, sessions_prev)}"
    )

    pv_curr, pv_prev = counts.get("pageview", (0, 0))
    pageviews_shown = pv_curr > 0 or pv_prev > 0
    if pageviews_shown:
        lines.append(f"  📄 Pageviews: <b>{pv_curr:,}</b>  {_format_delta(pv_curr, pv_prev)}")

    for kpi in pinned:
        curr, prev = counts.get(kpi.event_name, (0, 0))
        lines.append(
            f"  🎯 {html.escape(kpi.event_name)}: <b>{curr:,}</b>  {_format_delta(curr, prev)}"
        )

    shown = {k.event_name for k in kpis}
    if pageviews_shown:
        shown.add("pageview")
    remaining = [n for n in alerted_names if n not in shown]
    rows = [(name, *counts.get(name, (0, 0))) for name in remaining]
    rows.sort(key=lambda r: (-r[1], r[0]))
    for name, curr, prev in rows:
        lines.append(f"  🎯 {html.escape(name)}: <b>{curr:,}</b>  {_format_delta(curr, prev)}")

    if north_star is None:
        lines.append("  💤 Pin your North Star with the 🎯 KPIs button in /projects")

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
