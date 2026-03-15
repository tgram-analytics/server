"""Reports handler: 7-day analytics summary with on-demand chart."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from app.core.config import get_settings
from app.core.database import get_session_factory
from app.models.event import Event
from app.services.analytics import events_over_time
from app.services.charts import ChartGenerationError, generate_line_chart
from app.services.projects import get_project


async def show_reports_menu(query, project_id_str: str, admin_chat_id: int) -> None:
    """Show a 7-day analytics text summary for the project."""
    pid = uuid.UUID(project_id_str)
    now = datetime.now(UTC)
    seven_days_ago = now - timedelta(days=7)

    factory = get_session_factory()
    async with factory() as session:
        project = await get_project(session, pid, admin_chat_id)
        if project is None:
            await query.edit_message_text("❌ Project not found.")
            return

        # Total events in last 7 days
        total_result = await session.execute(
            select(func.count())
            .select_from(Event)
            .where(
                Event.project_id == pid,
                Event.timestamp >= seven_days_ago,
            )
        )
        total = total_result.scalar_one()

        # Unique sessions in last 7 days
        sessions_result = await session.execute(
            select(func.count(func.distinct(Event.session_id)))
            .select_from(Event)
            .where(
                Event.project_id == pid,
                Event.timestamp >= seven_days_ago,
            )
        )
        unique_sessions = sessions_result.scalar_one()

        # Top 5 event names by count
        top_result = await session.execute(
            select(Event.event_name, func.count().label("cnt"))
            .where(Event.project_id == pid, Event.timestamp >= seven_days_ago)
            .group_by(Event.event_name)
            .order_by(func.count().desc())
            .limit(5)
        )
        top_events = top_result.all()

    period = f"{seven_days_ago.strftime('%-d %b')} – {now.strftime('%-d %b')}"
    lines = [
        f"📈 <b>Report: {project.name}</b>",
        f"<i>{period}</i>",
        "─────────────────",
        f"📊 Total events: <b>{total:,}</b>",
        f"👤 Unique sessions: <b>{unique_sessions:,}</b>",
    ]

    if top_events:
        lines.append("\n<b>Top events (7 days):</b>")
        for row in top_events:
            lines.append(f"  • {row.event_name}: <b>{row.cnt:,}</b>")
    else:
        lines.append("\n<i>No events in the last 7 days.</i>")

    text = "\n".join(lines)
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📊 View Chart", callback_data=f"rpt_chart:{project_id_str}")],
            [InlineKeyboardButton("« Back", callback_data=f"proj:{project_id_str}")],
        ]
    )
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)


async def send_chart_photo(query, project_id_str: str, admin_chat_id: int) -> None:
    """Generate and send a 7-day line chart as a photo reply."""
    pid = uuid.UUID(project_id_str)
    now = datetime.now(UTC)
    seven_days_ago = now - timedelta(days=7)
    settings = get_settings()

    factory = get_session_factory()
    async with factory() as session:
        project = await get_project(session, pid, admin_chat_id)
        if project is None:
            await query.edit_message_text("❌ Project not found.")
            return

        # Try pageview first; fall back to the most-frequent event
        data = await events_over_time(
            session,
            project_id=pid,
            event_name="pageview",
            start=seven_days_ago,
            end=now,
            granularity="day",
        )
        chart_event = "pageview"

        if not data:
            top_result = await session.execute(
                select(Event.event_name, func.count().label("cnt"))
                .where(Event.project_id == pid, Event.timestamp >= seven_days_ago)
                .group_by(Event.event_name)
                .order_by(func.count().desc())
                .limit(1)
            )
            top = top_result.first()
            if top:
                chart_event = top.event_name
                data = await events_over_time(
                    session,
                    project_id=pid,
                    event_name=chart_event,
                    start=seven_days_ago,
                    end=now,
                    granularity="day",
                )

    back_keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("« Back", callback_data=f"menu:reports:{project_id_str}")],
        ]
    )

    if not data:
        await query.edit_message_text(
            "📭 No event data available for chart.",
            reply_markup=back_keyboard,
        )
        return

    try:
        png_bytes = await generate_line_chart(
            data,
            title=chart_event,
            period_label="Last 7 days",
            quickchart_url=settings.quickchart_url,
        )
    except ChartGenerationError:
        await query.edit_message_text(
            "⚠️ Chart service unavailable. Please try again later.",
            reply_markup=back_keyboard,
        )
        return

    # Edit original message to a navigation anchor, then send the photo below it
    await query.edit_message_text(
        f"📊 <b>{chart_event}</b> — last 7 days  ↓",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "« Back to report", callback_data=f"menu:reports:{project_id_str}"
                    )
                ],
            ]
        ),
    )
    await query.message.reply_photo(
        photo=png_bytes,
        caption=f"📈 {project.name} · {chart_event} · last 7 days",
    )
